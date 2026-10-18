import threading
from datetime import timedelta
from pathlib import Path

import pytest
from PIL import Image

from tests.recovery_support import capabilities, make_agent, open_plan, read_rows, require_ffmpeg
from video_agent.errors import VideoAgentError
from video_agent.lease import hold_lease, ManualSleeper
from video_agent.media import still_clip
from video_agent.ports import QueryResult, SubmitResult


class TemporaryVideo:
    def __init__(self) -> None:
        self.events: list[str] = []

    def capabilities(self):
        return capabilities()

    def submit_shot(self, request):
        self.events.append("submit")
        if self.events.count("submit") == 1:
            raise VideoAgentError(
                "PROVIDER_TEMPORARY",
                "稍后",
                stage="generating_shots",
                retryable=True,
                details={"retry_after_seconds": 5},
            )
        return SubmitResult(external_task_id="job-1", uncertain=False)

    def query(self, *, external_task_id, provider_idempotency_key):
        del external_task_id, provider_idempotency_key
        self.events.append("query")
        return QueryResult(state="absent")

    def download(self, external_task_id: str) -> bytes:
        del external_task_id
        raise AssertionError("这一测试停在再次提交")


def test_temporary_failure_backs_off_on_the_frozen_clock(tmp_path: Path):
    agent = make_agent(tmp_path)
    video = TemporaryVideo()
    agent.video = video
    task, _image = open_plan(agent)
    agent.confirm(task.task_id, int(agent.get_task(task.task_id).head_plan_version))
    assert agent.tick().step == "first_frame"
    assert agent.tick().step == "submit_shot"
    view = agent.get_task(task.task_id)
    assert view.status == "generating"
    assert view.stage == "generating_shots"
    row = read_rows(agent.home, "SELECT status, auto_attempts_used FROM generations WHERE generation_id=?", (view.generation_id,))[0]
    assert row["status"] == "running"
    assert row["auto_attempts_used"] == 1
    assert agent.tick().did_work is False
    agent.clock.advance(4)
    assert agent.tick().did_work is False
    assert video.events == ["submit"]
    agent.clock.advance(1)
    assert agent.tick().did_work is True
    assert video.events == ["submit", "query", "submit"]


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
        Image.new("RGB", (request.width, request.height), (20, 20, 20)).save(image)
        dest = self.directory / "half.mp4"
        still_clip(image, request.frame_count // 2, request.width, request.height, dest, video_bitrate=8_000_000)
        return dest.read_bytes()


def test_short_clip_fails_only_after_two_extra_attempts(tmp_path: Path):
    require_ffmpeg()
    agent = make_agent(tmp_path)
    video = HalfVideo(tmp_path)
    agent.video = video
    task, _image = open_plan(agent)
    confirmed = agent.confirm(task.task_id, int(agent.get_task(task.task_id).head_plan_version))
    generation_id = confirmed.generation_id

    def row():
        return read_rows(
            agent.home,
            "SELECT status, stage, auto_attempts_used, failure_code, failure_retryable FROM generations WHERE generation_id=?",
            (generation_id,),
        )[0]

    for _ in range(8):
        current = row()
        if current["auto_attempts_used"] == 1 and current["status"] == "running":
            break
        result = agent.tick()
        if not result.did_work:
            agent.clock.advance(1)
    current = row()
    assert current["status"] == "running"
    assert current["stage"] == "generating_shots"
    assert current["auto_attempts_used"] == 1
    assert agent.get_task(task.task_id).status == "generating"
    assert agent.tick().did_work is False
    agent.clock.advance(1)
    agent.tick()
    assert row()["auto_attempts_used"] == 2
    assert row()["status"] == "running"
    assert agent.tick().did_work is False
    agent.clock.advance(1)
    agent.tick()
    done = row()
    assert done["status"] == "failed"
    assert done["auto_attempts_used"] == 2
    assert done["failure_code"] == "SHOT_DURATION_SHORT"
    assert done["failure_retryable"] == 1
    assert video.submits == 1
    assert video.downloads == 3


class BlockingVideo:
    def __init__(self) -> None:
        self.gate = threading.Event()
        self.started = threading.Event()
        self.submits = 0

    def capabilities(self):
        return capabilities()

    def submit_shot(self, request):
        del request
        self.submits += 1
        self.started.set()
        self.gate.wait()
        return SubmitResult(external_task_id="job-1", uncertain=False)

    def query(self, *, external_task_id, provider_idempotency_key):
        del external_task_id, provider_idempotency_key
        return QueryResult(state="pending")

    def download(self, external_task_id: str) -> bytes:
        del external_task_id
        return b""


def test_live_lease_is_renewed_without_running_the_step_twice(tmp_path: Path):
    agent = make_agent(tmp_path)
    assert agent.config.lease_seconds == 60
    task, _image = open_plan(agent)
    video = BlockingVideo()
    sleeper = ManualSleeper()
    agent.video = video
    agent.clock.lease_sleeper = sleeper
    agent.confirm(task.task_id, int(agent.get_task(task.task_id).head_plan_version))
    assert agent.tick().step == "first_frame"
    worker = threading.Thread(target=agent.tick, daemon=True)
    worker.start()
    try:
        assert video.started.wait(timeout=5)
        assert sleeper.entered.wait(timeout=5)
        assert agent.recover() == 0
        assert agent.tick().did_work is False
        assert video.submits == 1
        assert sleeper.seconds[0] == 30
        agent.clock.advance(30)
        sleeper.renewed.clear()
        sleeper.release()
        assert sleeper.renewed.wait(timeout=5)
        until = read_rows(
            agent.home,
            "SELECT lease_until FROM work_items WHERE task_id=? AND kind='generation'",
            (task.task_id,),
        )[0]["lease_until"]
        assert until == (agent.clock.now() + timedelta(seconds=60)).isoformat()
        assert agent.recover() == 0
        assert agent.tick().did_work is False
        assert video.submits == 1
        video.gate.set()
        worker.join(timeout=5)
        assert video.submits == 1
    finally:
        video.gate.set()
        sleeper.release()


def test_lease_under_15_seconds_is_rejected(tmp_path: Path):
    agent = make_agent(tmp_path)
    with pytest.raises(VideoAgentError) as caught:
        with hold_lease(agent.db, "missing-work", "owner", agent.clock, 14):
            pass
    assert caught.value.code == "VALIDATION_ERROR"
    assert agent.config.lease_seconds >= 15
