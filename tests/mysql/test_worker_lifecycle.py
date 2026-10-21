import threading
import io
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest
from cryptography.fernet import Fernet
from PIL import Image

from video_agent.clock import FrozenClock
from video_agent.config_store import ensure_settings_schema
from video_agent.models import CreateTaskRequest
from video_agent.service import VideoAgent
from video_agent.settings_service import SettingsService


def _create(agent):
    data = io.BytesIO()
    Image.new('RGB', (10, 10)).save(data, format='PNG')
    image = agent.register_asset(filename='a.png', kind='image', data=data.getvalue())
    return agent.create_task(CreateTaskRequest(images=[image], product_info={'name': '瓶子'}))


@pytest.fixture
def agent(mysql_database, tmp_path):
    from video_agent.provider_jobs import metadata
    from video_agent.config_store import metadata as settings_metadata, seed_default_settings
    metadata.create_all(mysql_database.engine)
    with mysql_database.transaction() as conn:
        conn.execute('DELETE FROM worker_heartbeats')
        conn.execute('DELETE FROM provider_jobs')
        for table in reversed(settings_metadata.sorted_tables):
            conn.execute(f'DELETE FROM {table.name}')
        seed_default_settings(conn)
    ensure_settings_schema(mysql_database)
    return VideoAgent(tmp_path, database=mysql_database,
        settings_service=SettingsService(mysql_database, Fernet.generate_key()),
        clock=FrozenClock(datetime(2026, 10, 14, tzinfo=timezone.utc)))


def test_two_claimers_do_not_share_work(agent):
    from video_agent.worker import _claim
    _create(agent)
    def claim(owner):
        with agent.db.transaction() as conn:
            return _claim(conn, owner=owner, now_iso=agent.clock.now().isoformat(), lease_until='2026-10-14T00:01:00+00:00')
    with ThreadPoolExecutor(2) as pool:
        rows = list(pool.map(claim, ['a', 'b']))
    assert sum(row is not None for row in rows) == 1


def test_waiting_claimer_rechecks_committed_lease(agent, monkeypatch):
    """MySQL 5.7 waits on FOR UPDATE, then must exclude the newly leased row."""
    from video_agent.db import Connection
    from video_agent.worker import _claim

    first_task = _create(agent)
    second_task = _create(agent)
    entered = threading.Event()
    original = Connection.execute

    def observe(conn, sql, parameters=None):
        if threading.current_thread().name.startswith('waiting-claimer') and 'SELECT * FROM work_items' in sql:
            entered.set()
        return original(conn, sql, parameters)

    monkeypatch.setattr(Connection, 'execute', observe)

    def claim(owner):
        with agent.db.transaction() as conn:
            return _claim(conn, owner=owner, now_iso=agent.clock.now().isoformat(), lease_until='2026-10-14T00:01:00+00:00')

    with ThreadPoolExecutor(1, thread_name_prefix='waiting-claimer') as pool:
        with agent.db.transaction() as conn:
            first = _claim(conn, owner='first', now_iso=agent.clock.now().isoformat(), lease_until='2026-10-14T00:01:00+00:00')
            future = pool.submit(claim, 'second')
            assert entered.wait(5)
        second = future.result(timeout=10)

    assert {first['task_id'], second['task_id']} == {first_task.task_id, second_task.task_id}
    assert first['work_id'] != second['work_id']
    with agent.db.transaction(write=False) as conn:
        owners = {row['lease_owner'] for row in conn.execute('SELECT lease_owner FROM work_items')}
    assert owners == {'first', 'second'}


