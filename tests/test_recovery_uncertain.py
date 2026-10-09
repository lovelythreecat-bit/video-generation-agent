import json
from pathlib import Path

import pytest
from PIL import Image

from tests.recovery_support import (
    capabilities,
    make_agent,
    open_plan,
    read_rows,
    require_ffmpeg,
    set_voice_marker,
)
from video_agent.config import AdapterConfig
from video_agent.errors import VideoAgentError
from video_agent.media import still_clip
from video_agent.ports import QueryResult, SubmitResult


def _marker(payload: dict) -> str:
    return payload["adapters"]["voice"]["marker"]


def _generation_id(agent, task_id: str) -> str:
    return read_rows(agent.home, "SELECT generation_id FROM tasks WHERE task_id=?", (task_id,))[0]["generation_id"]


def _config_row(agent, task_id: str, column: str) -> dict:
    if column == "work":
        sql = "SELECT copied_config_json FROM work_items WHERE task_id=? ORDER BY ready_at DESC LIMIT 1"
    else:
        sql = "SELECT copied_config_json FROM generations WHERE task_id=? ORDER BY created_at DESC LIMIT 1"
    raw = read_rows(agent.home, sql, (task_id,))[0]["copied_config_json"]
    return json.loads(raw)


class UncertainThenAbsent:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.keys: list[str] = []

    def capabilities(self):
        return capabilities()

    def submit_shot(self, request):
        self.events.append("submit")
        self.keys.append(request.provider_idempotency_key)
        return SubmitResult(external_task_id=None, uncertain=True)

    def query(self, *, external_task_id, provider_idempotency_key):
        del external_task_id
        self.events.append("query")
        self.keys.append(provider_idempotency_key)
        return QueryResult(state="absent")

    def download(self, external_task_id: str) -> bytes:
        del external_task_id
        raise AssertionError("absent 之前不能下载")


def test_uncertain_submit_queries_and_only_then_submits(tmp_path: Path):
    agent = make_agent(tmp_path)
    video = UncertainThenAbsent()
    agent.video = video
    task, _image = open_plan(agent)
    agent.confirm(task.task_id, int(agent.get_task(task.task_id).head_plan_version))
    assert agent.tick().step == "first_frame"
    assert agent.tick().step == "submit_shot"
    view = agent.get_task(task.task_id)
    assert view.status == "generating"
    assert video.events == ["submit"]
    shot = read_rows(
        agent.home,
        "SELECT submission_state, external_task_id FROM generation_shots WHERE generation_id=?",
        (view.generation_id,),
    )[0]
    assert shot["submission_state"] == "uncertain"
    assert shot["external_task_id"] is None
    used = read_rows(agent.home, "SELECT auto_attempts_used FROM generations WHERE generation_id=?", (view.generation_id,))[0]
    assert used["auto_attempts_used"] == 0
    assert agent.tick().did_work is True
    assert video.events == ["submit", "query", "submit"]
    assert video.keys[0] == video.keys[2]


class PendingVideo:
    def __init__(self) -> None:
        self.submits = 0
        self.queries = 0

    def capabilities(self):
        return capabilities()

    def submit_shot(self, request):
        del request
        self.submits += 1
        return SubmitResult(external_task_id="job-1", uncertain=False)

    def query(self, *, external_task_id, provider_idempotency_key):
        del external_task_id, provider_idempotency_key
        self.queries += 1
        return QueryResult(state="pending")

    def download(self, external_task_id: str) -> bytes:
        del external_task_id
        raise AssertionError("pending 期间不能下载")


def test_pending_query_waits_one_second_without_holding_the_lease(tmp_path: Path):
    agent = make_agent(tmp_path)
    assert agent.config.lease_seconds == 60
    video = PendingVideo()
    agent.video = video
    task, _image = open_plan(agent)
    agent.confirm(task.task_id, int(agent.get_task(task.task_id).head_plan_version))
    agent.tick()
    agent.tick()
    agent.tick()
    assert video.submits == 1
    assert video.queries == 1
    assert agent.get_task(task.task_id).status == "generating"
    assert agent.tick().did_work is False
    agent.clock.advance(1)
    assert agent.tick().did_work is True
    assert video.queries == 2
    assert video.submits == 1


class DownloadOnce:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.submits = 0
        self.queries = 0
        self.downloads = 0
        self.request = None

    def capabilities(self):
        return capabilities()

    def submit_shot(self, request):
        self.submits += 1
        self.request = request
        return SubmitResult(external_task_id="job-1", uncertain=False)

    def query(self, *, external_task_id, provider_idempotency_key):
        del external_task_id, provider_idempotency_key
        self.queries += 1
        return QueryResult(state="succeeded")

    def download(self, external_task_id: str) -> bytes:
        del external_task_id
        self.downloads += 1
        if self.downloads == 1:
            raise VideoAgentError(
                "PROVIDER_TEMPORARY",
                "下载中断",
                stage="generating_shots",
                retryable=True,
                details={"retry_after_seconds": 1},
            )
        request = self.request
        image = self.directory / "frame.png"
        Image.new("RGB", (request.width, request.height), (30, 30, 30)).save(image)
        dest = self.directory / "clip.mp4"
        still_clip(image, request.frame_count, request.width, request.height, dest, video_bitrate=8_000_000)
        return dest.read_bytes()


