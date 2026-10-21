import array
import subprocess
import tempfile
from pathlib import Path

import pytest

from tests.recovery_support import (
    drain, generation_dir, make_agent, open_plan, require_ffmpeg, revise_two_shots,
)
from video_agent.fake_video import FakeVideo
from video_agent.media import _run_ffmpeg, probe
from video_agent.models import PlanChange, PlanPatch, RegenerateRequest


class UnusedServices:
    def synthesize(self, **kwargs):
        raise AssertionError("independent voice must not run")

    def generate(self, **kwargs):
        raise AssertionError("independent music must not run")

    def check(self, *args):
        raise AssertionError("visual checks must not run")


class SoundtrackVideo(FakeVideo):
    def __init__(self, audio_shots):
        super().__init__()
        self.audio_shots = audio_shots

    def download(self, external_task_id):
        data = super().download(external_task_id)
        request = self._by_external[external_task_id]
        if request.shot_id not in self.audio_shots:
            return data
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "picture.mp4"
            dest = Path(directory) / "with-audio.mp4"
            source.write_bytes(data)
            _run_ffmpeg([
                "-i", str(source), "-f", "lavfi", "-i",
                f"sine=frequency=440:sample_rate=48000:duration={request.frame_count / 30}",
                "-c:v", "copy", "-c:a", "aac", "-ac", "2", "-shortest", str(dest),
            ])
            return dest.read_bytes()


def audio_level(path, start):
    result = subprocess.run([
        "ffmpeg", "-v", "error", "-ss", str(start), "-i", str(path),
        "-t", "0.25", "-f", "s16le", "-ac", "1", "-ar", "8000", "pipe:1",
    ], check=True, capture_output=True, stdin=subprocess.DEVNULL)
    samples = array.array("h", result.stdout)
    assert samples
    return sum(abs(sample) for sample in samples) / len(samples)


def test_export_accepts_provider_clips_with_different_pixel_aspects(tmp_path):
    require_ffmpeg()
    from video_agent.media import export_clips

    clips = []
    for index, sar in enumerate(("1/1", "99/100")):
        clip = tmp_path / f"shot-{index}.mp4"
        _run_ffmpeg([
            "-f", "lavfi", "-i", "color=red:s=180x320:r=30:d=0.1",
            "-vf", f"setsar={sar}", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(clip),
        ])
        clips.append(clip)
    final = tmp_path / "final.mp4"
    export_clips(clips, final, keep_audio=False, video_bitrate=500000, audio_bitrate=192000)
    info = probe(final)
    assert (info.width, info.height, info.frame_count) == (180, 320, 6)


class OverlongSoundtrackVideo(SoundtrackVideo):
    def download(self, external_task_id):
        original = self._by_external[external_task_id]
        self._by_external[external_task_id] = original.model_copy(update={"frame_count": original.frame_count + 30})
        try:
            return super().download(external_task_id)
        finally:
            self._by_external[external_task_id] = original


def test_overlong_model_audio_clip_is_trimmed_and_exported(tmp_path):
    require_ffmpeg()
    agent = make_agent(tmp_path)
    agent.video = OverlongSoundtrackVideo({"s1"})
    agent.voice = agent.music = UnusedServices()
    task, _ = open_plan(agent)
    agent.confirm(task.task_id, task.head_plan_version)
    finished = drain(agent, task.task_id)
    assert finished.status == "succeeded", finished.failure
    artifact = agent.list_artifacts(task.task_id)[0]
    info = probe(agent.artifact_path(artifact.artifact_id))
    assert info.frame_count == 150
    assert info.has_audio is True