def test_expired_owner_cannot_commit(agent):
    from video_agent.lease import LeaseLostError, assert_lease
    task = _create(agent)
    with agent.db.transaction() as conn:
        row = conn.execute('SELECT work_id FROM work_items WHERE task_id=:id', {'id': task.task_id}).fetchone()
        conn.execute("UPDATE work_items SET lease_owner='old', lease_until=:now WHERE work_id=:id", {'now': agent.clock.now().isoformat(), 'id': row['work_id']})
    with pytest.raises(LeaseLostError):
        with agent.db.transaction() as conn:
            assert_lease(conn, row['work_id'], 'old', agent.clock)
            conn.execute("UPDATE tasks SET status='failed' WHERE task_id=:id", {'id': task.task_id})
    assert agent.get_task(task.task_id).status == 'pending'


def test_expired_owner_cannot_renew(agent):
    from video_agent.lease import LeaseLostError, hold_lease
    task = _create(agent)
    with agent.db.transaction() as conn:
        work = conn.execute('SELECT * FROM work_items WHERE task_id=:id', {'id': task.task_id}).fetchone()
        conn.execute("UPDATE work_items SET lease_owner=:owner, lease_until=:now WHERE work_id=:id", {'owner': agent.worker_owner, 'now': agent.clock.now().isoformat(), 'id': work['work_id']})
    with pytest.raises(LeaseLostError):
        with hold_lease(agent.db, work['work_id'], agent.worker_owner, agent.clock, 30):
            pytest.fail('expired lease was revived')


def test_worker_restart_recovers_expired_lease(agent):
    task = _create(agent)
    with agent.db.transaction() as conn:
        conn.execute("UPDATE work_items SET lease_owner='dead', lease_until='2026-10-13T00:00:00+00:00' WHERE task_id=:id", {'id': task.task_id})
    assert agent.recover() == 1
    assert agent.tick().did_work
    assert agent.tick().did_work
    assert agent.get_task(task.task_id).status == 'awaiting_confirmation'


def test_stop_preserves_queued_work(agent):
    from video_agent.worker_main import run_worker
    task = _create(agent)
    stop = threading.Event()
    stop.set()
    run_worker(agent, stop)
    with agent.db.transaction(write=False) as conn:
        work = conn.execute('SELECT * FROM work_items WHERE task_id=:id', {'id': task.task_id}).fetchone()
    assert work['done'] == 0 and work['lease_owner'] is None


def test_singleton_loser_does_not_recover_or_tick(agent, monkeypatch):
    from video_agent.worker_main import acquire_worker, run_worker
    assert acquire_worker(agent.db, 'other', agent.clock)
    monkeypatch.setattr(agent, 'recover', lambda: pytest.fail('loser recovered work'))
    monkeypatch.setattr(agent, 'tick', lambda: pytest.fail('loser claimed work'))
    run_worker(agent, threading.Event())


def test_stale_planner_result_cannot_publish(agent, monkeypatch):
    from video_agent.fake_planner import FakePlanner
    from video_agent.factory import AdapterBundle
    original = FakePlanner().plan
    class Planner:
        def plan(self, request):
            result = original(request)
            agent.clock.advance(61)
            with agent.db.transaction() as conn:
                conn.execute("UPDATE work_items SET lease_owner='new', lease_until='2026-10-14T00:05:00+00:00' WHERE task_id=:id", {'id': task.task_id})
            return result
    monkeypatch.setattr('video_agent.factory.AdapterResolver.for_snapshot', lambda *a, **kw: AdapterBundle(Planner(), agent.video, None, None, None))
    task = _create(agent)
    agent.tick()
    agent.tick()
    with agent.db.transaction(write=False) as conn:
        assert conn.execute('SELECT COUNT(*) AS n FROM plan_versions').fetchone()['n'] == 0
        assert conn.execute('SELECT lease_owner FROM work_items WHERE task_id=:id', {'id': task.task_id}).fetchone()['lease_owner'] == 'new'


