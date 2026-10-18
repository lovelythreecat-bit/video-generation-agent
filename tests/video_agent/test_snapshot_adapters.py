import json

import pytest
from cryptography.fernet import Fernet

from video_agent.config import AdapterSetting, SettingsPatch
from video_agent.db import Database
from video_agent.errors import VideoAgentError


def _service(tmp_path):
    from video_agent.config_store import ensure_settings_schema
    from video_agent.settings_service import SettingsService
    database = Database(tmp_path)
    ensure_settings_schema(database)
    return SettingsService(database, Fernet.generate_key())


def _profile(service, **changes):
    from video_agent.config_store import ModelProfileCreate
    data = dict(name='文本 A', role='planner', adapter='openai_chat',
                base_url='https://a.invalid/v1', model='model-A', secrets={'api_key': 'test-credential-A'})
    data.update(changes)
    return service.create_profile(ModelProfileCreate(**data))


def test_profile_crud_and_role_binding(tmp_path):
    from video_agent.config_store import BindingsPatch, BindingTarget, ModelProfileUpdate
    service = _service(tmp_path)
    profile = _profile(service)
    assert profile.has_api_key
    assert 'test-credential-A' not in profile.model_dump_json()
    revision = service.read_bindings().configuration_revision
    binding = service.set_bindings(BindingsPatch(planner=BindingTarget(profile_id=profile.profile_id)), expected_version=revision)
    assert binding.planner.profile_id == profile.profile_id
    with pytest.raises(VideoAgentError) as caught:
        service.delete_profile(profile.profile_id, expected_version=profile.version)
    assert caught.value.code == 'CONFLICT'
    with pytest.raises(VideoAgentError):
        service.update_profile(profile.profile_id, ModelProfileUpdate(expected_version=profile.version, enabled=False))
    service.set_bindings(BindingsPatch(planner=BindingTarget(adapter='fake')), expected_version=binding.configuration_revision)
    service.delete_profile(profile.profile_id, expected_version=profile.version)
    with pytest.raises(VideoAgentError) as missing:
        service.get_profile(profile.profile_id)
    assert missing.value.code == 'NOT_FOUND'


def test_invalid_real_mode_rolls_back_and_version_conflict(tmp_path):
    service = _service(tmp_path)
    before = service.read_settings()
    with pytest.raises(VideoAgentError):
        service.save_settings(SettingsPatch(mode='real'), expected_version=before.configuration_revision)
    assert service.read_settings() == before
    service.save_settings(SettingsPatch(default_duration=15), expected_version=before.configuration_revision)
    with pytest.raises(VideoAgentError) as caught:
        service.save_settings(SettingsPatch(default_duration=5), expected_version=before.configuration_revision)
    assert caught.value.code == 'CONFLICT'
    assert service.read_settings().default_duration == 15


def test_blank_secret_keeps_clear_secret_removes(tmp_path):
    from video_agent.config_store import BindingsPatch, BindingTarget, ModelProfileUpdate
    service = _service(tmp_path)
    profile = _profile(service)
    kept = service.update_profile(profile.profile_id, ModelProfileUpdate(expected_version=1, secrets={'api_key': ''}))
    assert kept.has_api_key
    with pytest.raises(VideoAgentError):
        service.update_profile(profile.profile_id, ModelProfileUpdate(expected_version=kept.version, secrets={'api_key': ''}, clear_secrets=['api_key']))
    cleared = service.update_profile(profile.profile_id, ModelProfileUpdate(expected_version=kept.version, clear_secrets=['api_key']))
    assert not cleared.has_api_key
    revision = service.read_settings().configuration_revision
    with pytest.raises(VideoAgentError):
        service.set_bindings(BindingsPatch(video=BindingTarget(profile_id=profile.profile_id)), expected_version=revision)
    assert service.read_settings().configuration_revision == revision


def test_credentials_encrypted_in_revisions_and_snapshots(tmp_path):
    from video_agent.config_store import BindingsPatch, BindingTarget
    from video_agent.secrets import SnapshotCodec
    service = _service(tmp_path)
    profile = _profile(service)
    service.set_bindings(BindingsPatch(planner=BindingTarget(profile_id=profile.profile_id)), expected_version=service.read_settings().configuration_revision)
    snapshot = service.snapshot()
    assert 'test-credential-A' not in json.dumps(snapshot)
    with service.database.transaction(write=False) as conn:
        rows = conn.execute('SELECT * FROM model_profile_revisions').fetchall()
    assert 'test-credential-A' not in str([dict(row) for row in rows])
    assert service.codec.decode(snapshot).adapters['planner'].field_map()['api_key'] == 'test-credential-A'
    with pytest.raises(VideoAgentError) as caught:
        SnapshotCodec(Fernet.generate_key()).decode(snapshot)
    assert 'test-credential-A' not in str(caught.value)
    assert 'test-credential-A' not in repr(service.read_effective_config())


