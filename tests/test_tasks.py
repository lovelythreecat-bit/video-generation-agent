import io
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from PIL import Image

from video_agent import VideoAgent, VideoAgentError
from video_agent.clock import FrozenClock
from video_agent.config import AdapterSetting, SettingsPatch, save_settings
from video_agent.models import AudioOptions, CreateTaskRequest, InputPatch, ProductInfo, VideoOptions
from video_agent.ports import PlanRequest


class IdlePlanner:
    def capabilities_note(self) -> str:
        return "idle"

    def plan(self, request: PlanRequest):
        raise AssertionError("plan should not run in this task")

    def revise(self, current, instruction: str, request: PlanRequest):
        raise AssertionError("revise should not run in this task")


def png_bytes() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (2, 2), (200, 10, 10)).save(buffer, format="PNG")
    return buffer.getvalue()


def make_agent(home: Path) -> tuple[VideoAgent, FrozenClock]:
    clock = FrozenClock(datetime(2026, 10, 9, 3, 0, tzinfo=timezone.utc))
    return VideoAgent(home, clock=clock, planner=IdlePlanner()), clock


def test_register_asset_writes_file_then_row(tmp_path: Path):
    agent, _clock = make_agent(tmp_path)
    asset_id = agent.register_asset(filename="bottle.png", kind="image", data=png_bytes())
    with agent.db.transaction() as conn:
        row = conn.execute("SELECT relative_path, kind FROM assets WHERE asset_id=?", (asset_id,)).fetchone()
    assert row["kind"] == "image"
    assert agent.files.read_bytes(row["relative_path"]) == png_bytes()
    with pytest.raises(VideoAgentError) as caught:
        agent.register_asset(filename="x.png", kind="image")
    assert caught.value.code == "VALIDATION_ERROR"


def test_create_without_images_waits_and_with_images_queues_planning(tmp_path: Path):
    agent, clock = make_agent(tmp_path)
    empty = agent.create_task(CreateTaskRequest(product_info=ProductInfo(name="瓶子"), user_prompt="桌面"))
    assert empty.status == "awaiting_input"
    assert empty.questions == ["缺少产品图片"]
    assert empty.stage is None
    assert empty.product_name == "瓶子"
    assert empty.product_info.name == "瓶子"
    assert empty.user_prompt == "桌面"
    assert empty.video_options.aspect_ratio == "9:16"
    assert empty.image_asset_ids == []
    assert empty.first_frame_asset_id is None
    with agent.db.transaction() as conn:
        assert conn.execute("SELECT COUNT(*) AS c FROM work_items").fetchone()["c"] == 0
    image_id = agent.register_asset(filename="bottle.png", kind="image", data=png_bytes())
    clock.advance(1)
    created = agent.create_task(
        CreateTaskRequest(
            product_info=ProductInfo(name="瓶子"),
            images=[image_id],
            first_frame_asset_id=image_id,
            external_request_id="same",
        )
    )
    again = agent.create_task(CreateTaskRequest(images=[image_id], external_request_id="same"))
    assert created.status == "pending"
    assert created.image_asset_ids == [image_id]
    assert created.first_frame_asset_id == image_id
    assert created.task_id != again.task_id
    with agent.db.transaction() as conn:
        work = conn.execute(
            "SELECT kind, step, copied_config_json FROM work_items WHERE task_id=?",
            (created.task_id,),
        ).fetchone()
    copied = json.loads(work["copied_config_json"])
    assert work["kind"] == "planning"
    assert work["step"] == "start"
    assert copied["mode"] == "fake"
    assert set(copied["adapters"]) == {"planner", "video", "voice", "music", "visual"}
    save_settings(tmp_path, SettingsPatch(planner=AdapterSetting(adapter="fake", fields={"base_url": "http://new"})))
    clock.advance(1)
    later = agent.create_task(CreateTaskRequest(images=[image_id], first_frame_asset_id=image_id))
    with agent.db.transaction() as conn:
        old = json.loads(conn.execute("SELECT copied_config_json FROM work_items WHERE task_id=?", (created.task_id,)).fetchone()["copied_config_json"])
        new = json.loads(conn.execute("SELECT copied_config_json FROM work_items WHERE task_id=?", (later.task_id,)).fetchone()["copied_config_json"])
    assert "base_url" not in old["adapters"]["planner"]
    assert new["adapters"]["planner"]["base_url"] == "http://new"