def test_expired_generation_owner_cannot_write_any_outcome(agent, tmp_path):
    import json
    from video_agent import pipeline
    from video_agent.lease import LeaseLostError
    from video_agent.models import PlanDocument
    from video_agent.errors import VideoAgentError
    from video_agent.worker import _claim
    task = _create(agent)
    agent.tick()
    agent.tick()
    confirmed = agent.confirm(task.task_id, 1)
    with agent.db.transaction() as conn:
        work = _claim(conn, owner=agent.worker_owner, now_iso=agent.clock.now().isoformat(), lease_until='2026-10-14T00:01:00+00:00')
        generation = dict(conn.execute('SELECT * FROM generations WHERE generation_id=:id', {'id': confirmed.generation_id}).fetchone())
    execution = agent._for_work(work)
    document = PlanDocument.model_validate_json(generation['snapshot_json'])
    agent.clock.advance(61)
    source = tmp_path / 'candidate.mp4'
    source.write_bytes(b'candidate')
    destination = tmp_path / 'final.mp4'
    def snapshot():
        with agent.db.transaction(write=False) as conn:
            return {table: [dict(row) for row in conn.execute(f'SELECT * FROM {table}').fetchall()] for table in ('tasks', 'generation_shots', 'generations', 'artifacts', 'work_items')}
    before = snapshot()
    actions = [
        lambda: pipeline._enter(execution, work, 'submit_shot'),
        lambda: pipeline._advance(execution, work, 'export'),
        lambda: pipeline._mark_intended(execution, confirmed.generation_id, 's1', 'key'),
        lambda: pipeline._mark_submission(execution, confirmed.generation_id, 's1', 'accepted', 'remote', 'submitted'),
        lambda: pipeline._mark_imported(execution, confirmed.generation_id, 's1', 'final.mp4', source=source),
        lambda: pipeline._fail(execution, work, VideoAgentError('PROVIDER_REJECTED', 'late failure')),
        lambda: pipeline._register_artifact(execution, work, generation, document, destination, source=source),
    ]
    for action in actions:
        with pytest.raises(LeaseLostError):
            action()
    assert snapshot() == before
    assert source.read_bytes() == b'candidate' and not destination.exists()


def test_transaction_cannot_commit_after_lease_expires(agent):
    from video_agent.lease import owned_transaction, LeaseLostError
    from video_agent.worker import _claim
    task = _create(agent)
    with agent.db.transaction() as conn:
        work = _claim(conn, owner=agent.worker_owner, now_iso=agent.clock.now().isoformat(), lease_until='2026-10-14T00:01:00+00:00')
    with pytest.raises(LeaseLostError):
        with owned_transaction(agent.db, work['work_id'], agent.worker_owner, agent.clock) as conn:
            conn.execute("UPDATE tasks SET status='failed' WHERE task_id=:id", {'id': task.task_id})
            agent.clock.advance(61)
    assert agent.get_task(task.task_id).status == 'pending'


@pytest.mark.parametrize('takeover', [False, True])
def test_obsolete_runtime_cannot_claim_after_successful_renewal(agent, monkeypatch, takeover):
    from tests.test_review_fences import rows, files
    import video_agent.worker_main as worker_main
    _create(agent)
    entered, resume = threading.Event(), threading.Event()
    original = worker_main.renew_worker
    def delayed_renew(db, owner, clock):
        result = original(db, owner, clock)
        entered.set()
        assert resume.wait(10)
        return result
    monkeypatch.setattr(worker_main, 'renew_worker', delayed_renew)
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(worker_main.run_worker, agent, threading.Event())
        assert entered.wait(10)
        agent.clock.advance(31)
        if takeover:
            assert worker_main.acquire_worker(agent.db, 'replacement', agent.clock)
        before_rows, before_files = rows(agent), files(agent.home)
        resume.set()
        with pytest.raises(RuntimeError):
            future.result(timeout=10)
    assert rows(agent) == before_rows
    assert files(agent.home) == before_files
    if not takeover:
        assert worker_main.acquire_worker(agent.db, 'replacement', agent.clock)
    assert agent.tick(runtime_owner='replacement').did_work
    assert agent.tick(runtime_owner='replacement').did_work


