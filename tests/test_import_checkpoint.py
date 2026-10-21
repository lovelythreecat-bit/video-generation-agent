"""Import checkpoints must survive both old residue and new transaction crashes."""
from types import SimpleNamespace

import pytest

from tests.recovery_support import capabilities, make_agent, open_plan, revise_two_shots
from tests.test_review_fences import claim_generation, files, rows
from video_agent import pipeline
from video_agent.db import Connection
from video_agent.ports import QueryResult, SubmitResult
from video_agent.service import VideoAgent


class RecoveryVideo:
    def __init__(self):
        self.events = []

    def capabilities(self):
        return capabilities()

    def submit_shot(self, request):
        self.events.append(('submit', request.shot_id))
        return SubmitResult(external_task_id='remote-' + request.shot_id, uncertain=False)

    def query(self, *, external_task_id, provider_idempotency_key):
        self.events.append(('query', external_task_id))
        return QueryResult(state='succeeded')

    def download(self, external_task_id):
        self.events.append(('download', external_task_id))
        assert external_task_id is not None, 'import tried to download a new shot without submission evidence'
        return ('clip-' + external_task_id).encode()


def prepare(agent, monkeypatch, *, two_shots=True):
    video = RecoveryVideo()
    agent.video = video
    if agent.settings_service is not None:
        from video_agent.factory import AdapterResolver
        resolve = AdapterResolver.for_snapshot
        monkeypatch.setattr(AdapterResolver, 'for_snapshot',
                            lambda self, *args, **kwargs: resolve(self, *args, **kwargs)._replace(video=video))
    task, _ = open_plan(agent)
    if two_shots:
        revise_two_shots(agent, task.task_id)
    confirmed = agent.confirm(task.task_id, 2 if two_shots else 1)
    root = pipeline._gen_dir(agent, {'task_id': task.task_id, 'generation_id': confirmed.generation_id})
    (root / 'shots').mkdir(parents=True)
    monkeypatch.setattr(pipeline, 'probe', lambda *_: SimpleNamespace(frame_count=75 if two_shots else 150, has_audio=False))
    def frame(source, frame_index, destination):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b'neighbor-frame')
    monkeypatch.setattr(pipeline, 'extract_png', frame)
    return confirmed, root, video


def restart(agent, video):
    # A fresh worker owner exercises lease recovery and snapshot reconstruction.
    resumed = VideoAgent(agent.home, database=agent.db, settings_service=agent.settings_service,
                         clock=agent.clock, planner=agent.planner, video=video)
    agent.clock.advance(61)
    resumed.recover()
    return resumed


def reach_export(agent, generation_id):
    for _ in range(8):
        with agent.db.transaction(write=False) as conn:
            step = conn.execute('SELECT step FROM work_items WHERE generation_id=:id', {'id': generation_id}).fetchone()['step']
        if step == 'export' or agent.get_generation(generation_id).status == 'failed':
            return step
        agent.tick()
    raise AssertionError('import recovery did not reach export')


def assert_legacy_import_checkpoint_recovers(agent, monkeypatch, detail, predecessor):
    generation, root, video = prepare(agent, monkeypatch)
    first = root / 'shots/s1.mp4'
    first.write_bytes(b'accepted-s1')
    relative = first.relative_to(agent.home).as_posix()
    with agent.db.transaction() as conn:
        conn.execute('UPDATE generation_shots SET status=:status, clip_path=:path WHERE generation_id=:id AND shot_id=:shot',
                     {'status': predecessor, 'path': relative, 'id': generation.generation_id, 'shot': 's1'})
        if predecessor == 'imported':
            conn.execute("UPDATE generation_shots SET provider_idempotency_key='key-s1', external_task_id='remote-s1', submission_state='accepted' WHERE generation_id=:id AND shot_id='s1'",
                         {'id': generation.generation_id})
        conn.execute("UPDATE work_items SET step='import_shot', lease_owner='crashed', lease_until=:now WHERE generation_id=:id",
                     {'now': agent.clock.now().isoformat(), 'id': generation.generation_id})
        conn.execute('UPDATE generations SET stage_detail=:detail WHERE generation_id=:id', {'detail': detail, 'id': generation.generation_id})
        before_first = dict(conn.execute("SELECT * FROM generation_shots WHERE generation_id=:id AND shot_id='s1'", {'id': generation.generation_id}).fetchone())
    resumed = restart(agent, video)
    assert reach_export(resumed, generation.generation_id) == 'export'
    with agent.db.transaction(write=False) as conn:
        first_row = dict(conn.execute("SELECT * FROM generation_shots WHERE generation_id=:id AND shot_id='s1'", {'id': generation.generation_id}).fetchone())
        second = conn.execute("SELECT * FROM generation_shots WHERE generation_id=:id AND shot_id='s2'", {'id': generation.generation_id}).fetchone()
    assert first_row == before_first
    assert first.read_bytes() == b'accepted-s1'
    assert second['status'] == 'imported'
    assert (root / 'shots/s2.mp4').read_bytes() == b'clip-remote-s2'
    assert video.events == [('submit', 's2'), ('query', 'remote-s2'), ('download', 'remote-s2')]


