import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from video_agent.clock import FrozenClock
from video_agent.errors import VideoAgentError
from video_agent.models import (
    CreateTaskRequest,
    PlanChange,
    PlanDocument,
    PlanPatch,
    SharedVisual,
    ShotPlan,
    ShotTextPatch,
)
from video_agent.ports import VideoCapabilities
from video_agent.service import VideoAgent
from video_agent.state import publish_document


class IdlePlanner:
    def capabilities_note(self) -> str:
        return "idle"

    def plan(self, request):
        raise AssertionError("planner is not used")

    def revise(self, current, instruction, request):
        raise AssertionError("planner is not used")


class AspectVideo:
    def capabilities(self) -> VideoCapabilities:
        return VideoCapabilities(
            native_aspects=["9:16"],
            max_segment_seconds=30,
            supports_first_frame=True,
            supports_reference_images=True,
            supports_tail_frame=True,
        )


def document(*, subtitles: bool = True, text: str = "卖点") -> PlanDocument:
    return PlanDocument(
        creative="创意",
        audience="买家",
        selling_points=["轻便"],
        shared_visual=SharedVisual(
            product_reference="红瓶",
            scene="桌面",
            lighting="日光",
            palette="暖",
            style="写实",
        ),
        shots=[
            ShotPlan(shot_id="s1", content="展示", action="转", shot_size="中景", camera="固定", scene_id="main", start_frame=0, end_frame=150, subtitle_text=text, voiceover_text=text),
            ShotPlan(shot_id="s2", content="细节", action="推", shot_size="特写", camera="推进", scene_id="main", start_frame=150, end_frame=300, subtitle_text=text, voiceover_text=text),
        ],
        music_direction="轻快",
        duration_seconds=10,
        aspect_ratio="9:16",
        fit="native",
        subtitles=subtitles,
        voiceover=True,
        music=True,
        first_frame_source="generate",
        model_capabilities="单段 30 秒",
    )


def seed(home: Path) -> tuple[VideoAgent, str]:
    clock = FrozenClock(datetime(2026, 10, 9, 3, 0, tzinfo=timezone.utc))
    agent = VideoAgent(home, clock=clock, planner=IdlePlanner(), video=AspectVideo())
    created = agent.create_task(CreateTaskRequest())
    with agent.db.transaction() as conn:
        publish_document(conn, task_id=created.task_id, document=document(), now=clock.now().isoformat())
    return agent, created.task_id


def test_structured_revise_redistributes_and_sets_fit(tmp_path: Path):
    agent, task_id = seed(tmp_path)
    revised = agent.revise_plan(task_id, PlanChange(patch=PlanPatch(duration_seconds=5)), idempotency_key="dur")
    assert revised.status == "awaiting_confirmation"
    assert revised.head_plan_version == 2
    replay = agent.revise_plan(task_id, PlanChange(patch=PlanPatch(duration_seconds=5)), idempotency_key="dur")
    assert replay.model_dump() == revised.model_dump()
    with agent.db.transaction() as conn:
        row = conn.execute(
            "SELECT document_json FROM plan_versions WHERE task_id=? AND plan_version=2",
            (task_id,),
        ).fetchone()
        count = conn.execute("SELECT COUNT(*) AS c FROM plan_versions WHERE task_id=?", (task_id,)).fetchone()["c"]
    body = PlanDocument.model_validate_json(row["document_json"])
    assert [shot.end_frame - shot.start_frame for shot in body.shots] == [75, 75]
    assert body.duration_seconds == 5
    assert count == 2
    fitted = agent.revise_plan(task_id, PlanChange(patch=PlanPatch(aspect_ratio="1:1")))
    cropped = agent.revise_plan(task_id, PlanChange(patch=PlanPatch(aspect_ratio="16:9", fit="crop")))
    assert fitted.head_plan_version == 3
    assert cropped.head_plan_version == 4
    with agent.db.transaction() as conn:
        pad = PlanDocument.model_validate_json(conn.execute("SELECT document_json FROM plan_versions WHERE task_id=? AND plan_version=3", (task_id,)).fetchone()["document_json"])
        crop = PlanDocument.model_validate_json(conn.execute("SELECT document_json FROM plan_versions WHERE task_id=? AND plan_version=4", (task_id,)).fetchone()["document_json"])
    assert pad.fit == "pad"
    assert crop.fit == "crop"


