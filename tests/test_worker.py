import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from video_agent.clock import FrozenClock
from video_agent.errors import VideoAgentError
from video_agent.lease import hold_lease
from video_agent.models import CreateTaskRequest, PlanChange, PlanDocument, PlanPatch, ProductInfo, SharedVisual, ShotPlan
from video_agent.ports import PlanRequest, PlanResult, ReviseResult
from video_agent.service import VideoAgent
from video_agent.state import publish_document


def shot_document(duration: int = 10) -> PlanDocument:
    return PlanDocument(
        creative="创意",
        audience="买家",
        selling_points=["轻便"],
        shared_visual=SharedVisual(product_reference="红瓶", scene="桌面", lighting="日光", palette="暖", style="写实"),
        shots=[ShotPlan(shot_id="s1", content="展示", action="转", shot_size="中景", camera="固定", scene_id="main", start_frame=0, end_frame=duration * 30, subtitle_text="卖点", voiceover_text="卖点")],
        music_direction="轻快",
        duration_seconds=duration,
        aspect_ratio="9:16",
        fit="native",
        subtitles=True,
        voiceover=True,
        music=True,
        first_frame_source="generate",
        model_capabilities="单段",
    )


class ScriptedPlanner:
    def __init__(self, result=None, error=None, revise_result=None):
        self.result = result
        self.error = error
        self.revise_result = revise_result
        self.requests: list[PlanRequest] = []

    def capabilities_note(self) -> str:
        return "scripted"

    def plan(self, request: PlanRequest) -> PlanResult:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return self.result

    def revise(self, current: PlanDocument, instruction: str, request: PlanRequest) -> ReviseResult:
        if self.error is not None:
            raise self.error
        return self.revise_result


def make(home: Path, planner: ScriptedPlanner) -> tuple[VideoAgent, FrozenClock]:
    clock = FrozenClock(datetime(2026, 10, 9, 3, 0, tzinfo=timezone.utc))
    return VideoAgent(home, clock=clock, planner=planner), clock


def _png() -> bytes:
    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (2, 2), (9, 8, 7)).save(buffer, format="PNG")
    return buffer.getvalue()


class TemporaryThenReady(ScriptedPlanner):
    def __init__(self) -> None:
        super().__init__(result=PlanResult(document=shot_document()))
        self.failures = 0

    def plan(self, request: PlanRequest) -> PlanResult:
        self.requests.append(request)
        self.failures += 1
        if self.failures <= 2:
            raise VideoAgentError("PROVIDER_TEMPORARY", "稍后", details={"retry_after_seconds": 2})
        return self.result


class AlwaysTemporary(ScriptedPlanner):
    def plan(self, request: PlanRequest) -> PlanResult:
        self.requests.append(request)
        raise VideoAgentError("PROVIDER_TEMPORARY", "稍后", details={"retry_after_seconds": 1})


def test_tick_publishes_a_valid_plan_without_importing_pipeline(tmp_path: Path):
    planner = ScriptedPlanner(result=PlanResult(document=shot_document()))
    agent, clock = make(tmp_path, planner)
    assert agent.tick().did_work is False
    assert agent.recover() == 0
    image_id = agent.register_asset(filename="a.png", kind="image", data=_png())
    created = agent.create_task(CreateTaskRequest(product_info=ProductInfo(name="瓶子"), images=[image_id]))
    started = agent.tick()
    assert started.did_work is True
    assert started.step == "start"
    assert started.task_id == created.task_id
    assert agent.get_task(created.task_id).status == "planning"
    finished = agent.tick()
    assert finished.step == "finish"
    view = agent.get_task(created.task_id)
    assert view.status == "awaiting_confirmation"
    assert view.head_plan_version == 1
    assert view.stage is None
    assert planner.requests[0].video_capabilities.native_aspects == ["9:16"]
    assert "video_agent.pipeline" not in sys.modules


