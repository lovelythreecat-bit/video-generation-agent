"""Regressions for effective capabilities and delayed file/runtime owners."""
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.recovery_support import make_agent, open_plan, revise_two_shots
from video_agent import pipeline
from video_agent.lease import LeaseLostError
from video_agent.models import PlanChange, PlanPatch
from video_agent.worker import _claim


def rows(agent):
    with agent.db.transaction(write=False) as conn:
        return {table: [dict(row) for row in conn.execute(f'SELECT * FROM {table} ORDER BY 1, 2')]
                for table in ('tasks', 'work_items', 'generations', 'generation_shots', 'artifacts')}


def files(root):
    return {path.relative_to(root).as_posix(): path.read_bytes()
            for path in root.rglob('*') if path.is_file()}


def claim_generation(agent, generation_id, step):
    with agent.db.transaction() as conn:
        conn.execute('UPDATE work_items SET step=:step WHERE generation_id=:id', {'step': step, 'id': generation_id})
        work = _claim(conn, owner=agent.worker_owner, now_iso=agent.clock.now().isoformat(),
                      lease_until=(agent.clock.now() + timedelta(seconds=60)).isoformat())
    return work, agent._for_work(work)


@pytest.mark.parametrize('stage', ['first_frame', 'audio', 'audio_provider', 'music', 'music_trim', 'compose', 'visual', 'visual_frames', 'export_subtitles', 'export_mix', 'reuse_audio'])
def test_late_stage_cannot_replace_media_or_sidecars(tmp_path, monkeypatch, stage):
    assert_late_stage_cannot_replace_media_or_sidecars(tmp_path, monkeypatch, stage)