def test_revise_validation_does_not_publish_or_store_idempotency(tmp_path: Path):
    agent, task_id = seed(tmp_path)
    silent = document(subtitles=False, text="")
    with agent.db.transaction() as conn:
        conn.execute("DELETE FROM plan_versions WHERE task_id=?", (task_id,))
        publish_document(conn, task_id=task_id, document=silent, now=agent.clock.now().isoformat())
    cases = [
        PlanChange(),
        PlanChange(instruction="改", patch=PlanPatch(duration_seconds=5)),
        PlanChange(patch=PlanPatch(subtitles=True)),
        PlanChange(patch=PlanPatch(shots=[], shot_texts=[ShotTextPatch(shot_id="s1", content="新")])),
    ]
    for change in cases:
        with pytest.raises(VideoAgentError) as caught:
            agent.revise_plan(task_id, change, idempotency_key="bad-revise")
        assert caught.value.code == "VALIDATION_ERROR"
    with agent.db.transaction() as conn:
        count = conn.execute("SELECT COUNT(*) AS c FROM plan_versions WHERE task_id=?", (task_id,)).fetchone()["c"]
        keys = conn.execute("SELECT COUNT(*) AS c FROM idempotency").fetchone()["c"]
        head = conn.execute("SELECT head_plan_version FROM tasks WHERE task_id=?", (task_id,)).fetchone()["head_plan_version"]
    assert count == 1
    assert keys == 0
    assert head == 1


def test_natural_language_revise_keeps_head_and_queues_planning(tmp_path: Path):
    agent, task_id = seed(tmp_path)
    view = agent.revise_plan(task_id, PlanChange(instruction="换成户外"))
    assert view.status == "pending"
    assert view.head_plan_version == 1
    with agent.db.transaction() as conn:
        task = conn.execute("SELECT pending_instruction FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        versions = conn.execute("SELECT COUNT(*) AS c FROM plan_versions WHERE task_id=?", (task_id,)).fetchone()["c"]
        work = conn.execute(
            "SELECT kind, step FROM work_items WHERE task_id=? AND done=0",
            (task_id,),
        ).fetchone()
    assert task["pending_instruction"] == "换成户外"
    assert versions == 1
    assert work["kind"] == "planning"
    assert work["step"] == "start"


def test_discard_restores_confirmed_head_below_max_version(tmp_path: Path):
    agent, task_id = seed(tmp_path)
    now = agent.clock.now().isoformat()
    with agent.db.transaction() as conn:
        conn.execute("UPDATE tasks SET confirmed_plan_version=1, generation_id='gen-1' WHERE task_id=?", (task_id,))
        conn.execute(
            """INSERT INTO generations (
                generation_id, task_id, plan_version, kind, shot_ids_json, base_artifact_id,
                status, stage, snapshot_json, copied_config_json, auto_attempts_used, created_at, updated_at
            ) VALUES ('gen-1', ?, 1, 'full', '["s1","s2"]', NULL, 'succeeded', 'exporting', '{}', '{}', 0, ?, ?)""",
            (task_id, now, now),
        )
    agent.revise_plan(task_id, PlanChange(patch=PlanPatch(duration_seconds=15)))
    discarded = agent.discard_revision(task_id)
    assert discarded.status == "succeeded"
    assert discarded.head_plan_version == 1
    assert discarded.confirmed_plan_version == 1
    with agent.db.transaction() as conn:
        max_version = conn.execute("SELECT MAX(plan_version) AS n FROM plan_versions WHERE task_id=?", (task_id,)).fetchone()["n"]
        abandoned = conn.execute(
            "SELECT abandoned FROM plan_versions WHERE task_id=? AND plan_version=2",
            (task_id,),
        ).fetchone()["abandoned"]
        generations = conn.execute("SELECT COUNT(*) AS c FROM generations WHERE task_id=?", (task_id,)).fetchone()["c"]
    assert max_version == 2
    assert abandoned == 1
    assert generations == 1
    with pytest.raises(VideoAgentError) as caught:
        agent.discard_revision(task_id, idempotency_key="no-confirmed")
    fresh = VideoAgent(tmp_path / "other", clock=agent.clock, planner=IdlePlanner(), video=AspectVideo())
    only = fresh.create_task(CreateTaskRequest())
    with fresh.db.transaction() as conn:
        publish_document(conn, task_id=only.task_id, document=document(), now=now)
    with pytest.raises(VideoAgentError) as missing_confirmed:
        fresh.discard_revision(only.task_id)
    assert caught.value.code == "STATE_CONFLICT"
    assert missing_confirmed.value.code == "STATE_CONFLICT"
