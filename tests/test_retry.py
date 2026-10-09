from datetime import datetime, timezone
from pathlib import Path

import pytest

from video_agent.clock import FrozenClock
from video_agent.config import AdapterSetting, SettingsPatch, save_settings
from video_agent.errors import VideoAgentError
from video_agent.models import CreateTaskRequest, FailureView, InputPatch, PlanChange, PlanDocument, PlanPatch, RegenerateRequest, SharedVisual, ShotPlan
from video_agent.service import VideoAgent
from video_agent.state import mark_generation_failed, mark_generation_succeeded, publish_document


class IdlePlanner:
    def capabilities_note(self) -> str:
        return "idle"

    def plan(self, request):
        raise AssertionError("planner is not used")

    def revise(self, current, instruction, request):
        raise AssertionError("planner is not used")


def document() -> PlanDocument:
    return PlanDocument(
        creative="创意",
        audience="买家",
        selling_points=["轻便"],
        shared_visual=SharedVisual(product_reference="红瓶", scene="桌面", lighting="日光", palette="暖", style="写实"),
        shots=[ShotPlan(shot_id="s1", content="展示", action="转", shot_size="中景", camera="固定", scene_id="main", start_frame=0, end_frame=300, subtitle_text="卖点", voiceover_text="卖点")],
        music_direction="轻快",
        duration_seconds=10,
        aspect_ratio="9:16",
        fit="native",
        subtitles=True,
        voiceover=True,
        music=True,
        first_frame_source="generate",
        model_capabilities="单段",
    )


def make(home: Path) -> VideoAgent:
    return VideoAgent(home, clock=FrozenClock(datetime(2026, 10, 9, 3, 0, tzinfo=timezone.utc)), planner=IdlePlanner())


def test_retry_entries_follow_the_failure_scope(tmp_path: Path):
    agent = make(tmp_path)
    created = agent.create_task(CreateTaskRequest(user_prompt="原需求"))
    with agent.db.transaction() as conn:
        conn.execute(
            "UPDATE tasks SET status='failed', failure_scope='planning', stage='planning', failure_code='PROVIDER_TEMPORARY', failure_message='临时', failure_stage='planning', failure_retryable=1, pending_instruction='换成户外' WHERE task_id=?",
            (created.task_id,),
        )
    retried = agent.retry_planning(created.task_id, idempotency_key="plan-retry")
    assert retried.status == "pending"
    assert retried.failure_scope is None
    with agent.db.transaction() as conn:
        task = conn.execute("SELECT pending_instruction, user_prompt FROM tasks WHERE task_id=?", (created.task_id,)).fetchone()
        works = conn.execute("SELECT COUNT(*) AS c FROM work_items WHERE task_id=? AND kind='planning' AND done=0", (created.task_id,)).fetchone()["c"]
    assert task["pending_instruction"] == "换成户外"
    assert task["user_prompt"] == "原需求"
    assert works == 1
    assert agent.retry_planning(created.task_id, idempotency_key="plan-retry").model_dump() == retried.model_dump()
    confirmed = agent.create_task(CreateTaskRequest())
    with agent.db.transaction() as conn:
        publish_document(conn, task_id=confirmed.task_id, document=document(), now=agent.clock.now().isoformat())
    view = agent.confirm(confirmed.task_id, 1)
    with agent.db.transaction() as conn:
        conn.execute("UPDATE generations SET auto_attempts_used=2, stage='generating_shots' WHERE generation_id=?", (view.generation_id,))
        mark_generation_failed(
            conn,
            view.generation_id,
            FailureView(code="PROVIDER_TEMPORARY", message="临时", stage="generating_shots", retryable=True),
            agent.clock.now().isoformat(),
        )
    with pytest.raises(VideoAgentError) as wrong:
        agent.retry_planning(confirmed.task_id, idempotency_key="wrong-entry")
    assert wrong.value.code == "STATE_CONFLICT"
    resumed = agent.retry_generation(view.generation_id, idempotency_key="gen-retry")
    assert resumed.generation_id == view.generation_id
    assert resumed.status == "generating"
    assert resumed.stage == "generating_shots"
    with agent.db.transaction() as conn:
        count = conn.execute("SELECT COUNT(*) AS c FROM generations WHERE task_id=?", (confirmed.task_id,)).fetchone()["c"]
        generation = conn.execute("SELECT auto_attempts_used, status FROM generations WHERE generation_id=?", (view.generation_id,)).fetchone()
        work = conn.execute("SELECT step, done FROM work_items WHERE generation_id=?", (view.generation_id,)).fetchone()
        keys = conn.execute("SELECT COUNT(*) AS c FROM idempotency WHERE idempotency_key='wrong-entry'").fetchone()["c"]
    assert count == 1
    assert generation["auto_attempts_used"] == 0
    assert generation["status"] == "running"
    assert work["step"] == "submit_shot"
    assert work["done"] == 0
    assert keys == 0
    blocked = agent.create_task(CreateTaskRequest())
    with agent.db.transaction() as conn:
        publish_document(conn, task_id=blocked.task_id, document=document(), now=agent.clock.now().isoformat())
    other = agent.confirm(blocked.task_id, 1)
    with agent.db.transaction() as conn:
        mark_generation_failed(
            conn,
            other.generation_id,
            FailureView(code="FIRST_FRAME_NOT_IMPLEMENTED", message="未实现", stage="preparing_first_frame", retryable=False),
            agent.clock.now().isoformat(),
        )
    with pytest.raises(VideoAgentError) as denied:
        agent.retry_generation(other.generation_id, idempotency_key="no-retry")
    assert denied.value.code == "STATE_CONFLICT"
    with agent.db.transaction() as conn:
        assert conn.execute("SELECT COUNT(*) AS c FROM generations WHERE task_id=?", (blocked.task_id,)).fetchone()["c"] == 1
        assert conn.execute("SELECT COUNT(*) AS c FROM idempotency WHERE idempotency_key='no-retry'").fetchone()["c"] == 0