def assert_late_stage_cannot_replace_media_or_sidecars(tmp_path, monkeypatch, stage, *, agent=None):
    agent = agent or make_agent(tmp_path)
    task, _ = open_plan(agent)
    confirmed = agent.confirm(task.task_id, 1)
    generation_id = confirmed.generation_id
    root = pipeline._gen_dir(agent, {'task_id': task.task_id, 'generation_id': generation_id})
    root.mkdir(parents=True, exist_ok=True)
    (root / 'composed.mp4').write_bytes(b'accepted-picture')
    if stage == 'reuse_audio':
        base = tmp_path / 'base'
        base.mkdir()
        for name in ('voice.m4a', 'music.m4a', 'cues.json'):
            (base / name).write_bytes(b'base')
        monkeypatch.setattr(pipeline, '_base_generation_dir', lambda *_: base)
    step = ('export' if stage.startswith('export') else 'audio' if stage in {'reuse_audio', 'audio_provider', 'music', 'music_trim'}
            else 'visual' if stage == 'visual_frames' else stage)
    with agent.db.transaction() as conn:
        generation = conn.execute('SELECT * FROM generations WHERE generation_id=:id', {'id': generation_id}).fetchone()
        document = json.loads(generation['snapshot_json'])
        document['music'] = stage in {'music', 'music_trim'}
        document['voiceover'] = stage not in {'music', 'music_trim'}
        conn.execute('UPDATE generations SET snapshot_json=:doc, kind=:kind WHERE generation_id=:id',
                     {'doc': json.dumps(document), 'kind': 'shots' if stage == 'reuse_audio' else 'full', 'id': generation_id})
    work, execution = claim_generation(agent, generation_id, step)
    entered, resume = threading.Event(), threading.Event()
    info = SimpleNamespace(frame_count=150, has_audio=False)
    monkeypatch.setattr(pipeline, 'probe', lambda *_: info)
    monkeypatch.setattr(pipeline, '_generation_clips', lambda *_: [])
    monkeypatch.setattr(pipeline, '_assert_final', lambda *_: None)

    def write(dest, data=b'old-media', sidecar=None):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        if sidecar:
            dest.with_suffix(sidecar).write_bytes(b'old-sidecar')

    def pause():
        entered.set()
        assert resume.wait(10), 'test did not release delayed writer'

    def delayed_write(dest, sidecar=None):
        pause()
        write(dest, sidecar=sidecar)

    monkeypatch.setattr(pipeline, 'fit_image', lambda *args: delayed_write(args[-1]))
    monkeypatch.setattr(pipeline, '_atempo_pad', lambda src, speed, seconds, dest, bitrate: write(dest))
    monkeypatch.setattr(pipeline, '_concat_audio', lambda pieces, dest, bitrate: delayed_write(dest, '.concat.txt') if stage == 'audio' else write(dest, sidecar='.concat.txt'))
    monkeypatch.setattr(pipeline.media, 'concat_clips', lambda clips, dest, **kw: delayed_write(dest, '.concat.txt'))
    monkeypatch.setattr(pipeline, 'extract_png', lambda src, frame, dest: delayed_write(dest) if stage == 'visual_frames' else write(dest))
    execution.voice = SimpleNamespace(synthesize=lambda **kw: ((pause() if stage == 'audio_provider' else None) or SimpleNamespace(audio=b'raw', duration_ms=1000, cues=[])))
    execution.music = SimpleNamespace(generate=lambda **kw: ((pause() if stage == 'music' else None) or b'raw-music'))
    monkeypatch.setattr(pipeline, 'trim_or_loop_audio', lambda src, seconds, dest, **kw: delayed_write(dest) if stage == 'music_trim' else write(dest))
    execution.visual = SimpleNamespace(check=lambda *args: (pause() or pipeline.VisualResult(status='passed')))

    def burn(src, cues, width, height, dest, **kw):
        if stage == 'export_subtitles':
            pause()
        write(dest, sidecar='.ass')

    def mix(src, voice, music, cues, dest, **kw):
        if stage == 'export_mix':
            pause()
        write(dest, sidecar='.wav')

    monkeypatch.setattr(pipeline, 'burn_subtitles', burn)
    monkeypatch.setattr(pipeline, 'mix_audio', mix)
    if stage == 'reuse_audio':
        copy = pipeline.shutil.copyfile
        def delayed_copy(src, dest):
            if Path(src).parent == base:
                pause()
            return copy(src, dest)
        monkeypatch.setattr(pipeline.shutil, 'copyfile', delayed_copy)

    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(getattr(pipeline, '_step_' + step), execution, work)
        assert entered.wait(10)
        agent.clock.advance(61)
        with agent.db.transaction() as conn:
            conn.execute("UPDATE work_items SET lease_owner='replacement', lease_until=:until, step='export' WHERE work_id=:id",
                         {'until': (agent.clock.now() + timedelta(seconds=60)).isoformat(), 'id': work['work_id']})
            conn.execute("UPDATE generations SET stage_detail='replacement-published' WHERE generation_id=:id", {'id': generation_id})
        names = ['first_frame.png', 'first_frame_fitted.png', 'voice-s1.m4a', 'voice-s1.raw.m4a',
                 'voice.m4a', 'voice.concat.txt', 'music.m4a', 'music.raw.m4a', 'cues.json',
                 'composed.mp4', 'composed.concat.txt', 'quality.json', 'subtitled.mp4', 'subtitled.ass',
                 'final.mp4', 'frames/s1-start.png', 'frames/s1-middle.png', 'frames/s1-end.png']
        for name in names:
            dest = root / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b'{"status":"flagged","findings":[],"reason":"replacement"}' if name == 'quality.json' else b'replacement-' + name.encode())
        before_rows, before_files = rows(agent), files(root)
        resume.set()
        with pytest.raises(LeaseLostError):
            future.result(timeout=10)
    assert rows(agent) == before_rows
    assert files(root) == before_files