def test_inflight_job_uses_snapshot_after_config_change(tmp_path, monkeypatch):
    from video_agent.config_store import BindingsPatch, BindingTarget, ModelProfileUpdate
    from video_agent.factory import AdapterResolver, REAL_ADAPTERS, AdapterSpec
    service = _service(tmp_path)
    first = _profile(service)
    video = _profile(service, name='视频', role='video', adapter='seedance')
    service.set_bindings(BindingsPatch(planner=BindingTarget(profile_id=first.profile_id), video=BindingTarget(profile_id=video.profile_id)), expected_version=service.read_settings().configuration_revision)
    service.save_settings(SettingsPatch(mode='real'), expected_version=service.read_settings().configuration_revision)
    original = service.snapshot()
    class LocalProvider:
        def __init__(self, fields): self.model = fields['model']
        def query(self, task_id): return f'{self.model}:{task_id}'
        def download(self, task_id): return f'{self.model}:download:{task_id}'
    monkeypatch.setitem(REAL_ADAPTERS, 'seedance', AdapterSpec(('model',), lambda fields, home: LocalProvider(fields), roles=('video',)))
    resolver = AdapterResolver(service.codec)
    inflight = resolver.for_snapshot(original, tmp_path)
    service.update_profile(video.profile_id, ModelProfileUpdate(expected_version=video.version, model='model-B'))
    fresh = resolver.for_snapshot(service.snapshot(), tmp_path)
    assert fresh.video.query('existing-task') == 'model-B:existing-task'
    assert inflight.video.query('existing-task') == 'model-A:existing-task'
    assert resolver.for_snapshot(original, tmp_path).video.download('existing-task') == 'model-A:download:existing-task'
    assert inflight.video is not fresh.video


def test_profile_idempotency_is_secret_safe(tmp_path):
    from video_agent.config_store import ModelProfileCreate
    service = _service(tmp_path)
    data = ModelProfileCreate(name='文本', role='planner', adapter='openai_chat', secrets={'api_key': 'idempotency-credential'})
    first = service.create_profile(data, idempotency_key='create-profile')
    assert service.create_profile(data, idempotency_key='create-profile').profile_id == first.profile_id
    with pytest.raises(VideoAgentError):
        service.create_profile(data.model_copy(update={'name': 'different'}), idempotency_key='create-profile')
    assert service.list_profiles(limit=20, offset=0).total == 1


def test_failed_binding_batch_and_real_credential_clear_are_atomic(tmp_path):
    from video_agent.config_store import BindingsPatch, BindingTarget, ModelProfileUpdate
    service = _service(tmp_path)
    planner = _profile(service)
    video = _profile(service, role='video', adapter='seedance')
    original = service.read_bindings()
    with pytest.raises(VideoAgentError):
        service.set_bindings(BindingsPatch(planner=BindingTarget(profile_id=planner.profile_id), video=BindingTarget(profile_id=planner.profile_id)), expected_version=original.configuration_revision)
    assert service.read_bindings() == original
    service.set_bindings(BindingsPatch(planner=BindingTarget(profile_id=planner.profile_id), video=BindingTarget(profile_id=video.profile_id)), expected_version=original.configuration_revision)
    service.save_settings(SettingsPatch(mode='real'), expected_version=service.read_settings().configuration_revision)
    before = service.read_settings()
    with pytest.raises(VideoAgentError):
        service.update_profile(video.profile_id, ModelProfileUpdate(expected_version=1, clear_secrets=['api_key']))
    assert service.get_profile(video.profile_id).version == 1
    assert service.get_profile(video.profile_id).has_api_key
    assert service.read_settings() == before
    with service.database.transaction(write=False) as conn:
        assert conn.execute('SELECT COUNT(*) AS n FROM model_profile_revisions WHERE profile_id=:id', {'id': video.profile_id}).fetchone()['n'] == 1


