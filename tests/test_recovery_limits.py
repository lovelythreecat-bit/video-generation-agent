from pathlib import Path

import pytest
from PIL import Image

from tests.recovery_support import (
    capabilities,
    confirm_and_finish,
    drain,
    make_agent,
    open_plan,
    read_rows,
    require_ffmpeg,
    wav_bytes,
)
from video_agent.errors import VideoAgentError
from video_agent.media import probe, still_clip
from video_agent.ports import QueryResult, SubmitResult, VoiceCue, VoiceResult


class HalfVideo:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.submits = 0
        self.downloads = 0
        self.request = None

    def capabilities(self):
        return capabilities()

    def submit_shot(self, request):
        self.submits += 1
        self.request = request
        return SubmitResult(external_task_id=request.provider_idempotency_key, uncertain=False)

    def query(self, *, external_task_id, provider_idempotency_key):
        del external_task_id, provider_idempotency_key
        return QueryResult(state="succeeded")

    def download(self, external_task_id: str) -> bytes:
        del external_task_id
        self.downloads += 1
        request = self.request
        image = self.directory / "half.png"
        Image.new("RGB", (request.width, request.height), (12, 12, 12)).save(image)
        dest = self.directory / "half.mp4"
        still_clip(image, max(1, request.frame_count // 2), request.width, request.height, dest, video_bitrate=8_000_000)
        return dest.read_bytes()


def test_default_fake_keeps_the_full_frame_count(tmp_path: Path):
    require_ffmpeg()
    agent = make_agent(tmp_path)
    task, _image = open_plan(agent, duration=5)
    finished = confirm_and_finish(agent, task.task_id)
    assert finished.status == "succeeded"
    clip = agent.home / "tasks" / task.task_id / "generations" / finished.generation_id / "shots" / "s1.mp4"
    assert probe(clip).frame_count == 5 * 30


def test_injected_short_clip_does_not_resubmit(tmp_path: Path):
    require_ffmpeg()
    agent = make_agent(tmp_path)
    video = HalfVideo(tmp_path)
    agent.video = video
    task, _image = open_plan(agent, duration=5)
    confirmed = agent.confirm(task.task_id, int(agent.get_task(task.task_id).head_plan_version))
    failed = drain(agent, task.task_id)
    assert failed.status == "failed"
    assert failed.failure is not None
    assert failed.failure.code == "SHOT_DURATION_SHORT"
    assert failed.failure.retryable is True
    row = read_rows(
        agent.home,
        "SELECT auto_attempts_used FROM generations WHERE generation_id=?",
        (confirmed.generation_id,),
    )[0]
    assert row["auto_attempts_used"] == 2
    assert video.submits == 1
    assert video.downloads == 3


class LongVoice:
    def synthesize(self, *, text: str, language: str | None, preference: str) -> VoiceResult:
        del text, language, preference
        return VoiceResult(audio=b"too-long", cues=[VoiceCue(text="卖点", start_ms=0, end_ms=30000)], duration_ms=30000)


def test_unused_independent_voice_does_not_block_export(tmp_path: Path):
    require_ffmpeg()
    agent = make_agent(tmp_path)
    agent.voice = LongVoice()
    task, _image = open_plan(agent, duration=5)
    agent.confirm(task.task_id, int(agent.get_task(task.task_id).head_plan_version))
    finished = drain(agent, task.task_id)
    assert finished.status == "succeeded"
    assert finished.failure is None
    used = read_rows(
        agent.home, "SELECT auto_attempts_used FROM generations WHERE generation_id=?",
        (finished.generation_id,),
    )[0]
    assert used["auto_attempts_used"] == 0


def test_missing_asset_does_not_resubmit_the_video_model(tmp_path: Path):
    require_ffmpeg()
    agent = make_agent(tmp_path)
    submits = {"count": 0}
    original = agent.video.submit_shot

    def count_submit(request):
        submits["count"] += 1
        return original(request)

    agent.video.submit_shot = count_submit
    music_id = agent.register_asset(filename="bed.wav", kind="audio", data=wav_bytes())
    task, image_id = open_plan(
        agent,
        music_asset_id=music_id,
        duration=5,
    )
    agent.confirm(task.task_id, int(agent.get_task(task.task_id).head_plan_version))
    for _ in range(12):
        step = read_rows(
            agent.home,
            "SELECT step FROM work_items WHERE task_id=? AND kind='generation' ORDER BY ready_at DESC LIMIT 1",
            (task.task_id,),
        )[0]["step"]
        if step == "export":
            break
        result = agent.tick()
        if not result.did_work:
            agent.clock.advance(1)
    assert submits["count"] == 1
    relative = read_rows(agent.home, "SELECT relative_path FROM assets WHERE asset_id=?", (music_id,))[0]["relative_path"]
    (agent.home / relative).unlink()
    finished = drain(agent, task.task_id)
    assert finished.status == "succeeded"
    assert finished.failure is None
    assert submits["count"] == 1

    bare = make_agent(tmp_path / "bare")
    bare_submits = {"count": 0}
    bare_original = bare.video.submit_shot

    def count_bare(request):
        bare_submits["count"] += 1
        return bare_original(request)

    bare.video.submit_shot = count_bare
    bare_task, bare_image = open_plan(bare, duration=5)
    relative = read_rows(bare.home, "SELECT relative_path FROM assets WHERE asset_id=?", (bare_image,))[0]["relative_path"]
    (bare.home / relative).unlink()
    bare.confirm(bare_task.task_id, int(bare.get_task(bare_task.task_id).head_plan_version))
    missing = drain(bare, bare_task.task_id)
    assert missing.failure is not None
    assert missing.failure.code == "ASSET_MISSING"
    assert bare_submits["count"] == 0