@pytest.mark.parametrize('operation', ['reuse_shots', 'neighbor_frames'])
def test_late_reuse_and_neighbor_writers_are_private(tmp_path, monkeypatch, operation):
    from video_agent.models import PlanDocument
    agent = make_agent(tmp_path)
    task, _ = open_plan(agent)
    revise_two_shots(agent, task.task_id)
    confirmed = agent.confirm(task.task_id, 2)
    work, execution = claim_generation(agent, confirmed.generation_id, 'first_frame')
    generation = pipeline._require_generation(execution, confirmed.generation_id)
    document = PlanDocument.model_validate_json(generation['snapshot_json'])
    base = tmp_path / 'base'
    (base / 'shots').mkdir(parents=True)
    for name in ('s1', 's2'):
        (base / 'shots' / (name + '.mp4')).write_bytes(b'base-shot')
    root = pipeline._gen_dir(agent, work)
    (root / 'shots').mkdir(parents=True)
    (root / 'shots/s1.mp4').write_bytes(b'accepted-shot')
    monkeypatch.setattr(pipeline, '_base_generation_dir', lambda *_: base)
    entered, resume = threading.Event(), threading.Event()
    def pause():
        entered.set()
        assert resume.wait(10)
    if operation == 'reuse_shots':
        with agent.db.transaction() as conn:
            conn.execute('DELETE FROM generation_shots WHERE generation_id=:id', {'id': confirmed.generation_id})
        copy = pipeline.shutil.copyfile
        def delayed_copy(source, dest):
            pause()
            return copy(source, dest)
        monkeypatch.setattr(pipeline.shutil, 'copyfile', delayed_copy)
        action = lambda: pipeline._reuse_base_shots(execution, work, generation, document)
    else:
        monkeypatch.setattr(pipeline, 'probe', lambda *_: SimpleNamespace(frame_count=75))
        def extract(source, frame, dest):
            pause()
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b'old-neighbor')
        monkeypatch.setattr(pipeline, 'extract_png', extract)
        action = lambda: pipeline._shot_request(execution, work, {'shot_id': 's2', 'start_frame': 75, 'end_frame': 150, 'provider_idempotency_key': None}, document)
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(action)
        assert entered.wait(10)
        agent.clock.advance(61)
        with agent.db.transaction() as conn:
            conn.execute("UPDATE work_items SET lease_owner='replacement', lease_until=:until WHERE work_id=:id",
                         {'until': (agent.clock.now() + timedelta(seconds=60)).isoformat(), 'id': work['work_id']})
        (root / 'frames').mkdir(exist_ok=True)
        (root / 'frames/s1-taken-end.png').write_bytes(b'replacement-frame')
        (root / 'shots/s2.mp4').write_bytes(b'replacement-shot')
        before_rows, before_files = rows(agent), files(root)
        resume.set()
        if operation == 'reuse_shots':
            with pytest.raises(LeaseLostError):
                future.result(timeout=10)
        else:
            future.result(timeout=10)
    assert rows(agent) == before_rows
    assert files(root) == before_files


@pytest.mark.parametrize('replacement', [False, True])
def test_runtime_losing_singleton_after_renewal_cannot_claim(tmp_path, monkeypatch, replacement):
    from video_agent.provider_jobs import ensure_worker_schema
    from video_agent.worker_main import acquire_worker, run_worker
    import video_agent.worker_main as worker_main
    agent = make_agent(tmp_path)
    ensure_worker_schema(agent.db)
    task, _ = open_plan(agent)
    agent.confirm(task.task_id, 1)
    entered, resume = threading.Event(), threading.Event()
    renew = worker_main.renew_worker

    def delayed_renew(db, owner, clock):
        result = renew(db, owner, clock)
        entered.set()
        assert resume.wait(10)
        return result

    monkeypatch.setattr(worker_main, 'renew_worker', delayed_renew)
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(run_worker, agent, threading.Event())
        assert entered.wait(10)
        agent.clock.advance(31)
        if replacement:
            assert acquire_worker(agent.db, 'replacement-runtime', agent.clock)
        before_rows, before_files = rows(agent), files(agent.home / 'tasks')
        resume.set()
        with pytest.raises(RuntimeError):
            future.result(timeout=10)
    assert rows(agent) == before_rows
    assert files(agent.home / 'tasks') == before_files
    assert agent.tick().did_work  # explicit unmanaged core ticks remain usable