def test_planner_questions_and_invalid_frames_do_not_publish(tmp_path: Path):
    asking = ScriptedPlanner(result=PlanResult(questions=["看不清产品外观"]))
    agent, _clock = make(tmp_path / "ask", asking)
    image_id = agent.register_asset(filename="a.png", kind="image", data=_png())
    created = agent.create_task(CreateTaskRequest(images=[image_id]))
    agent.tick()
    agent.tick()
    view = agent.get_task(created.task_id)
    assert view.status == "awaiting_input"
    assert view.questions == ["看不清产品外观"]
    with agent.db.transaction() as conn:
        assert conn.execute("SELECT COUNT(*) AS c FROM plan_versions").fetchone()["c"] == 0
    bad = shot_document()
    bad.shots[0].end_frame = 10
    rejecting = ScriptedPlanner(result=PlanResult(document=bad))
    other, _clock = make(tmp_path / "bad", rejecting)
    image_id = other.register_asset(filename="a.png", kind="image", data=_png())
    failed = other.create_task(CreateTaskRequest(images=[image_id]))
    other.tick()
    other.tick()
    failed_view = other.get_task(failed.task_id)
    assert failed_view.status == "failed"
    assert failed_view.failure_scope == "planning"
    assert failed_view.failure is not None
    assert failed_view.failure.code == "PROVIDER_REJECTED"
    with other.db.transaction() as conn:
        assert conn.execute("SELECT COUNT(*) AS c FROM plan_versions").fetchone()["c"] == 0


def test_temporary_planning_failures_stay_in_planning_until_retries_are_spent(tmp_path: Path):
    agent, clock = make(tmp_path / "flaky", TemporaryThenReady())
    image_id = agent.register_asset(filename="a.png", kind="image", data=_png())
    created = agent.create_task(CreateTaskRequest(images=[image_id]))
    agent.tick()
    agent.tick()
    assert agent.get_task(created.task_id).status == "planning"
    assert agent.tick().did_work is False
    clock.advance(2)
    agent.tick()
    assert agent.get_task(created.task_id).status == "planning"
    clock.advance(2)
    agent.tick()
    assert agent.get_task(created.task_id).status == "awaiting_confirmation"
    exhausted, clock = make(tmp_path / "spent", AlwaysTemporary())
    image_id = exhausted.register_asset(filename="a.png", kind="image", data=_png())
    failed = exhausted.create_task(CreateTaskRequest(images=[image_id]))
    exhausted.tick()
    exhausted.tick()
    assert exhausted.get_task(failed.task_id).status == "planning"
    clock.advance(1)
    exhausted.tick()
    clock.advance(1)
    exhausted.tick()
    done = exhausted.get_task(failed.task_id)
    assert done.status == "failed"
    assert done.failure is not None
    assert done.failure.code == "PROVIDER_TEMPORARY"


