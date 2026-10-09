import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from video_agent.clock import FrozenClock
from video_agent.errors import VideoAgentError
from video_agent.models import (
    CreateTaskRequest,
    FailureView,
    PlanChange,
    PlanDocument,
    PlanPatch,
    RegenerateRequest,
    SharedVisual,
    ShotPlan,
    ShotTextPatch,
)
from video_agent.service import VideoAgent
from video_agent.state import mark_generation_failed, mark_generation_succeeded, publish_document, record_artifact


class IdlePlanner:
    def capabilities_note(self) -> str:
        return "idle"

    def plan(self, request):
        raise AssertionError("planner is not used")

    def revise(self, current, instruction, request):
        raise AssertionError("planner is not used")


def document() -> PlanDocument:
    return PlanDocument(
        creative="已确认创意",
        audience="买家",
        selling_points=["轻便"],
        shared_visual=SharedVisual(product_reference="红瓶", scene="桌面", lighting="日光", palette="暖", style="写实"),
        shots=[
            ShotPlan(shot_id="s1", content="展示", action="转", shot_size="中景", camera="固定", scene_id="main", start_frame=0, end_frame=150, subtitle_text="卖点", voiceover_text="卖点"),
            ShotPlan(shot_id="s2", content="细节", action="推", shot_size="特写", camera="推进", scene_id="main", start_frame=150, end_frame=300, subtitle_text="卖点", voiceover_text="卖点"),
        ],
        music_direction="轻快",
        duration_seconds=10,
        aspect_ratio="9:16",
        fit="native",
        subtitles=True,
        voiceover=True,
        music=True,
        first_frame_source="generate",
        model_capabilities="单段 30 秒",
    )


def agent_with_plan(home: Path) -> tuple[VideoAgent, str]:
    clock = FrozenClock(datetime(2026, 10, 9, 3, 0, tzinfo=timezone.utc))
    agent = VideoAgent(home, clock=clock, planner=IdlePlanner())
    created = agent.create_task(CreateTaskRequest())
    with agent.db.transaction() as conn:
        publish_document(conn, task_id=created.task_id, document=document(), now=clock.now().isoformat())
    return agent, created.task_id


def test_confirm_writes_one_full_generation_and_replays_it(tmp_path: Path):
    agent, task_id = agent_with_plan(tmp_path)
    view = agent.confirm(task_id, 1, idempotency_key="confirm-1")
    assert view.status == "generating"
    assert view.stage == "queued"
    assert view.confirmed_plan_version == 1
    assert view.completed_shots == 0
    assert view.total_shots == 2
    with agent.db.transaction() as conn:
        generation = conn.execute("SELECT * FROM generations WHERE generation_id=?", (view.generation_id,)).fetchone()
        shots = conn.execute("SELECT shot_id, status FROM generation_shots WHERE generation_id=? ORDER BY ordinal", (view.generation_id,)).fetchall()
        work = conn.execute("SELECT kind, step, generation_id FROM work_items WHERE generation_id=?", (view.generation_id,)).fetchone()
    assert generation["kind"] == "full"
    assert generation["status"] == "queued"
    assert generation["stage"] == "queued"
    assert json.loads(generation["shot_ids_json"]) == ["s1", "s2"]
    assert [row["shot_id"] for row in shots] == ["s1", "s2"]
    assert {row["status"] for row in shots} == {"pending"}
    assert work["kind"] == "generation"
    assert work["step"] == "first_frame"
    assert list(tmp_path.rglob("*.mp4")) == []
    replay = agent.confirm(task_id, 1, idempotency_key="confirm-1")
    assert replay.model_dump() == view.model_dump()
    with pytest.raises(VideoAgentError) as again:
        agent.confirm(task_id, 1)
    assert again.value.code == "STATE_CONFLICT"
    assert again.value.generation_id == view.generation_id
    with agent.db.transaction() as conn:
        mark_generation_succeeded(conn, view.generation_id, agent.clock.now().isoformat())
    assert agent.get_task(task_id).status == "succeeded"
    with pytest.raises(VideoAgentError) as after_success:
        agent.confirm(task_id, 1)
    assert after_success.value.code == "STATE_CONFLICT"
    assert after_success.value.generation_id == view.generation_id
    assert agent.confirm(task_id, 1, idempotency_key="confirm-1").status == "generating"


def test_confirm_rejects_old_abandoned_and_unknown_versions(tmp_path: Path):
    agent, task_id = agent_with_plan(tmp_path)
    agent.revise_plan(task_id, PlanChange(patch=PlanPatch(shot_texts=[ShotTextPatch(shot_id="s1", content="新文案")])))
    with pytest.raises(VideoAgentError) as old:
        agent.confirm(task_id, 1)
    assert old.value.code == "PLAN_VERSION_CONFLICT"
    assert old.value.details["head_plan_version"] == 2
    confirmed = agent.confirm(task_id, 2)
    with agent.db.transaction() as conn:
        mark_generation_succeeded(conn, confirmed.generation_id, agent.clock.now().isoformat())
    agent.revise_plan(task_id, PlanChange(patch=PlanPatch(duration_seconds=15)))
    agent.discard_revision(task_id)
    with pytest.raises(VideoAgentError) as abandoned:
        agent.confirm(task_id, 3)
    assert abandoned.value.code == "PLAN_VERSION_CONFLICT"
    assert abandoned.value.details["head_plan_version"] == 2
    with pytest.raises(VideoAgentError) as missing:
        agent.confirm(task_id, 99)
    assert missing.value.code == "PLAN_VERSION_CONFLICT"