def test_structured_revision_uses_current_capabilities_without_reloading(tmp_path):
    from tests.video_agent.test_snapshot_adapters import _service, _profile
    from video_agent.config import SettingsPatch
    from video_agent.config_store import BindingsPatch, BindingTarget, ModelProfileUpdate
    from video_agent.service import VideoAgent
    from tests.recovery_support import png_bytes
    from video_agent.models import CreateTaskRequest
    service = _service(tmp_path)
    agent = VideoAgent(tmp_path, database=service.database, settings_service=service)
    image = agent.register_asset(filename='a.png', kind='image', data=png_bytes())
    task = agent.create_task(CreateTaskRequest(images=[image], product_info={'name': '杯子'}))
    agent.tick()
    agent.tick()
    queued = agent.create_task(CreateTaskRequest(images=[image], product_info={'name': '旧配置'}))
    with agent.db.transaction(write=False) as conn:
        old_work = conn.execute('SELECT * FROM work_items WHERE task_id=:id', {'id': queued.task_id}).fetchone()
    startup_video = agent.video
    planner = _profile(service)
    video = _profile(service, role='video', adapter='seedance')
    service.set_bindings(BindingsPatch(planner=BindingTarget(profile_id=planner.profile_id), video=BindingTarget(profile_id=video.profile_id)),
                         expected_version=service.read_settings().configuration_revision)
    service.save_settings(SettingsPatch(mode='real'), expected_version=service.read_settings().configuration_revision)
    agent.revise_plan(task.task_id, PlanChange(patch=PlanPatch(aspect_ratio='16:9')))
    assert agent.get_plan(task.task_id, 2).document.fit == 'native'
    agent.revise_plan(task.task_id, PlanChange(patch=PlanPatch(aspect_ratio='4:5')))
    assert agent.get_plan(task.task_id, 3).document.fit == 'pad'
    service.update_profile(video.profile_id, ModelProfileUpdate(expected_version=1, adapter='wan', parameters={'max_segment_seconds': '5'}))
    agent.revise_plan(task.task_id, PlanChange(patch=PlanPatch(aspect_ratio='4:5')))
    assert agent.get_plan(task.task_id, 4).document.fit == 'native'
    agent.revise_plan(task.task_id, PlanChange(patch=PlanPatch(aspect_ratio='16:9', fit='crop')))
    assert agent.get_plan(task.task_id, 5).document.fit == 'crop'
    assert agent.video is startup_video
    assert agent._for_work(old_work).video.capabilities().native_aspects == ['9:16']


def test_interrupted_first_frame_is_not_reused_on_retry(tmp_path, monkeypatch):
    agent = make_agent(tmp_path)
    task, _ = open_plan(agent)
    generation = agent.confirm(task.task_id, 1)
    work, execution = claim_generation(agent, generation.generation_id, 'first_frame')
    root = pipeline._gen_dir(agent, work)
    def interrupt(source, fit, width, height, dest):
        dest.write_bytes(b'incomplete')
        raise RuntimeError('interrupted local writer')
    monkeypatch.setattr(pipeline, 'fit_image', interrupt)
    with pytest.raises(RuntimeError):
        pipeline._step_first_frame(execution, work)
    assert not (root / 'first_frame_fitted.png').exists()
    def complete(source, fit, width, height, dest):
        dest.write_bytes(b'complete')
    monkeypatch.setattr(pipeline, 'fit_image', complete)
    pipeline._step_first_frame(execution, work)
    assert (root / 'first_frame_fitted.png').read_bytes() == b'complete'


def test_publication_expiry_restores_files_before_unlock(tmp_path, monkeypatch):
    agent = make_agent(tmp_path)
    task, _ = open_plan(agent)
    generation = agent.confirm(task.task_id, 1)
    work, execution = claim_generation(agent, generation.generation_id, 'first_frame')
    root = pipeline._gen_dir(agent, work)
    root.mkdir(parents=True, exist_ok=True)
    destination = root / 'first_frame_fitted.png'
    destination.write_bytes(b'previous-accepted')
    monkeypatch.setattr(pipeline, 'fit_image', lambda *args: args[-1].write_bytes(b'candidate'))
    replace = pipeline.os.replace
    expired = False
    def expire_on_replace(source, dest):
        nonlocal expired
        replace(source, dest)
        if Path(dest) == destination and not expired:
            expired = True
            agent.clock.advance(61)
    monkeypatch.setattr(pipeline.os, 'replace', expire_on_replace)
    before = rows(agent)
    with pytest.raises(LeaseLostError):
        pipeline._step_first_frame(execution, work)
    assert destination.read_bytes() == b'previous-accepted'
    # Entering the stage is allowed; publication must leave the step unchanged.
    assert rows(agent)['work_items'] == before['work_items']


