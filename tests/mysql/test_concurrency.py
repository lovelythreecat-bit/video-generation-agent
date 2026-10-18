from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from video_agent import VideoAgentError
from video_agent.models import CreateTaskRequest, ProductInfo
from video_agent.state import publish_document


def test_concurrent_same_key_creates_one_task(mysql_database, tmp_path):
    from test_core import agent_for

    agent = agent_for(mysql_database, tmp_path)
    barrier = Barrier(8)

    def create():
        barrier.wait(timeout=10)
        return agent.create_task(CreateTaskRequest(product_info=ProductInfo(name="并发中文")), idempotency_key="same-key")

    with ThreadPoolExecutor(max_workers=8) as pool:
        views = list(pool.map(lambda _: create(), range(8)))
    assert len({view.task_id for view in views}) == 1
    assert all(view.model_dump() == views[0].model_dump() for view in views)
    with mysql_database.transaction(write=False) as connection:
        assert connection.execute("SELECT COUNT(*) AS n FROM tasks").fetchone()["n"] == 1
        assert connection.execute("SELECT COUNT(*) AS n FROM idempotency").fetchone()["n"] == 1
    with pytest.raises(VideoAgentError) as conflict:
        agent.create_task(CreateTaskRequest(user_prompt="different"), idempotency_key="same-key")
    assert conflict.value.code == "IDEMPOTENCY_CONFLICT"
    assert agent.create_task(CreateTaskRequest(), idempotency_key="Same-Key").task_id != views[0].task_id


def test_concurrent_confirm_has_one_generation(mysql_database, tmp_path):
    from test_core import agent_for
    from tests.test_confirm import document

    agent = agent_for(mysql_database, tmp_path)
    task = agent.create_task(CreateTaskRequest())
    with mysql_database.transaction() as connection:
        publish_document(connection, task_id=task.task_id, document=document(), now=agent.clock.now().isoformat())
    barrier = Barrier(2)

    def confirm():
        barrier.wait(timeout=10)
        try:
            return agent.confirm(task.task_id, 1)
        except VideoAgentError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: confirm(), range(2)))
    success = [item for item in outcomes if not isinstance(item, VideoAgentError)]
    failures = [item for item in outcomes if isinstance(item, VideoAgentError)]
    assert len(success) == len(failures) == 1
    assert failures[0].code == "STATE_CONFLICT"
    assert failures[0].generation_id == success[0].generation_id
    with mysql_database.transaction(write=False) as connection:
        assert connection.execute("SELECT COUNT(*) AS n FROM generations").fetchone()["n"] == 1
        assert connection.execute("SELECT COUNT(*) AS n FROM work_items WHERE kind='generation'").fetchone()["n"] == 1


def test_deadlock_retries_only_rolled_back_database_callback(mysql_database):
    with mysql_database.transaction() as connection:
        for asset_id in ("deadlock-a", "deadlock-b"):
            connection.execute("""INSERT INTO assets (asset_id,kind,filename,media_type,byte_size,relative_path,created_at)
                VALUES (:asset_id,'image','a.png','image/png',1,'assets/a.png','t')""", {"asset_id": asset_id})
    barrier = Barrier(2)

    def update_pair(order):
        attempts = 0

        def update(connection):
            nonlocal attempts
            attempts += 1
            connection.execute("UPDATE assets SET byte_size=byte_size+1 WHERE asset_id=:asset_id", {"asset_id": order[0]})
            if attempts == 1:
                barrier.wait(timeout=10)
            connection.execute("UPDATE assets SET byte_size=byte_size+1 WHERE asset_id=:asset_id", {"asset_id": order[1]})

        mysql_database.run_transaction(update)
        return attempts

    with ThreadPoolExecutor(max_workers=2) as pool:
        attempts = list(pool.map(update_pair, (("deadlock-a", "deadlock-b"), ("deadlock-b", "deadlock-a"))))
    assert sum(attempts) >= 3
    assert max(attempts) <= 3
    with mysql_database.transaction(write=False) as connection:
        assert [row["byte_size"] for row in connection.execute("SELECT byte_size FROM assets ORDER BY asset_id")] == [3, 3]