def test_write_guards_cover_the_state_table(tmp_path: Path):
    agent = make(tmp_path)
    now = agent.clock.now().isoformat()

    def conflict(call) -> None:
        with pytest.raises(VideoAgentError) as caught:
            call()
        assert caught.value.code == "STATE_CONFLICT"

    def plant(task_id: str) -> str:
        generation_id = "g-" + task_id
        with agent.db.transaction() as conn:
            conn.execute(
                """INSERT INTO generations (
                    generation_id, task_id, plan_version, kind, shot_ids_json,
                    status, stage, snapshot_json, copied_config_json,
                    failure_retryable, auto_attempts_used, created_at, updated_at
                ) VALUES (?, ?, 1, 'full', '[]', 'failed', 'generating_shots', '{}', '{}', 1, 0, ?, ?)""",
                (generation_id, task_id, now, now),
            )
        return generation_id

    def publish(task_id: str) -> None:
        with agent.db.transaction() as conn:
            publish_document(conn, task_id=task_id, document=document(), now=now)

    def succeed(task_id: str) -> str:
        publish(task_id)
        view = agent.confirm(task_id, 1)
        with agent.db.transaction() as conn:
            mark_generation_succeeded(conn, view.generation_id, now)
        return view.generation_id

    def draft():
        created = agent.create_task(CreateTaskRequest())
        succeed(created.task_id)
        return agent.revise_plan(created.task_id, PlanChange(patch=PlanPatch(duration_seconds=5)))

    def fail_generation(task_id: str) -> str:
        with agent.db.transaction() as conn:
            generation_id = conn.execute("SELECT generation_id FROM tasks WHERE task_id=?", (task_id,)).fetchone()["generation_id"]
            mark_generation_failed(
                conn,
                generation_id,
                FailureView(code="PROVIDER_TEMPORARY", message="临时", stage="generating_shots", retryable=True),
                now,
            )
        return generation_id

    waiting = agent.create_task(CreateTaskRequest())
    waiting_generation = plant(waiting.task_id)
    conflict(lambda: agent.discard_revision(waiting.task_id))
    conflict(lambda: agent.revise_plan(waiting.task_id, PlanChange(instruction="改")))
    conflict(lambda: agent.confirm(waiting.task_id, 1))
    conflict(lambda: agent.regenerate(waiting.task_id, RegenerateRequest(kind="full")))
    conflict(lambda: agent.retry_planning(waiting.task_id))
    conflict(lambda: agent.retry_generation(waiting_generation))
    kept = agent.submit_inputs(waiting.task_id, InputPatch(user_prompt="补充"))
    assert kept.status == "awaiting_input"
    assert kept.questions == ["缺少产品图片"]

    asked = agent.create_task(CreateTaskRequest())
    succeed(asked.task_id)
    with agent.db.transaction() as conn:
        conn.execute(
            "UPDATE tasks SET status='awaiting_input', stage=NULL, questions_json=? WHERE task_id=?",
            ('["缺少产品图片"]', asked.task_id),
        )
    asked_generation = plant(asked.task_id)
    conflict(lambda: agent.revise_plan(asked.task_id, PlanChange(instruction="改")))
    conflict(lambda: agent.confirm(asked.task_id, 1))
    conflict(lambda: agent.regenerate(asked.task_id, RegenerateRequest(kind="full")))
    conflict(lambda: agent.retry_planning(asked.task_id))
    conflict(lambda: agent.retry_generation(asked_generation))
    still_waiting = agent.submit_inputs(asked.task_id, InputPatch(user_prompt="仍无图"))
    assert still_waiting.status == "awaiting_input"
    assert still_waiting.confirmed_plan_version == 1
    asked_again = agent.create_task(CreateTaskRequest())
    succeed(asked_again.task_id)
    with agent.db.transaction() as conn:
        conn.execute("UPDATE tasks SET status='awaiting_input', stage=NULL WHERE task_id=?", (asked_again.task_id,))
    restored = agent.discard_revision(asked_again.task_id)
    assert restored.status == "succeeded"
    assert restored.head_plan_version == 1

    for status in ("pending", "planning", "generating"):
        blocked = agent.create_task(CreateTaskRequest())
        publish(blocked.task_id)
        with agent.db.transaction() as conn:
            conn.execute("UPDATE tasks SET status=?, stage=NULL WHERE task_id=?", (status, blocked.task_id))
        blocked_generation = plant(blocked.task_id)
        conflict(lambda task_id=blocked.task_id: agent.submit_inputs(task_id, InputPatch(user_prompt="n")))
        conflict(lambda task_id=blocked.task_id: agent.revise_plan(task_id, PlanChange(instruction="n")))
        conflict(lambda task_id=blocked.task_id: agent.confirm(task_id, 1))
        conflict(lambda task_id=blocked.task_id: agent.discard_revision(task_id))
        conflict(lambda task_id=blocked.task_id: agent.regenerate(task_id, RegenerateRequest(kind="full")))
        conflict(lambda task_id=blocked.task_id: agent.retry_planning(task_id))
        conflict(lambda generation_id=blocked_generation: agent.retry_generation(generation_id))
        assert agent.get_task(blocked.task_id).status == status

    fresh = agent.create_task(CreateTaskRequest())
    publish(fresh.task_id)
    fresh_generation = plant(fresh.task_id)
    conflict(lambda: agent.regenerate(fresh.task_id, RegenerateRequest(kind="full")))
    conflict(lambda: agent.discard_revision(fresh.task_id))
    conflict(lambda: agent.submit_inputs(fresh.task_id, InputPatch(user_prompt="n")))
    conflict(lambda: agent.retry_planning(fresh.task_id))
    conflict(lambda: agent.retry_generation(fresh_generation))
    revised = agent.revise_plan(fresh.task_id, PlanChange(patch=PlanPatch(duration_seconds=15)))
    assert revised.status == "awaiting_confirmation"
    assert revised.head_plan_version == 2
    assert revised.confirmed_plan_version is None
    opened = agent.confirm(fresh.task_id, 2)
    assert opened.status == "generating"
    assert opened.confirmed_plan_version == 2

    with_confirmed = draft()
    assert with_confirmed.confirmed_plan_version == 1
    assert with_confirmed.head_plan_version == 2
    regenerated = agent.regenerate(with_confirmed.task_id, RegenerateRequest(kind="full"))
    assert regenerated.status == "generating"
    assert regenerated.confirmed_plan_version == 1
    dropped = agent.discard_revision(draft().task_id)
    assert dropped.status == "succeeded"
    assert dropped.head_plan_version == 1
    changed = agent.revise_plan(draft().task_id, PlanChange(patch=PlanPatch(duration_seconds=15)))
    assert changed.head_plan_version == 3
    reconfirmed = agent.confirm(draft().task_id, 2)
    assert reconfirmed.status == "generating"
    assert reconfirmed.confirmed_plan_version == 2

    done = agent.create_task(CreateTaskRequest())
    done_generation = succeed(done.task_id)
    conflict(lambda: agent.submit_inputs(done.task_id, InputPatch(user_prompt="n")))
    conflict(lambda: agent.confirm(done.task_id, 1))
    conflict(lambda: agent.discard_revision(done.task_id))
    conflict(lambda: agent.retry_planning(done.task_id))
    conflict(lambda: agent.retry_generation(done_generation))
    assert agent.revise_plan(done.task_id, PlanChange(patch=PlanPatch(duration_seconds=15))).status == "awaiting_confirmation"
    done_again = agent.create_task(CreateTaskRequest())
    succeed(done_again.task_id)
    assert agent.regenerate(done_again.task_id, RegenerateRequest(kind="full")).status == "generating"

    planning_bare = agent.create_task(CreateTaskRequest())
    with agent.db.transaction() as conn:
        conn.execute(
            "UPDATE tasks SET status='failed', failure_scope='planning', stage='planning' WHERE task_id=?",
            (planning_bare.task_id,),
        )
    bare_generation = plant(planning_bare.task_id)
    conflict(lambda: agent.revise_plan(planning_bare.task_id, PlanChange(instruction="改")))
    conflict(lambda: agent.discard_revision(planning_bare.task_id))
    conflict(lambda: agent.confirm(planning_bare.task_id, 1))
    conflict(lambda: agent.regenerate(planning_bare.task_id, RegenerateRequest(kind="full")))
    conflict(lambda: agent.retry_generation(bare_generation))
    assert agent.submit_inputs(planning_bare.task_id, InputPatch(user_prompt="补")).status == "awaiting_input"
    planning_retry = agent.create_task(CreateTaskRequest())
    with agent.db.transaction() as conn:
        conn.execute(
            "UPDATE tasks SET status='failed', failure_scope='planning', stage='planning' WHERE task_id=?",
            (planning_retry.task_id,),
        )
    assert agent.retry_planning(planning_retry.task_id).status == "pending"

    planning_head = agent.create_task(CreateTaskRequest())
    publish(planning_head.task_id)
    with agent.db.transaction() as conn:
        conn.execute(
            "UPDATE tasks SET status='failed', failure_scope='planning', stage='planning' WHERE task_id=?",
            (planning_head.task_id,),
        )
    head_generation = plant(planning_head.task_id)
    conflict(lambda: agent.submit_inputs(planning_head.task_id, InputPatch(user_prompt="n")))
    conflict(lambda: agent.discard_revision(planning_head.task_id))
    conflict(lambda: agent.confirm(planning_head.task_id, 1))
    conflict(lambda: agent.regenerate(planning_head.task_id, RegenerateRequest(kind="full")))
    conflict(lambda: agent.retry_generation(head_generation))
    assert agent.revise_plan(planning_head.task_id, PlanChange(patch=PlanPatch(duration_seconds=15))).head_plan_version == 2
    planning_head_retry = agent.create_task(CreateTaskRequest())
    publish(planning_head_retry.task_id)
    with agent.db.transaction() as conn:
        conn.execute(
            "UPDATE tasks SET status='failed', failure_scope='planning', stage='planning' WHERE task_id=?",
            (planning_head_retry.task_id,),
        )
    assert agent.retry_planning(planning_head_retry.task_id).status == "pending"

    planning_confirmed = draft()
    with agent.db.transaction() as conn:
        conn.execute(
            "UPDATE tasks SET status='failed', failure_scope='planning', stage='planning' WHERE task_id=?",
            (planning_confirmed.task_id,),
        )
    confirmed_generation = plant(planning_confirmed.task_id)
    conflict(lambda: agent.submit_inputs(planning_confirmed.task_id, InputPatch(user_prompt="n")))
    conflict(lambda: agent.confirm(planning_confirmed.task_id, 2))
    conflict(lambda: agent.regenerate(planning_confirmed.task_id, RegenerateRequest(kind="full")))
    conflict(lambda: agent.retry_generation(confirmed_generation))
    assert agent.revise_plan(planning_confirmed.task_id, PlanChange(patch=PlanPatch(duration_seconds=15))).status == "awaiting_confirmation"
    planning_discard = draft()
    with agent.db.transaction() as conn:
        conn.execute(
            "UPDATE tasks SET status='failed', failure_scope='planning', stage='planning' WHERE task_id=?",
            (planning_discard.task_id,),
        )
    assert agent.discard_revision(planning_discard.task_id).status == "succeeded"
    planning_confirmed_retry = draft()
    with agent.db.transaction() as conn:
        conn.execute(
            "UPDATE tasks SET status='failed', failure_scope='planning', stage='planning' WHERE task_id=?",
            (planning_confirmed_retry.task_id,),
        )
    assert agent.retry_planning(planning_confirmed_retry.task_id).status == "pending"

    generation_draft = draft()
    draft_generation = fail_generation(generation_draft.task_id)
    conflict(lambda: agent.submit_inputs(generation_draft.task_id, InputPatch(user_prompt="n")))
    conflict(lambda: agent.retry_planning(generation_draft.task_id))
    assert agent.get_task(generation_draft.task_id).head_plan_version == 2
    resumed = agent.retry_generation(draft_generation)
    assert resumed.status == "generating"
    assert resumed.generation_id == draft_generation
    generation_regen = draft()
    fail_generation(generation_regen.task_id)
    assert agent.regenerate(generation_regen.task_id, RegenerateRequest(kind="full")).status == "generating"
    generation_revise = draft()
    fail_generation(generation_revise.task_id)
    assert agent.revise_plan(generation_revise.task_id, PlanChange(instruction="改")).status == "pending"
    generation_confirm = draft()
    fail_generation(generation_confirm.task_id)
    assert agent.confirm(generation_confirm.task_id, 2).confirmed_plan_version == 2
    generation_drop = draft()
    fail_generation(generation_drop.task_id)
    assert agent.discard_revision(generation_drop.task_id).head_plan_version == 1

    generation_head = agent.create_task(CreateTaskRequest())
    generation_id = succeed(generation_head.task_id)
    with agent.db.transaction() as conn:
        mark_generation_failed(
            conn,
            generation_id,
            FailureView(code="PROVIDER_TEMPORARY", message="临时", stage="exporting", retryable=True),
            now,
        )
    conflict(lambda: agent.submit_inputs(generation_head.task_id, InputPatch(user_prompt="n")))
    conflict(lambda: agent.confirm(generation_head.task_id, 1))
    conflict(lambda: agent.discard_revision(generation_head.task_id))
    conflict(lambda: agent.retry_planning(generation_head.task_id))
    assert agent.retry_generation(generation_id).generation_id == generation_id
    generation_head_regen = agent.create_task(CreateTaskRequest())
    failed_id = succeed(generation_head_regen.task_id)
    with agent.db.transaction() as conn:
        mark_generation_failed(
            conn,
            failed_id,
            FailureView(code="PROVIDER_REJECTED", message="拒绝", stage="exporting", retryable=True),
            now,
        )
    assert agent.regenerate(generation_head_regen.task_id, RegenerateRequest(kind="full")).status == "generating"
    generation_head_revise = agent.create_task(CreateTaskRequest())
    revise_id = succeed(generation_head_revise.task_id)
    with agent.db.transaction() as conn:
        mark_generation_failed(
            conn,
            revise_id,
            FailureView(code="PROVIDER_REJECTED", message="拒绝", stage="exporting", retryable=True),
            now,
        )
    assert agent.revise_plan(generation_head_revise.task_id, PlanChange(patch=PlanPatch(duration_seconds=15))).status == "awaiting_confirmation"
