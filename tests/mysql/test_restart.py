import io
import json
from datetime import datetime, timezone

import pytest
from PIL import Image
from cryptography.fernet import Fernet

from video_agent.config_store import ensure_settings_schema
from video_agent.settings_service import SettingsService
from video_agent.service import VideoAgent
from video_agent.models import CreateTaskRequest, VideoOptions


def test_fake_provider_query_survives_new_agent(mysql_database, tmp_path):
    from video_agent.provider_jobs import metadata
    metadata.create_all(mysql_database.engine)
    with mysql_database.transaction() as conn:
        conn.execute('DELETE FROM provider_jobs')
    from video_agent.config_store import metadata as settings_metadata, seed_default_settings
    with mysql_database.transaction() as conn:
        for table in reversed(settings_metadata.sorted_tables):
            conn.execute(f'DELETE FROM {table.name}')
        seed_default_settings(conn)
    settings = SettingsService(mysql_database, Fernet.generate_key())
    agent = VideoAgent(tmp_path, database=mysql_database, settings_service=settings)
    image = io.BytesIO()
    Image.new('RGB', (8, 8)).save(image, format='PNG')
    asset = agent.register_asset(filename='a.png', kind='image', data=image.getvalue())
    task = agent.create_task(CreateTaskRequest(product_info={'name': '杯子'}, images=[asset], first_frame_asset_id=asset, video=VideoOptions(duration_seconds=5, subtitles=False, voiceover=False, music=False)))
    agent.tick()
    agent.tick()
    agent.confirm(task.task_id, 1)
    # Avoid FFmpeg here: test the durable provider boundary directly using the
    # same confirmed work snapshot and a structural local request.
    from tests.mysql.test_provider_jobs import _request
    request = _request(tmp_path)
    accepted = agent.video.submit_shot(request)
    restarted = VideoAgent(tmp_path, database=mysql_database, settings_service=settings)
    assert restarted.video.query(external_task_id=accepted.external_task_id, provider_idempotency_key=request.provider_idempotency_key).state == 'succeeded'
    assert restarted.video.submit_shot(request).external_task_id == accepted.external_task_id
    assert not (tmp_path / 'config.json').exists()
    assert not (tmp_path / 'provider-state').exists()


@pytest.mark.parametrize('external_id,state', [('remote-A', 'accepted'), (None, 'uncertain')])
def test_worker_restores_original_provider_snapshot(mysql_database, tmp_path, monkeypatch, external_id, state):
    from video_agent.config_store import metadata as settings_metadata, seed_default_settings, ModelProfileCreate, ModelProfileUpdate, BindingsPatch, BindingTarget
    from video_agent.provider_jobs import metadata
    from video_agent.factory import REAL_ADAPTERS, AdapterSpec
    from video_agent.fake_planner import FakePlanner
    from video_agent.fake_video import FakeVideo
    from video_agent.config import SettingsPatch
    from video_agent.clock import FrozenClock
    from video_agent.ports import QueryResult
    metadata.create_all(mysql_database.engine)
    with mysql_database.transaction() as conn:
        conn.execute('DELETE FROM provider_jobs')
        for table in reversed(settings_metadata.sorted_tables):
            conn.execute(f'DELETE FROM {table.name}')
        seed_default_settings(conn)
    settings = SettingsService(mysql_database, Fernet.generate_key())
    planner = settings.create_profile(ModelProfileCreate(name='planner', role='planner', adapter='openai_chat', base_url='https://p.invalid', model='planner', secrets={'api_key': 'test-planner-key'}))
    video = settings.create_profile(ModelProfileCreate(name='video', role='video', adapter='seedance', base_url='https://a.invalid', model='A', secrets={'api_key': 'test-video-key'}))
    settings.set_bindings(BindingsPatch(planner=BindingTarget(profile_id=planner.profile_id), video=BindingTarget(profile_id=video.profile_id)), expected_version=settings.read_settings().configuration_revision)
    settings.save_settings(SettingsPatch(mode='real'), expected_version=settings.read_settings().configuration_revision)
    calls = []
    class Provider:
        def __init__(self, fields): self.model = fields['model']
        def capabilities(self): return FakeVideo().capabilities()
        def query(self, **kwargs):
            calls.append((self.model, kwargs['external_task_id']))
            return QueryResult(state='pending')
    monkeypatch.setitem(REAL_ADAPTERS, 'openai_chat', AdapterSpec(('model',), lambda fields, home: FakePlanner(), roles=('planner',)))
    monkeypatch.setitem(REAL_ADAPTERS, 'seedance', AdapterSpec(('model',), lambda fields, home: Provider(fields), roles=('video',)))
    clock = FrozenClock(datetime(2026, 10, 14, tzinfo=timezone.utc))
    agent = VideoAgent(tmp_path, database=mysql_database, settings_service=settings, clock=clock)
    image = io.BytesIO()
    Image.new('RGB', (8, 8)).save(image, format='PNG')
    asset = agent.register_asset(filename='a.png', kind='image', data=image.getvalue())
    task = agent.create_task(CreateTaskRequest(product_info={'name': '杯子'}, images=[asset], first_frame_asset_id=asset))
    agent.tick()
    agent.tick()
    confirmed = agent.confirm(task.task_id, 1)
    settings.update_profile(video.profile_id, ModelProfileUpdate(expected_version=1, model='B'))
    with mysql_database.transaction() as conn:
        conn.execute("UPDATE generation_shots SET external_task_id=:external, submission_state=:state, provider_idempotency_key='existing-key', status='submitted' WHERE generation_id=:id", {'external': external_id, 'state': state, 'id': confirmed.generation_id})
        conn.execute("UPDATE work_items SET step='import_shot', copied_config_json=:snapshot WHERE generation_id=:id", {'id': confirmed.generation_id, 'snapshot': json.dumps(settings.snapshot(conn=conn))})
    sentinel = object()
    agent.video = sentinel
    agent.tick()
    assert agent.video is sentinel
    clock.advance(2)
    restarted = VideoAgent(tmp_path, database=mysql_database, settings_service=settings, clock=clock)
    restarted.tick()
    assert calls == [('A', external_id), ('A', external_id)]
    assert restarted.video.model == 'B'
