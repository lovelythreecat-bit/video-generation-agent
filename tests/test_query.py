from datetime import datetime, timezone
from pathlib import Path

import pytest

from video_agent.clock import FrozenClock
from video_agent.errors import VideoAgentError
from video_agent.models import CreateTaskRequest, PlanChange, PlanDocument, PlanPatch, ProductInfo, SharedVisual, ShotPlan
from video_agent.service import VideoAgent
from video_agent.state import mark_generation_failed, mark_generation_succeeded, publish_document, record_artifact
from video_agent.models import FailureView


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


def test_queries_order_flags_and_missing_objects(tmp_path: Path):
    clock = FrozenClock(datetime(2026, 10, 9, 3, 0, tzinfo=timezone.utc))
    agent = VideoAgent(tmp_path, clock=clock, planner=IdlePlanner())
    first = agent.create_task(CreateTaskRequest(product_info=ProductInfo(name="甲")))
    second = agent.create_task(CreateTaskRequest(product_info=ProductInfo(name="乙")))
    tied = [item.task_id for item in agent.list_tasks()]
    assert tied == sorted([first.task_id, second.task_id], reverse=True)
    clock.advance(1)
    third = agent.create_task(CreateTaskRequest(product_info=ProductInfo(name="丙")))
    assert agent.list_tasks()[0].task_id == third.task_id
    assert agent.list_tasks()[0].product_name == "丙"
    assert agent.get_task(first.task_id).questions == ["缺少产品图片"]
    with agent.db.transaction() as conn:
        publish_document(conn, task_id=first.task_id, document=document(), now=clock.now().isoformat())
    confirmed = agent.confirm(first.task_id, 1)
    generation = agent.get_generation(confirmed.generation_id)
    assert generation.kind == "full"
    assert generation.status == "queued"
    assert generation.shot_ids == ["s1"]
    assert agent.get_task(first.task_id).completed_shots == 0
    with agent.db.transaction() as conn:
        mark_generation_succeeded(conn, confirmed.generation_id, clock.now().isoformat())
        first_artifact = record_artifact(conn, task_id=first.task_id, generation_id=confirmed.generation_id, plan_version=1, duration_seconds=10, aspect_ratio="9:16", width=1080, height=1920, relative_path=f"tasks/{first.task_id}/generations/{confirmed.generation_id}/final.mp4", quality_status="not_assessed", quality={}, now=clock.now().isoformat())
    agent.files.write_bytes(f"tasks/{first.task_id}/generations/{confirmed.generation_id}/final.mp4", b"mp4")
    assert agent.get_task(first.task_id).completed_shots is None
    assert agent.get_task(first.task_id).questions == []
    agent.revise_plan(first.task_id, PlanChange(patch=PlanPatch(duration_seconds=15)))
    agent.discard_revision(first.task_id)
    old = agent.get_plan(first.task_id, 2)
    head = agent.get_plan(first.task_id, 1)
    assert old.abandoned is True
    assert old.is_head is False
    assert head.is_head is True
    assert head.confirmed is True
    assert head.plan_version == 1
    with agent.db.transaction() as conn:
        second_artifact = record_artifact(conn, task_id=first.task_id, generation_id=confirmed.generation_id, plan_version=1, duration_seconds=10, aspect_ratio="9:16", width=1080, height=1920, relative_path="tasks/second.mp4", quality_status="passed", quality={"items": []}, now=clock.now().isoformat())
    listed = agent.list_artifacts(first.task_id)
    assert [item.version for item in listed] == [2, 1]
    assert listed[0].artifact_id == second_artifact
    assert agent.artifact_path(first_artifact) == agent.files.path_for(f"tasks/{first.task_id}/generations/{confirmed.generation_id}/final.mp4")
    with agent.db.transaction() as conn:
        publish_document(conn, task_id=second.task_id, document=document(), now=clock.now().isoformat())
    other = agent.confirm(second.task_id, 1)
    with agent.db.transaction() as conn:
        mark_generation_failed(conn, other.generation_id, FailureView(code="PROVIDER_REJECTED", message="拒绝", stage="generating_shots", retryable=True), clock.now().isoformat())
    assert agent.get_task(second.task_id).stage == "generating_shots"
    assert agent.get_task(second.task_id).failure_scope == "generation"
    for call in (
        lambda: agent.get_task("missing"),
        lambda: agent.get_plan(first.task_id, 99),
        lambda: agent.get_generation("missing"),
        lambda: agent.artifact_path("missing"),
    ):
        with pytest.raises(VideoAgentError) as caught:
            call()
        assert caught.value.code == "NOT_FOUND"
