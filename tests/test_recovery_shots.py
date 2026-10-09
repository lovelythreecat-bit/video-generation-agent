import pytest
from pathlib import Path

from PIL import Image

from tests.recovery_support import (
    CountingMusic,
    CountingVoice,
    capabilities,
    confirm_and_finish,
    digest,
    drain,
    generation_dir,
    make_agent,
    open_plan,
    read_rows,
    require_ffmpeg,
    revise_two_shots,
)
from video_agent.errors import VideoAgentError
from video_agent.media import extract_png, probe, still_clip
from video_agent.models import RegenerateRequest
from video_agent.ports import QueryResult, ShotRequest, SubmitResult


class ColoredVideo:
    def __init__(self) -> None:
        self.submits = 0
        self.requests: list[ShotRequest] = []
        self.colors: dict[str, tuple[int, int, int]] = {}

    def capabilities(self):
        return capabilities()

    def submit_shot(self, request: ShotRequest) -> SubmitResult:
        self.submits += 1
        color = ((40 * self.submits) % 256, (70 * self.submits) % 256, (110 * self.submits) % 256)
        self.colors[request.provider_idempotency_key] = color
        self.requests.append(request)
        return SubmitResult(external_task_id=request.provider_idempotency_key, uncertain=False)

    def query(self, *, external_task_id: str | None, provider_idempotency_key: str) -> QueryResult:
        del external_task_id, provider_idempotency_key
        return QueryResult(state="succeeded")

    def download(self, external_task_id: str) -> bytes:
        request = next(item for item in self.requests if item.provider_idempotency_key == external_task_id)
        image = request.first_frame_path.parent / f"solid-{external_task_id}.png"
        Image.new("RGB", (request.width, request.height), self.colors[external_task_id]).save(image, format="PNG")
        dest = request.first_frame_path.parent / f"solid-{external_task_id}.mp4"
        still_clip(image, request.frame_count, request.width, request.height, dest, video_bitrate=8_000_000)
        data = dest.read_bytes()
        dest.unlink()
        image.unlink()
        return data


def _two_shot_film(tmp_path: Path):
    require_ffmpeg()
    agent = make_agent(tmp_path)
    agent.video = ColoredVideo()
    agent.voice = CountingVoice(agent.voice)
    agent.music = CountingMusic(agent.music)
    task, _image = open_plan(agent)
    revise_two_shots(agent, task.task_id)
    finished = confirm_and_finish(agent, task.task_id)
    assert finished.status == "succeeded"
    return agent, task.task_id, finished


def _pixels(path: Path) -> bytes:
    return Image.open(path).convert("RGB").tobytes()


def test_shot_regenerate_reuses_audio_and_keeps_kind(tmp_path: Path):
    agent, task_id, finished = _two_shot_film(tmp_path)
    base = agent.list_artifacts(task_id)[0]
    base_dir = generation_dir(agent, task_id, finished.generation_id)
    voice_calls = agent.voice.calls
    music_calls = agent.music.calls
    agent.regenerate(
        task_id,
        RegenerateRequest(kind="shots", artifact_id=base.artifact_id, shot_ids=["s1", "s2"]),
    )
    again = drain(agent, task_id)
    view = agent.get_generation(again.generation_id)
    assert view.kind == "shots"
    assert set(view.shot_ids) == {"s1", "s2"}
    assert agent.voice.calls == voice_calls
    assert agent.music.calls == music_calls
    new_dir = generation_dir(agent, task_id, again.generation_id)
    assert digest(new_dir / "voice.m4a") == digest(base_dir / "voice.m4a")
    assert digest(new_dir / "music.m4a") == digest(base_dir / "music.m4a")
    assert digest(new_dir / "cues.json") == digest(base_dir / "cues.json")
    assert agent.artifact_path(base.artifact_id).is_file()
    assert len(agent.list_artifacts(task_id)) == 2


