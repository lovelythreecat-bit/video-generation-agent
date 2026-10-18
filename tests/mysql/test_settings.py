"""Actual isolated MySQL acceptance for configuration transactions and confidentiality."""
import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from cryptography.fernet import Fernet

from video_agent.config import SettingsPatch
from video_agent.config_store import (
    BindingTarget, BindingsPatch, ModelProfileCreate, ModelProfileUpdate,
    metadata, seed_default_settings,
)
from video_agent.errors import VideoAgentError
from video_agent.settings_service import SettingsService


@pytest.fixture
def settings(mysql_database):
    assert mysql_database.dialect_name == 'mysql'
    with mysql_database.transaction() as conn:
        for table in reversed(metadata.sorted_tables):
            conn.execute(f'DELETE FROM {table.name}')
        seed_default_settings(conn)
    return SettingsService(mysql_database, Fernet.generate_key())


def _profile(service, **changes):
    payload = dict(name='中文配置', role='planner', adapter='openai_chat',
                   base_url='https://a.invalid/v1', model='model-A',
                   secrets={'api_key': 'mysql-test-secret-A'})
    payload.update(changes)
    return service.create_profile(ModelProfileCreate(**payload))


def test_profile_crud_and_role_binding(settings):
    profile = _profile(settings)
    assert settings.get_profile(profile.profile_id).name == '中文配置'
    assert settings.list_profiles(limit=20, offset=0).total == 1
    revision = settings.read_settings().configuration_revision
    bindings = settings.set_bindings(BindingsPatch(planner=BindingTarget(profile_id=profile.profile_id)), expected_version=revision)
    assert bindings.planner.profile_id == profile.profile_id
    for action in (
        lambda: settings.update_profile(profile.profile_id, ModelProfileUpdate(expected_version=1, enabled=False)),
        lambda: settings.delete_profile(profile.profile_id, expected_version=1),
    ):
        with pytest.raises(VideoAgentError) as caught:
            action()
        assert caught.value.code == 'CONFLICT'
    settings.set_bindings(BindingsPatch(planner=BindingTarget(adapter='fake')), expected_version=bindings.configuration_revision)
    settings.delete_profile(profile.profile_id, expected_version=1)
    assert settings.list_profiles(limit=20, offset=0).total == 0
    with pytest.raises(VideoAgentError) as caught:
        settings.get_profile(profile.profile_id)
    assert caught.value.code == 'NOT_FOUND'


def test_invalid_real_mode_rolls_back(settings):
    before = settings.read_settings()
    with pytest.raises(VideoAgentError):
        settings.save_settings(SettingsPatch(mode='real'), expected_version=before.configuration_revision)
    assert settings.read_settings() == before


def test_version_conflict(settings):
    profile = _profile(settings)
    settings.update_profile(profile.profile_id, ModelProfileUpdate(expected_version=1, name='已更新'))
    with pytest.raises(VideoAgentError) as caught:
        settings.update_profile(profile.profile_id, ModelProfileUpdate(expected_version=1, name='过期'))
    assert caught.value.code == 'CONFLICT'
    assert settings.get_profile(profile.profile_id).name == '已更新'
    before = settings.read_settings()
    settings.save_settings(SettingsPatch(default_duration=15), expected_version=before.configuration_revision)
    with pytest.raises(VideoAgentError):
        settings.save_settings(SettingsPatch(default_duration=5), expected_version=before.configuration_revision)
    assert settings.read_settings().default_duration == 15


def test_blank_secret_keeps_clear_secret_removes(settings):
    profile = _profile(settings)
    kept = settings.update_profile(profile.profile_id, ModelProfileUpdate(expected_version=1, secrets={'api_key': ''}))
    assert kept.has_api_key
    with pytest.raises(VideoAgentError):
        settings.update_profile(profile.profile_id, ModelProfileUpdate(expected_version=2, secrets={'api_key': ''}, clear_secrets=['api_key']))
    assert settings.get_profile(profile.profile_id).version == 2
    cleared = settings.update_profile(profile.profile_id, ModelProfileUpdate(expected_version=2, clear_secrets=['api_key']))
    assert not cleared.has_api_key


def test_credentials_encrypted_in_revisions_and_snapshots(settings):
    profile = _profile(settings)
    settings.set_bindings(BindingsPatch(planner=BindingTarget(profile_id=profile.profile_id)), expected_version=settings.read_settings().configuration_revision)
    snapshot = settings.snapshot()
    assert 'mysql-test-secret-A' not in json.dumps(snapshot)
    assert 'mysql-test-secret-A' not in settings.get_profile(profile.profile_id).model_dump_json()
    with settings.database.transaction(write=False) as conn:
        for table in ('model_profiles', 'model_profile_revisions', 'app_settings'):
            assert 'mysql-test-secret-A' not in str([dict(row) for row in conn.execute(f'SELECT * FROM {table}').fetchall()])
    assert settings.codec.decode(snapshot).adapters['planner'].field_map()['api_key'] == 'mysql-test-secret-A'


def test_binding_batch_rollback_and_soft_delete_keep_history(settings):
    planner = _profile(settings)
    before = settings.read_bindings()
    with pytest.raises(VideoAgentError):
        settings.set_bindings(BindingsPatch(planner=BindingTarget(profile_id=planner.profile_id), video=BindingTarget(profile_id=planner.profile_id)), expected_version=before.configuration_revision)
    assert settings.read_bindings() == before
    settings.set_bindings(BindingsPatch(planner=BindingTarget(profile_id=planner.profile_id)), expected_version=before.configuration_revision)
    snapshot = settings.snapshot()
    settings.set_bindings(BindingsPatch(planner=BindingTarget(adapter='fake')), expected_version=settings.read_settings().configuration_revision)
    settings.delete_profile(planner.profile_id, expected_version=1)
    assert settings.codec.decode(snapshot).adapters['planner'].field_map()['api_key'] == 'mysql-test-secret-A'
    with settings.database.transaction(write=False) as conn:
        assert conn.execute('SELECT COUNT(*) AS total FROM model_profile_revisions').fetchone()['total'] == 2


def test_concurrent_profile_and_global_versions_have_one_winner(settings):
    profile = _profile(settings)
    def save(name):
        try:
            settings.update_profile(profile.profile_id, ModelProfileUpdate(expected_version=1, name=name))
            return 'saved'
        except VideoAgentError as exc:
            return exc.code
    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(save, ['first', 'second'])) == ['CONFLICT', 'saved']
    revision = settings.read_settings().configuration_revision
    def update(duration):
        try:
            settings.save_settings(SettingsPatch(default_duration=duration), expected_version=revision)
            return 'saved'
        except VideoAgentError as exc:
            return exc.code
    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(update, [5, 15])) == ['CONFLICT', 'saved']


def test_default_seed_never_resets_existing_settings(settings):
    settings.save_settings(SettingsPatch(default_duration=15), expected_version=1)
    with settings.database.transaction() as conn:
        seed_default_settings(conn)
    assert settings.read_settings().configuration_revision == 2
    assert settings.read_effective_config().default_duration == 15