def test_regenerate_full_uses_confirmed_plan_and_returns_to_draft(tmp_path: Path):
    agent, task_id = agent_with_plan(tmp_path)
    confirmed = agent.confirm(task_id, 1)
    with agent.db.transaction() as conn:
        mark_generation_succeeded(conn, confirmed.generation_id, agent.clock.now().isoformat())
    agent.revise_plan(task_id, PlanChange(patch=PlanPatch(duration_seconds=15)))
    with pytest.raises(VideoAgentError) as old_version:
        agent.confirm(task_id, 1)
    assert old_version.value.code == "PLAN_VERSION_CONFLICT"
    running = agent.regenerate(task_id, RegenerateRequest(kind="full"))
    with pytest.raises(VideoAgentError) as blocked:
        agent.revise_plan(task_id, PlanChange(instruction="等执行结束"))
    assert blocked.value.code == "STATE_CONFLICT"
    assert running.status == "generating"
    with agent.db.transaction() as conn:
        row = conn.execute(
            "SELECT plan_version, kind, snapshot_json FROM generations WHERE generation_id=?",
            (running.generation_id,),
        ).fetchone()
    snapshot = PlanDocument.model_validate_json(row["snapshot_json"])
    assert row["plan_version"] == 1
    assert row["kind"] == "full"
    assert snapshot.duration_seconds == 10
    with agent.db.transaction() as conn:
        record_artifact(
            conn,
            task_id=task_id,
            generation_id=running.generation_id,
            plan_version=1,
            duration_seconds=10,
            aspect_ratio="9:16",
            width=1080,
            height=1920,
            relative_path=f"tasks/{task_id}/generations/{running.generation_id}/final.mp4",
            quality_status="not_assessed",
            quality={"reason": "not run"},
            now=agent.clock.now().isoformat(),
        )
        mark_generation_succeeded(conn, running.generation_id, agent.clock.now().isoformat())
    restored = agent.get_task(task_id)
    assert restored.status == "awaiting_confirmation"
    assert restored.head_plan_version == 2
    assert restored.confirmed_plan_version == 1
    with agent.db.transaction() as conn:
        mark_generation_failed(
            conn,
            running.generation_id,
            FailureView(code="PROVIDER_TEMPORARY", message="临时失败", stage="generating_shots", retryable=True),
            agent.clock.now().isoformat(),
        )
    failed = agent.get_task(task_id)
    assert failed.status == "failed"
    assert failed.failure_scope == "generation"
    assert failed.failure is not None
    assert failed.failure.code == "PROVIDER_TEMPORARY"
    assert failed.head_plan_version == 2
    assert failed.stage == "generating_shots"
    resumed = agent.confirm(task_id, 2)
    assert resumed.status == "generating"
    assert resumed.head_plan_version == 2
    assert resumed.confirmed_plan_version == 2


def test_shot_regeneration_stays_shots_even_when_every_shot_is_selected(tmp_path: Path):
    agent, task_id = agent_with_plan(tmp_path)
    confirmed = agent.confirm(task_id, 1)
    with agent.db.transaction() as conn:
        mark_generation_succeeded(conn, confirmed.generation_id, agent.clock.now().isoformat())
        artifact_id = record_artifact(
            conn,
            task_id=task_id,
            generation_id=confirmed.generation_id,
            plan_version=1,
            duration_seconds=10,
            aspect_ratio="9:16",
            width=1080,
            height=1920,
            relative_path="tasks/final.mp4",
            quality_status="not_assessed",
            quality={},
            now=agent.clock.now().isoformat(),
        )
    with agent.db.transaction() as conn:
        before = conn.execute("SELECT COUNT(*) AS c FROM generations").fetchone()["c"]
    with pytest.raises(VideoAgentError) as bad_shot:
        agent.regenerate(task_id, RegenerateRequest(kind="shots", artifact_id=artifact_id, shot_ids=["missing"]), idempotency_key="bad-shot")
    assert bad_shot.value.code == "VALIDATION_ERROR"
    with pytest.raises(VideoAgentError) as bad_artifact:
        agent.regenerate(task_id, RegenerateRequest(kind="shots", artifact_id="missing", shot_ids=["s1"]))
    assert bad_artifact.value.code == "VALIDATION_ERROR"
    with agent.db.transaction() as conn:
        after = conn.execute("SELECT COUNT(*) AS c FROM generations").fetchone()["c"]
        keys = conn.execute("SELECT COUNT(*) AS c FROM idempotency WHERE idempotency_key='bad-shot'").fetchone()["c"]
    assert after == before
    assert keys == 0
    created = agent.regenerate(
        task_id,
        RegenerateRequest(kind="shots", artifact_id=artifact_id, shot_ids=["s1", "s2"]),
        idempotency_key="shots-1",
    )
    with agent.db.transaction() as conn:
        row = conn.execute("SELECT kind, shot_ids_json, base_artifact_id, plan_version FROM generations WHERE generation_id=?", (created.generation_id,)).fetchone()
        shot_rows = conn.execute("SELECT COUNT(*) AS c FROM generation_shots WHERE generation_id=?", (created.generation_id,)).fetchone()["c"]
    assert row["kind"] == "shots"
    assert json.loads(row["shot_ids_json"]) == ["s1", "s2"]
    assert row["base_artifact_id"] == artifact_id
    assert row["plan_version"] == 1
    assert shot_rows == 2
    assert agent.regenerate(task_id, RegenerateRequest(kind="shots", artifact_id=artifact_id, shot_ids=["s1", "s2"]), idempotency_key="shots-1").generation_id == created.generation_id