def assert_runtime_expiry_before_claim_commit(agent, monkeypatch):
    from video_agent.worker_main import acquire_worker
    from video_agent.provider_jobs import ensure_worker_schema
    from video_agent.db import Connection
    from tests.recovery_support import png_bytes
    from video_agent.models import CreateTaskRequest
    if agent.db.dialect_name == 'sqlite':
        ensure_worker_schema(agent.db)
    image = agent.register_asset(filename='a.png', kind='image', data=png_bytes())
    agent.create_task(CreateTaskRequest(images=[image], product_info={'name': '杯子'}))
    assert acquire_worker(agent.db, 'runtime', agent.clock)
    entered, resume = threading.Event(), threading.Event()
    execute = Connection.execute
    def delay(conn, sql, parameters=None):
        if "UPDATE tasks SET status='planning'" in sql:
            entered.set()
            assert resume.wait(10)
        return execute(conn, sql, parameters)
    monkeypatch.setattr(Connection, 'execute', delay)
    before = rows(agent)
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(agent.tick, runtime_owner='runtime')
        assert entered.wait(10)
        agent.clock.advance(31)
        resume.set()
        with pytest.raises(LeaseLostError):
            future.result(timeout=10)
    assert rows(agent) == before


def test_runtime_expiry_after_claim_rolls_back_entire_transaction(tmp_path, monkeypatch):
    assert_runtime_expiry_before_claim_commit(make_agent(tmp_path), monkeypatch)


def assert_import_publication_failure_restores_shot(agent, monkeypatch, failure):
    """A rename is not accepted until the imported-shot SQL transaction succeeds."""
    from video_agent.db import Connection
    task, _ = open_plan(agent)
    generation = agent.confirm(task.task_id, 1)
    work, execution = claim_generation(agent, generation.generation_id, 'import_shot')
    root = pipeline._gen_dir(agent, work)
    destination = root / 'shots' / 's1.mp4'
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(b'previous-accepted-shot')
    source = pipeline._scratch_dir(execution, work) / 's1.mp4'
    source.write_bytes(b'unaccepted-candidate-shot')
    relative = destination.relative_to(agent.files.home).as_posix()
    before_rows, before_files = rows(agent), files(root)

    if failure == 'expiry':
        replace = pipeline.os.replace
        expired = False
        def expire_after_publish(src, dest):
            nonlocal expired
            replace(src, dest)
            if Path(dest) == destination and not expired:
                expired = True
                agent.clock.advance(61)
        monkeypatch.setattr(pipeline.os, 'replace', expire_after_publish)
        expected = LeaseLostError
    else:
        execute = Connection.execute
        def fail_after_shot_update(conn, sql, parameters=None):
            result = execute(conn, sql, parameters)
            if "SET status='imported', clip_path=" in sql:
                raise RuntimeError('injected imported-shot transaction failure')
            return result
        monkeypatch.setattr(Connection, 'execute', fail_after_shot_update)
        expected = RuntimeError

    with pytest.raises(expected):
        pipeline._mark_imported(execution, generation.generation_id, 's1', relative, source=source)
    assert rows(agent) == before_rows
    assert files(root) == before_files
    assert source.is_file() and source.read_bytes() == b'unaccepted-candidate-shot'


@pytest.mark.parametrize('failure', ['expiry', 'sql_body'])
def test_import_publication_failure_restores_shot(tmp_path, monkeypatch, failure):
    assert_import_publication_failure_restores_shot(make_agent(tmp_path), monkeypatch, failure)