def test_replaced_left_neighbor_uses_new_end_frame(tmp_path: Path):
    agent, task_id, finished = _two_shot_film(tmp_path)
    base = agent.list_artifacts(task_id)[0]
    base_dir = generation_dir(agent, task_id, finished.generation_id)
    agent.video.requests.clear()
    agent.regenerate(
        task_id,
        RegenerateRequest(kind="shots", artifact_id=base.artifact_id, shot_ids=["s1", "s2"]),
    )
    drain(agent, task_id)
    second = next(item for item in agent.video.requests if item.shot_id == "s2")
    new_dir = generation_dir(agent, task_id, agent.get_task(task_id).generation_id)
    new_clip = new_dir / "shots" / "s1.mp4"
    base_clip = base_dir / "shots" / "s1.mp4"
    new_end = tmp_path / "new-end.png"
    base_end = tmp_path / "base-end.png"
    extract_png(new_clip, probe(new_clip).frame_count - 1, new_end)
    extract_png(base_clip, probe(base_clip).frame_count - 1, base_end)
    assert _pixels(new_end) != _pixels(base_end)
    assert second.first_frame_path is not None
    assert _pixels(second.first_frame_path) == _pixels(new_end)


def test_right_neighbor_outside_uses_base_start_frame(tmp_path: Path):
    agent, task_id, finished = _two_shot_film(tmp_path)
    base = agent.list_artifacts(task_id)[0]
    base_dir = generation_dir(agent, task_id, finished.generation_id)
    agent.video.requests.clear()
    agent.regenerate(task_id, RegenerateRequest(kind="shots", artifact_id=base.artifact_id, shot_ids=["s1"]))
    again = drain(agent, task_id)
    request = next(item for item in agent.video.requests if item.shot_id == "s1")
    expected = tmp_path / "base-s2-start.png"
    extract_png(base_dir / "shots" / "s2.mp4", 0, expected)
    assert request.tail_frame_path is not None
    assert _pixels(request.tail_frame_path) == _pixels(expected)
    new_dir = generation_dir(agent, task_id, again.generation_id)
    assert digest(new_dir / "shots" / "s2.mp4") == digest(base_dir / "shots" / "s2.mp4")
    assert digest(new_dir / "shots" / "s1.mp4") != digest(base_dir / "shots" / "s1.mp4")


def test_left_neighbor_outside_uses_base_end_frame(tmp_path: Path):
    agent, task_id, finished = _two_shot_film(tmp_path)
    base = agent.list_artifacts(task_id)[0]
    base_dir = generation_dir(agent, task_id, finished.generation_id)
    agent.video.requests.clear()
    agent.regenerate(task_id, RegenerateRequest(kind="shots", artifact_id=base.artifact_id, shot_ids=["s2"]))
    drain(agent, task_id)
    request = next(item for item in agent.video.requests if item.shot_id == "s2")
    expected = tmp_path / "base-s1-end.png"
    clip = base_dir / "shots" / "s1.mp4"
    extract_png(clip, probe(clip).frame_count - 1, expected)
    assert request.first_frame_path is not None
    assert _pixels(request.first_frame_path) == _pixels(expected)


def test_single_shot_does_not_use_neighbor_frames(tmp_path: Path):
    require_ffmpeg()
    agent = make_agent(tmp_path)
    agent.video = ColoredVideo()
    task, _image = open_plan(agent)
    finished = confirm_and_finish(agent, task.task_id)
    base = agent.list_artifacts(task.task_id)[0]
    agent.video.requests.clear()
    agent.regenerate(
        task.task_id,
        RegenerateRequest(kind="shots", artifact_id=base.artifact_id, shot_ids=["s1"]),
    )
    drain(agent, task.task_id)
    request = agent.video.requests[-1]
    assert request.tail_frame_path is None
    assert request.first_frame_path is not None
    assert "frames" not in request.first_frame_path.as_posix()
    assert all("frames" not in path.as_posix() for path in request.reference_paths)


def test_unknown_shot_does_not_create_generation(tmp_path: Path):
    agent, task_id, _finished = _two_shot_film(tmp_path)
    before = read_rows(agent.home, "SELECT generation_id FROM generations WHERE task_id=?", (task_id,))
    base = agent.list_artifacts(task_id)[0]
    with pytest.raises(VideoAgentError) as caught:
        agent.regenerate(task_id, RegenerateRequest(kind="shots", artifact_id=base.artifact_id, shot_ids=["missing"]))
    assert caught.value.code == "VALIDATION_ERROR"
    after = read_rows(agent.home, "SELECT generation_id FROM generations WHERE task_id=?", (task_id,))
    assert [row["generation_id"] for row in after] == [row["generation_id"] for row in before]