def test_natural_language_result_publishes_or_records_failure(tmp_path: Path):
    planner = ScriptedPlanner(revise_result=ReviseResult(patch=PlanPatch(duration_seconds=5)))
    agent, clock = make(tmp_path / "nl", planner)
    created = agent.create_task(CreateTaskRequest())
    with agent.db.transaction() as conn:
        publish_document(conn, task_id=created.task_id, document=shot_document(), now=clock.now().isoformat())
    agent.revise_plan(created.task_id, PlanChange(instruction="改成 5 秒"))
    agent.tick()
    agent.tick()
    view = agent.get_task(created.task_id)
    assert view.status == "awaiting_confirmation"
    assert view.head_plan_version == 2
    with agent.db.transaction() as conn:
        stored = PlanDocument.model_validate_json(
            conn.execute("SELECT document_json FROM plan_versions WHERE task_id=? AND plan_version=2", (created.task_id,)).fetchone()["document_json"]
        )
        instruction = conn.execute("SELECT pending_instruction FROM tasks WHERE task_id=?", (created.task_id,)).fetchone()["pending_instruction"]
    assert stored.duration_seconds == 5
    assert instruction is None
    asking = ScriptedPlanner(revise_result=ReviseResult(questions=["缺语言"]))
    asked, asked_clock = make(tmp_path / "ask-nl", asking)
    task_id = asked.create_task(CreateTaskRequest()).task_id
    with asked.db.transaction() as conn:
        publish_document(conn, task_id=task_id, document=shot_document(), now=asked_clock.now().isoformat())
    asked.revise_plan(task_id, PlanChange(instruction="换语言"))
    asked.tick()
    asked.tick()
    assert asked.get_task(task_id).questions == ["缺语言"]
    with asked.db.transaction() as conn:
        assert conn.execute("SELECT COUNT(*) AS c FROM plan_versions WHERE task_id=?", (task_id,)).fetchone()["c"] == 1
    invalid = ScriptedPlanner(revise_result=ReviseResult(patch=PlanPatch(duration_seconds=99)))
    broken, broken_clock = make(tmp_path / "bad-nl", invalid)
    task_id = broken.create_task(CreateTaskRequest()).task_id
    with broken.db.transaction() as conn:
        publish_document(conn, task_id=task_id, document=shot_document(), now=broken_clock.now().isoformat())
    broken.revise_plan(task_id, PlanChange(instruction="改成 99 秒"))
    broken.tick()
    broken.tick()
    failed = broken.get_task(task_id)
    assert failed.status == "failed"
    assert failed.failure is not None
    assert failed.failure.code == "PROVIDER_REJECTED"
    assert failed.head_plan_version == 1


def test_lease_and_claim_order(tmp_path: Path):
    planner = ScriptedPlanner(result=PlanResult(document=shot_document()))
    agent, clock = make(tmp_path, planner)
    image_id = agent.register_asset(filename="a.png", kind="image", data=_png())
    first = agent.create_task(CreateTaskRequest(images=[image_id], product_info=ProductInfo(name="先")))
    with agent.db.transaction() as conn:
        work_id = conn.execute("SELECT work_id FROM work_items WHERE task_id=?", (first.task_id,)).fetchone()["work_id"]
    with hold_lease(agent.db, work_id, agent.worker_owner, clock, 15):
        with agent.db.transaction() as conn:
            row = conn.execute("SELECT lease_owner, lease_until FROM work_items WHERE work_id=?", (work_id,)).fetchone()
        assert row["lease_owner"] == agent.worker_owner
        assert row["lease_until"] is not None
    future = (clock.now()).isoformat()
    with agent.db.transaction() as conn:
        conn.execute(
            "UPDATE work_items SET lease_owner='other', lease_until=? WHERE work_id=?",
            ("2026-10-09T03:00:30+00:00", work_id),
        )
    assert agent.recover() == 0
    clock.advance(31)
    assert agent.recover() == 1
    with agent.db.transaction() as conn:
        row = conn.execute("SELECT lease_owner, lease_until, step FROM work_items WHERE work_id=?", (work_id,)).fetchone()
    assert row["lease_owner"] is None
    assert row["lease_until"] is None
    assert row["step"] == "start"
    assert future.startswith("2026-10-09T03:00:00")
    blocked, blocked_clock = make(tmp_path / "order", ScriptedPlanner(result=PlanResult(document=shot_document())))
    image_id = blocked.register_asset(filename="a.png", kind="image", data=_png())
    earlier = blocked.create_task(CreateTaskRequest(images=[image_id], product_info=ProductInfo(name="早")))
    blocked_clock.advance(1)
    later = blocked.create_task(CreateTaskRequest(images=[image_id], product_info=ProductInfo(name="晚")))
    with blocked.db.transaction() as conn:
        early_work = conn.execute("SELECT work_id FROM work_items WHERE task_id=?", (earlier.task_id,)).fetchone()["work_id"]
        conn.execute(
            "UPDATE work_items SET lease_owner='other', lease_until='2026-10-09T04:00:00+00:00' WHERE work_id=?",
            (early_work,),
        )
    assert blocked.tick().task_id == later.task_id
    blocked_clock.advance(3600)
    assert blocked.tick().task_id == earlier.task_id