def test_runtime_claim_waiting_on_mysql_takeover_uses_locked_current_lease(agent, monkeypatch):
    """Hold the singleton row while the stale claimant enters its real transaction."""
    from datetime import timedelta
    from video_agent.worker_main import acquire_worker
    from video_agent.db import Connection
    from video_agent.lease import LeaseLostError
    from tests.test_review_fences import rows, files
    _create(agent)
    assert acquire_worker(agent.db, 'old-runtime', agent.clock)
    waiting = threading.Event()
    original = Connection.execute
    def observe(conn, sql, parameters=None):
        if threading.current_thread().name.startswith('stale-claim') and 'SELECT * FROM worker_heartbeats' in sql:
            waiting.set()
        return original(conn, sql, parameters)
    monkeypatch.setattr(Connection, 'execute', observe)
    before_rows, before_files = rows(agent), files(agent.home)
    with ThreadPoolExecutor(1, thread_name_prefix='stale-claim') as pool:
        with agent.db.transaction() as conn:
            conn.execute(conn.for_update('SELECT * FROM worker_heartbeats WHERE singleton_id=1'))
            future = pool.submit(agent.tick, runtime_owner='old-runtime')
            assert waiting.wait(5)
            agent.clock.advance(31)
            conn.execute('UPDATE worker_heartbeats SET worker_owner=:owner, lease_until=:until WHERE singleton_id=1',
                         {'owner': 'replacement', 'until': (agent.clock.now() + timedelta(seconds=30)).isoformat()})
        with pytest.raises(LeaseLostError):
            future.result(timeout=10)
    assert rows(agent) == before_rows
    assert files(agent.home) == before_files
    assert agent.tick(runtime_owner='replacement').did_work


def test_runtime_expiry_after_claim_before_mysql_commit_rolls_back(agent, monkeypatch):
    from tests.test_review_fences import assert_runtime_expiry_before_claim_commit
    assert_runtime_expiry_before_claim_commit(agent, monkeypatch)


@pytest.mark.parametrize('stage', ['first_frame', 'export'])
def test_stale_mysql_stage_cannot_replace_published_files(agent, tmp_path, monkeypatch, stage):
    from tests.test_review_fences import assert_late_stage_cannot_replace_media_or_sidecars
    assert_late_stage_cannot_replace_media_or_sidecars(tmp_path, monkeypatch, stage, agent=agent)


@pytest.mark.parametrize('failure', ['expiry', 'sql_body'])
def test_mysql_import_publication_failure_restores_shot(agent, monkeypatch, failure):
    from tests.test_review_fences import assert_import_publication_failure_restores_shot
    assert_import_publication_failure_restores_shot(agent, monkeypatch, failure)


@pytest.mark.parametrize('detail', ['download', '正在导入镜头'])
@pytest.mark.parametrize('predecessor', ['imported', 'reused'])
def test_mysql_legacy_import_checkpoint_recovers(agent, monkeypatch, detail, predecessor):
    from tests.test_import_checkpoint import assert_legacy_import_checkpoint_recovers
    assert_legacy_import_checkpoint_recovers(agent, monkeypatch, detail, predecessor)


@pytest.mark.parametrize('detail', ['download', '正在导入镜头'])
def test_mysql_unproven_first_import_remains_uncertain(agent, monkeypatch, detail):
    from tests.test_import_checkpoint import assert_unproven_first_import_remains_uncertain
    assert_unproven_first_import_remains_uncertain(agent, monkeypatch, detail)


@pytest.mark.parametrize('crash', ['before_commit', 'after_commit'])
@pytest.mark.parametrize('last', [False, True])
def test_mysql_import_checkpoint_is_atomic(agent, monkeypatch, crash, last):
    from tests.test_import_checkpoint import assert_import_checkpoint_is_atomic
    assert_import_checkpoint_is_atomic(agent, monkeypatch, crash, last)
