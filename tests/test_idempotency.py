from pathlib import Path

import pytest

from video_agent.db import Database
from video_agent.errors import VideoAgentError
from video_agent.idempotency import (
    create_task_payload,
    lookup_replay,
    request_hash,
    store_replay,
    validate_idempotency_key,
)
from video_agent.models import CreateTaskRequest, ProductInfo, TaskView


def _view(task_id: str = "task-1") -> TaskView:
    return TaskView(task_id=task_id, status="pending", updated_at="2026-10-09T03:00:00+00:00")


def test_hash_ignores_key_order_and_rejects_bad_keys():
    assert request_hash({"b": 1, "a": 2}) == request_hash({"a": 2, "b": 1})
    payload = create_task_payload(CreateTaskRequest(product_info=ProductInfo(name="瓶")))
    assert "idempotency_key" not in payload
    validate_idempotency_key(None)
    validate_idempotency_key("a" * 128)
    with pytest.raises(VideoAgentError) as caught:
        validate_idempotency_key("bad key")
    assert caught.value.code == "VALIDATION_ERROR"
    with pytest.raises(VideoAgentError):
        validate_idempotency_key("")
    with pytest.raises(VideoAgentError):
        validate_idempotency_key("a" * 129)


def test_replay_returns_snapshot_and_conflict_keeps_it(tmp_path: Path):
    database = Database(tmp_path)
    original = _view()
    payload = {"name": "瓶"}
    with database.transaction() as conn:
        assert lookup_replay(conn, "create_task", "k1", payload) is None
        store_replay(
            conn,
            method="create_task",
            key="k1",
            payload=payload,
            task_id="task-1",
            view=original,
            now="2026-10-09T03:00:00+00:00",
        )
    with database.transaction() as conn:
        replay = lookup_replay(conn, "create_task", "k1", payload)
    assert replay == original
    with pytest.raises(VideoAgentError) as caught:
        with database.transaction() as conn:
            lookup_replay(conn, "create_task", "k1", {"name": "杯"})
    assert caught.value.code == "IDEMPOTENCY_CONFLICT"
    with database.transaction() as conn:
        assert lookup_replay(conn, "create_task", "k1", payload) == original
        assert lookup_replay(conn, "submit_inputs", "k1", payload) is None


def test_failed_transaction_does_not_store_replay(tmp_path: Path):
    database = Database(tmp_path)
    with pytest.raises(RuntimeError):
        with database.transaction() as conn:
            store_replay(
                conn,
                method="create_task",
                key="k2",
                payload={"name": "瓶"},
                task_id="task-1",
                view=_view(),
                now="2026-10-09T03:00:00+00:00",
            )
            raise RuntimeError("boom")
    with database.transaction() as conn:
        count = conn.execute("SELECT COUNT(*) AS c FROM idempotency").fetchone()["c"]
    assert count == 0