def test_completed_download_retries_download_only(tmp_path: Path):
    require_ffmpeg()
    agent = make_agent(tmp_path)
    video = DownloadOnce(tmp_path)
    agent.video = video
    task, _image = open_plan(agent)
    agent.confirm(task.task_id, int(agent.get_task(task.task_id).head_plan_version))
    for _ in range(6):
        if video.downloads == 1:
            break
        result = agent.tick()
        if not result.did_work:
            agent.clock.advance(1)
    assert video.submits == 1
    assert video.queries == 1
    assert video.downloads == 1
    assert agent.get_task(task.task_id).status == "generating"
    assert agent.tick().did_work is False
    agent.clock.advance(1)
    agent.tick()
    assert video.submits == 1
    assert video.queries == 1
    assert video.downloads == 2


class RejectOnce:
    def __init__(self) -> None:
        self.submits = 0

    def capabilities(self):
        return capabilities()

    def submit_shot(self, request):
        del request
        self.submits += 1
        if self.submits == 1:
            raise VideoAgentError("PROVIDER_REJECTED", "拒绝", stage="generating_shots", retryable=True)
        return SubmitResult(external_task_id="job-2", uncertain=False)

    def query(self, *, external_task_id, provider_idempotency_key):
        del external_task_id, provider_idempotency_key
        return QueryResult(state="absent")

    def download(self, external_task_id: str) -> bytes:
        del external_task_id
        return b""


def test_retry_without_external_id_uses_the_new_saved_config(tmp_path: Path):
    agent = make_agent(tmp_path)
    agent.video = RejectOnce()
    task, _image = open_plan(agent)
    set_voice_marker(agent, "old")
    agent.confirm(task.task_id, int(agent.get_task(task.task_id).head_plan_version))
    agent.tick()
    agent.tick()
    failed = agent.get_task(task.task_id)
    assert failed.status == "failed"
    assert failed.failure.code == "PROVIDER_REJECTED"
    set_voice_marker(agent, "new")
    assert agent.config.adapters["voice"].model_extra["marker"] == "old"
    agent.retry_generation(failed.generation_id)
    agent.tick()
    assert _marker(_config_row(agent, task.task_id, "work")) == "new"
    assert _marker(_config_row(agent, task.task_id, "generation")) == "new"
    remembered = [item for item in agent.provider_config_calls if item["purpose"] == "submit"]
    assert _marker(remembered[-1]["config"]) == "new"


class RecordingDownload:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.request = None
        self.events: list[str] = []

    def capabilities(self):
        return capabilities()

    def submit_shot(self, request):
        self.request = request
        self.events.append("submit")
        return SubmitResult(external_task_id="job-1", uncertain=False)

    def query(self, *, external_task_id, provider_idempotency_key):
        del external_task_id, provider_idempotency_key
        self.events.append("query")
        return QueryResult(state="succeeded")

    def download(self, external_task_id: str) -> bytes:
        self.events.append("download")
        request = self.request
        image = self.directory / "frame.png"
        Image.new("RGB", (request.width, request.height), (40, 40, 40)).save(image)
        dest = self.directory / "clip.mp4"
        still_clip(image, request.frame_count, request.width, request.height, dest, video_bitrate=8_000_000)
        return dest.read_bytes()


def test_query_and_download_keep_the_config_copied_at_submit(tmp_path: Path):
    require_ffmpeg()
    agent = make_agent(tmp_path)
    video = RecordingDownload(tmp_path)
    agent.video = video
    task, _image = open_plan(agent)
    set_voice_marker(agent, "old")
    agent.confirm(task.task_id, int(agent.get_task(task.task_id).head_plan_version))
    for _ in range(4):
        external = read_rows(
            agent.home,
            """SELECT external_task_id FROM generation_shots
               WHERE generation_id=? AND external_task_id IS NOT NULL""",
            (_generation_id(agent, task.task_id),),
        )
        if external:
            break
        agent.tick()
    assert video.events == ["submit"]
    set_voice_marker(agent, "new")
    cloned = agent.config.model_copy(deep=True)
    cloned.adapters["voice"] = AdapterConfig.model_validate({"adapter": "fake", "marker": "memory"})
    agent.config = cloned
    agent.tick()
    assert "query" in video.events
    assert "download" in video.events
    assert _marker(_config_row(agent, task.task_id, "generation")) == "old"
    for purpose in ("query", "download"):
        seen = [item for item in agent.provider_config_calls if item["purpose"] == purpose]
        assert seen
        assert _marker(seen[-1]["config"]) == "old"