def test_unbound_soft_delete_preserves_original_snapshot(tmp_path):
    from video_agent.config_store import BindingsPatch, BindingTarget
    service = _service(tmp_path)
    planner = _profile(service)
    service.set_bindings(BindingsPatch(planner=BindingTarget(profile_id=planner.profile_id)), expected_version=service.read_settings().configuration_revision)
    before = service.snapshot()
    service.set_bindings(BindingsPatch(planner=BindingTarget(adapter='fake')), expected_version=service.read_settings().configuration_revision)
    service.delete_profile(planner.profile_id, expected_version=1)
    restored = service.codec.decode(before)
    assert restored.adapters['planner'].field_map()['api_key'] == 'test-credential-A'
    assert service.list_profiles(limit=20, offset=0).total == 0
    with service.database.transaction(write=False) as conn:
        assert conn.execute('SELECT COUNT(*) AS n FROM model_profile_revisions').fetchone()['n'] == 2


def test_seed_is_once_only_and_same_transaction_snapshot_rolls_back(tmp_path):
    from video_agent.config_store import seed_default_settings
    service = _service(tmp_path)
    service.save_settings(SettingsPatch(default_duration=15), expected_version=1)
    with service.database.transaction() as conn:
        seed_default_settings(conn)
        assert service.read_effective_config(conn=conn).default_duration == 15
        snapshot = service.snapshot(conn=conn)
        assert service.codec.decode(snapshot).default_duration == 15
    assert service.read_settings().configuration_revision == 2


def test_missing_key_and_nested_credentials_are_not_leaked(tmp_path):
    from video_agent.config import DEFAULT_CONFIG_JSON, validate_config
    from video_agent.secrets import SnapshotCodec
    from video_agent.config_store import ModelProfileCreate
    service = _service(tmp_path)
    with pytest.raises(VideoAgentError):
        service.create_profile(ModelProfileCreate(name='文本', role='planner', adapter='openai_chat', parameters={'headers': {'Authorization': 'private-header'}}))
    assert service.list_profiles(limit=20, offset=0).total == 0
    raw = json.loads(DEFAULT_CONFIG_JSON)
    raw['adapters']['planner']['headers'] = {'Authorization': 'nested-private-token'}
    config = validate_config(raw)
    encrypted = service.codec.encode(config)
    assert 'nested-private-token' not in json.dumps(encrypted)
    assert service.codec.decode(encrypted).adapters['planner'].model_extra['headers']['Authorization'] == 'nested-private-token'
    with pytest.raises(VideoAgentError) as missing:
        SnapshotCodec('').decode(encrypted)
    assert missing.value.code == 'CONFIGURATION_ERROR'
    assert 'nested-private-token' not in str(missing.value)


def test_concurrent_profile_update_has_one_winner(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from video_agent.config_store import ModelProfileUpdate
    service = _service(tmp_path)
    profile = _profile(service)
    def update(name):
        try:
            return service.update_profile(profile.profile_id, ModelProfileUpdate(expected_version=1, name=name)).version
        except VideoAgentError as exc:
            return exc.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        result = list(pool.map(update, ['one', 'two']))
    assert sorted(map(str, result)) == ['2', 'CONFLICT']
    assert service.get_profile(profile.profile_id).version == 2


def test_snapshot_rejects_corruption_and_boolean_version(tmp_path):
    from video_agent.secrets import SnapshotCodec
    from video_agent.config_store import ModelProfileUpdate
    from pydantic import ValidationError
    service = _service(tmp_path)
    malformed = service.snapshot()
    malformed['encrypted_secrets'] = {'unexpected': 'type'}
    with pytest.raises(VideoAgentError):
        service.codec.decode(malformed)
    with pytest.raises(ValidationError):
        ModelProfileUpdate(expected_version=True)


def test_bound_profile_can_change_to_compatible_adapter_atomically(tmp_path):
    from video_agent.config_store import BindingsPatch, BindingTarget, ModelProfileUpdate
    service = _service(tmp_path)
    video = _profile(service, role='video', adapter='seedance')
    service.set_bindings(BindingsPatch(video=BindingTarget(profile_id=video.profile_id)), expected_version=service.read_settings().configuration_revision)
    original = service.snapshot()
    updated = service.update_profile(video.profile_id, ModelProfileUpdate(expected_version=1, adapter='wan', parameters={'max_segment_seconds': '5'}))
    assert updated.adapter == 'wan'
    assert service.read_bindings().video.adapter == 'wan'
    assert service.read_effective_config().adapters['video'].adapter == 'wan'
    assert service.codec.decode(original).adapters['video'].adapter == 'seedance'