def test_create_validation_and_conflict_do_not_store_idempotency(tmp_path: Path):
    agent, _clock = make_agent(tmp_path)
    image_id = agent.register_asset(filename="bottle.png", kind="image", data=png_bytes())
    audio_id = agent.register_asset(filename="bed.wav", kind="audio", data=b"RIFF" + (36).to_bytes(4, "little") + b"WAVE" + b"fmt " + b"\x00" * 24)
    with pytest.raises(VideoAgentError) as bad_ratio:
        agent.create_task(
            CreateTaskRequest(images=[image_id], video_options=VideoOptions(aspect_ratio="2:1")),
            idempotency_key="ratio",
        )
    assert bad_ratio.value.code == "VALIDATION_ERROR"
    assert bad_ratio.value.details["aspect_ratios"] == ["9:16", "16:9", "1:1", "4:5", "3:4", "4:3", "3:2", "2:3"]
    with pytest.raises(VideoAgentError) as bad_duration:
        agent.create_task(
            CreateTaskRequest(images=[image_id], video_options=VideoOptions(duration_seconds=12)),
            idempotency_key="duration",
        )
    assert bad_duration.value.details["duration_tiers"] == [5, 10, 15, 30]
    with pytest.raises(VideoAgentError) as bad_frame:
        agent.create_task(CreateTaskRequest(images=[image_id], first_frame_asset_id=audio_id), idempotency_key="frame")
    assert bad_frame.value.code == "VALIDATION_ERROR"
    with pytest.raises(VideoAgentError):
        agent.create_task(
            CreateTaskRequest(images=[image_id], audio_options=AudioOptions(music_asset_id=image_id)),
            idempotency_key="music",
        )
    with pytest.raises(VideoAgentError) as bad_key:
        agent.create_task(CreateTaskRequest(images=[image_id]), idempotency_key="bad key")
    assert bad_key.value.code == "VALIDATION_ERROR"
    with agent.db.transaction() as conn:
        assert conn.execute("SELECT COUNT(*) AS c FROM tasks").fetchone()["c"] == 0
        assert conn.execute("SELECT COUNT(*) AS c FROM idempotency").fetchone()["c"] == 0
    pending = agent.create_task(CreateTaskRequest(images=[image_id], first_frame_asset_id=image_id))
    with pytest.raises(VideoAgentError) as conflict:
        agent.submit_inputs(pending.task_id, InputPatch(user_prompt="补充"), idempotency_key="blocked")
    assert conflict.value.code == "STATE_CONFLICT"
    with agent.db.transaction() as conn:
        assert conn.execute("SELECT COUNT(*) AS c FROM idempotency").fetchone()["c"] == 0


def test_replay_returns_the_original_snapshot(tmp_path: Path):
    agent, clock = make_agent(tmp_path)
    first = agent.create_task(CreateTaskRequest(product_info=ProductInfo(name="瓶子")), idempotency_key="create-1")
    clock.advance(30)
    image_id = agent.register_asset(filename="bottle.png", kind="image", data=png_bytes())
    agent.submit_inputs(first.task_id, InputPatch(images=[image_id]))
    replay = agent.create_task(CreateTaskRequest(product_info=ProductInfo(name="瓶子")), idempotency_key="create-1")
    assert replay.model_dump() == first.model_dump()
    assert replay.status == "awaiting_input"
    assert agent.get_task(first.task_id).status == "pending"
    with pytest.raises(VideoAgentError) as caught:
        agent.create_task(CreateTaskRequest(product_info=ProductInfo(name="杯子")), idempotency_key="create-1")
    assert caught.value.code == "IDEMPOTENCY_CONFLICT"
    with agent.db.transaction() as conn:
        assert conn.execute("SELECT COUNT(*) AS c FROM tasks").fetchone()["c"] == 1


def test_submit_inputs_replaces_fields_and_replans_only_with_images(tmp_path: Path):
    agent, _clock = make_agent(tmp_path)
    image_id = agent.register_asset(filename="bottle.png", kind="image", data=png_bytes())
    waiting = agent.create_task(CreateTaskRequest())
    still_empty = agent.submit_inputs(waiting.task_id, InputPatch(user_prompt="还没有图", images=[]))
    assert still_empty.status == "awaiting_input"
    assert still_empty.questions == ["缺少产品图片"]
    queued = agent.submit_inputs(
        waiting.task_id,
        InputPatch(images=[image_id], first_frame_asset_id=image_id, user_prompt="桌面展示"),
    )
    assert queued.status == "pending"
    assert queued.questions == []
    with agent.db.transaction() as conn:
        row = conn.execute("SELECT user_prompt, first_frame_asset_id FROM tasks WHERE task_id=?", (waiting.task_id,)).fetchone()
        works = conn.execute("SELECT COUNT(*) AS c FROM work_items WHERE task_id=? AND done=0", (waiting.task_id,)).fetchone()["c"]
    assert row["user_prompt"] == "桌面展示"
    assert row["first_frame_asset_id"] == image_id
    assert works == 1
    with agent.db.transaction() as conn:
        conn.execute(
            "UPDATE tasks SET status='failed', failure_scope='planning', stage='planning' WHERE task_id=?",
            (waiting.task_id,),
        )
        conn.execute("UPDATE work_items SET done=1 WHERE task_id=?", (waiting.task_id,))
    retried = agent.submit_inputs(waiting.task_id, InputPatch(user_prompt="换个场景"))
    assert retried.status == "pending"
    with agent.db.transaction() as conn:
        conn.execute(
            "UPDATE tasks SET status='failed', failure_scope='planning', stage='planning' WHERE task_id=?",
            (waiting.task_id,),
        )
        conn.execute(
            "INSERT INTO plan_versions (task_id, plan_version, document_json, abandoned, created_at) VALUES (?, 1, '{}', 0, 't')",
            (waiting.task_id,),
        )
    with pytest.raises(VideoAgentError) as caught:
        agent.submit_inputs(waiting.task_id, InputPatch(user_prompt="不能再补"))
    assert caught.value.code == "STATE_CONFLICT"