def test_generation_exports_without_audio_services_or_visual_checks(tmp_path):
    require_ffmpeg()
    agent = make_agent(tmp_path)
    agent.voice = agent.music = agent.visual = UnusedServices()
    task, _ = open_plan(agent)
    agent.confirm(task.task_id, task.head_plan_version)
    stages = []
    for _ in range(20):
        view = agent.get_task(task.task_id)
        stages.append(view.stage)
        if view.status != "generating":
            break
        agent.tick()
    assert view.status == "succeeded", view.failure
    assert not {"preparing_audio", "composing", "checking_visuals"}.intersection(stages)
    artifact = agent.list_artifacts(task.task_id)[0]
    assert artifact.quality_status == "not_assessed"
    assert artifact.visual_check_enabled is False
    assert probe(agent.artifact_path(artifact.artifact_id)).has_audio is False
    root = generation_dir(agent, task.task_id, view.generation_id)
    for name in ("voice.m4a", "music.m4a", "cues.json", "frames", "quality.json", "composed.mp4"):
        assert not (root / name).exists()


@pytest.mark.parametrize("step", ["audio", "compose", "visual", "export"])
def test_old_stage_resumes_from_shots_and_ignores_old_sidecars(tmp_path, step):
    require_ffmpeg()
    agent = make_agent(tmp_path)
    task, _ = open_plan(agent)
    generation = agent.confirm(task.task_id, task.head_plan_version)
    for _ in range(12):
        with agent.db.transaction(write=False) as conn:
            row = conn.execute("SELECT step FROM work_items WHERE generation_id=:id", {"id": generation.generation_id}).fetchone()
        if row["step"] in {"audio", "export"}:
            break
        agent.tick()
    root = generation_dir(agent, task.task_id, generation.generation_id)
    for name in ("composed.mp4", "voice.m4a", "music.m4a", "cues.json"):
        (root / name).write_bytes(b"interrupted legacy media")
    (root / "quality.json").write_text('{"status":"flagged","findings":[],"reason":"old report"}')
    with agent.db.transaction() as conn:
        conn.execute("UPDATE work_items SET step=:step WHERE generation_id=:id", {"step": step, "id": generation.generation_id})
    agent.voice = agent.music = agent.visual = UnusedServices()
    finished = drain(agent, task.task_id)
    assert finished.status == "succeeded", finished.failure
    artifact = agent.list_artifacts(task.task_id)[0]
    assert artifact.quality_status == "not_assessed"
    assert artifact.visual_check_enabled is False
    assert artifact.quality["reason"] != "old report"
    assert probe(agent.artifact_path(artifact.artifact_id)).frame_count == 150


@pytest.mark.parametrize("audio_shots,enabled", [
    (set(), True), ({"s1", "s2"}, True), ({"s1"}, True), ({"s2"}, True), ({"s1", "s2"}, False),
])
def test_direct_export_preserves_available_model_audio(tmp_path, audio_shots, enabled):
    require_ffmpeg()
    agent = make_agent(tmp_path)
    agent.video = SoundtrackVideo(audio_shots)
    agent.voice = agent.music = UnusedServices()
    task, _ = open_plan(agent)
    revise_two_shots(agent, task.task_id)
    if not enabled:
        agent.revise_plan(task.task_id, PlanChange(patch=PlanPatch(voiceover=False, music=False)))
    agent.confirm(task.task_id, agent.get_task(task.task_id).head_plan_version)
    finished = drain(agent, task.task_id)
    assert finished.status == "succeeded", finished.failure
    base = agent.list_artifacts(task.task_id)[0]
    info = probe(agent.artifact_path(base.artifact_id))
    assert info.frame_count == 150
    assert info.has_audio is bool(audio_shots and enabled)
    if info.has_audio:
        assert info.audio_codec == "aac"
        final = agent.artifact_path(base.artifact_id)
        for shot_id, start in (("s1", 0.5), ("s2", 3.0)):
            level = audio_level(final, start)
            assert level > 100 if shot_id in audio_shots else level < 10
    # Partial regeneration works without independent audio files in the base.
    agent.regenerate(task.task_id, RegenerateRequest(kind="shots", artifact_id=base.artifact_id, shot_ids=["s1"]))
    again = drain(agent, task.task_id)
    assert again.status == "succeeded", again.failure
    info = probe(agent.artifact_path(agent.list_artifacts(task.task_id)[0].artifact_id))
    assert info.frame_count == 150
    assert info.has_audio is bool(audio_shots and enabled)