@pytest.mark.parametrize('detail', ['download', '正在导入镜头'])
@pytest.mark.parametrize('predecessor', ['imported', 'reused'])
def test_legacy_import_checkpoint_recovers(tmp_path, monkeypatch, detail, predecessor):
    assert_legacy_import_checkpoint_recovers(make_agent(tmp_path), monkeypatch, detail, predecessor)


def assert_unproven_first_import_remains_uncertain(agent, monkeypatch, detail):
    generation, root, video = prepare(agent, monkeypatch)
    with agent.db.transaction() as conn:
        conn.execute("UPDATE work_items SET step='import_shot' WHERE generation_id=:id", {'id': generation.generation_id})
        conn.execute('UPDATE generations SET stage_detail=:detail WHERE generation_id=:id', {'detail': detail, 'id': generation.generation_id})
    before_files = files(root)
    agent.tick()
    view = agent.get_task(generation.task_id)
    assert view.failure is not None and view.failure.code == 'PROVIDER_SUBMISSION_UNCERTAIN'
    assert video.events == []
    assert files(root) == before_files


@pytest.mark.parametrize('detail', ['download', '正在导入镜头'])
def test_unproven_first_import_remains_uncertain(tmp_path, monkeypatch, detail):
    assert_unproven_first_import_remains_uncertain(make_agent(tmp_path), monkeypatch, detail)


def assert_import_checkpoint_is_atomic(agent, monkeypatch, crash, last):
    generation, root, video = prepare(agent, monkeypatch, two_shots=not last)
    work, execution = claim_generation(agent, generation.generation_id, 'import_shot')
    with agent.db.transaction() as conn:
        conn.execute("UPDATE generation_shots SET status='submitted', provider_idempotency_key='key-s1', external_task_id='remote-s1', submission_state='accepted' WHERE generation_id=:id AND shot_id='s1'",
                     {'id': generation.generation_id})
        conn.execute("UPDATE generations SET stage_detail='download' WHERE generation_id=:id", {'id': generation.generation_id})
    first = root / 'shots/s1.mp4'
    first.write_bytes(b'previous-accepted-s1')
    checkpoint = {}
    original_mark = pipeline._mark_imported
    execute = Connection.execute
    def observe_mark(*args, **kwargs):
        checkpoint['rows'], checkpoint['files'] = rows(agent), files(root)
        checkpoint['source'] = kwargs['source']
        result = original_mark(*args, **kwargs)
        if crash == 'after_commit':
            raise SystemExit('crash immediately after imported checkpoint commit')
        return result
    def crash_step_update(conn, sql, parameters=None):
        result = execute(conn, sql, parameters)
        if sql.startswith('UPDATE work_items') and 'SET step=' in sql:
            raise SystemExit('crash after next-step SQL inside checkpoint')
        return result
    with monkeypatch.context() as local:
        local.setattr(pipeline, '_mark_imported', observe_mark)
        if crash == 'before_commit':
            local.setattr(Connection, 'execute', crash_step_update)
        with pytest.raises(SystemExit):
            pipeline._step_import_shot(execution, work)
    if crash == 'before_commit':
        assert rows(agent) == checkpoint['rows']
        assert files(root) == checkpoint['files']
        assert checkpoint['source'].read_bytes() == b'clip-remote-s1'
        return
    with agent.db.transaction(write=False) as conn:
        saved_work = conn.execute('SELECT * FROM work_items WHERE work_id=:id', {'id': work['work_id']}).fetchone()
        shot = conn.execute("SELECT * FROM generation_shots WHERE generation_id=:id AND shot_id='s1'", {'id': generation.generation_id}).fetchone()
    assert shot['status'] == 'imported' and first.read_bytes() == b'clip-remote-s1'
    assert saved_work['step'] == ('export' if last else 'submit_shot')
    assert saved_work['lease_owner'] is None and saved_work['lease_until'] is None
    resumed = restart(agent, video)
    assert reach_export(resumed, generation.generation_id) == 'export'
    assert first.read_bytes() == b'clip-remote-s1'
    expected = [('download', 'remote-s1')]
    if not last:
        expected += [('submit', 's2'), ('query', 'remote-s2'), ('download', 'remote-s2')]
    assert video.events == expected


@pytest.mark.parametrize('crash', ['before_commit', 'after_commit'])
@pytest.mark.parametrize('last', [False, True])
def test_import_checkpoint_is_atomic(tmp_path, monkeypatch, crash, last):
    assert_import_checkpoint_is_atomic(make_agent(tmp_path), monkeypatch, crash, last)
