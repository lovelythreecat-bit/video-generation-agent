import uuid

import pytest
import sqlalchemy as sa

from video_agent import VideoAgent
from video_agent.db import Database
from video_agent.db_schema import metadata
from video_agent.models import CreateTaskRequest, ProductInfo
from video_agent.state import publish_document


def agent_for(database, home):
    from video_agent.settings_service import SettingsService
    return VideoAgent(home, database=database, settings_service=SettingsService(database), planner=object())


def test_fixture_resets_prior_suite_configuration_and_worker_state(_mysql_database, request, tmp_path):
    """A prior suite's encryption key and durable worker state cannot leak in."""
    from cryptography.fernet import Fernet
    from video_agent.config import SettingsPatch
    from video_agent.config_store import (
        BindingTarget, BindingsPatch, ModelProfileCreate,
        metadata as settings_metadata, seed_default_settings,
    )
    from video_agent.errors import VideoAgentError
    from video_agent.provider_jobs import ProviderJob, ProviderJobStore
    from video_agent.settings_service import SettingsService

    # Start from a deterministic prior suite with its own master key, as HTTP
    # and settings tests do. The session fixture has already guarded TEST_DB.
    with _mysql_database.transaction() as connection:
        for table in reversed(settings_metadata.sorted_tables):
            connection.execute(f'DELETE FROM {table.name}')
        seed_default_settings(connection)
        version = connection.execute('SELECT version_num FROM alembic_version').fetchone()['version_num']
        connection.execute("INSERT INTO worker_heartbeats (singleton_id, worker_owner, lease_until, stopping) "
                           "VALUES (1, 'previous-suite', '2099-01-01T00:00:00+00:00', 1) "
                           "ON DUPLICATE KEY UPDATE worker_owner=VALUES(worker_owner), "
                           "lease_until=VALUES(lease_until), stopping=1")
    previous = SettingsService(_mysql_database, Fernet.generate_key())
    profile = previous.create_profile(ModelProfileCreate(
        name='prior suite', role='planner', adapter='openai_chat',
        base_url='https://previous.invalid/v1', model='previous-model',
        secrets={'api_key': 'synthetic-previous-suite-secret'},
    ))
    previous.set_bindings(BindingsPatch(planner=BindingTarget(profile_id=profile.profile_id)),
                          expected_version=previous.read_settings().configuration_revision)
    previous.save_settings(SettingsPatch(default_duration=15),
                           expected_version=previous.read_settings().configuration_revision)
    ProviderJobStore(_mysql_database).save(ProviderJob(
        adapter='fake', key='previous-suite', external_task_id='previous-external',
        submission_state='accepted',
    ))
    with pytest.raises(VideoAgentError) as caught:
        SettingsService(_mysql_database, Fernet.generate_key()).read_effective_config()
    assert caught.value.code == 'CONFIGURATION_ERROR'

    # Request the actual function fixture only after the prior suite commits.
    database = request.getfixturevalue('mysql_database')
    agent = agent_for(database, tmp_path)
    task = agent.create_task(CreateTaskRequest())
    assert agent.get_task(task.task_id).task_id == task.task_id
    fresh = SettingsService(database)
    assert fresh.read_settings().configuration_revision == 1
    assert fresh.read_settings().default_duration == 10
    assert fresh.read_effective_config().mode == 'fake'
    assert fresh.read_bindings().planner == BindingTarget(adapter='fake')
    with database.transaction(write=False) as connection:
        for table in ('model_profiles', 'model_profile_revisions', 'provider_jobs', 'worker_heartbeats'):
            assert connection.execute(f'SELECT COUNT(*) AS n FROM {table}').fetchone()['n'] == 0
        assert connection.execute('SELECT version_num FROM alembic_version').fetchone()['version_num'] == version


def test_mysql_schema_roundtrip(mysql_database, mysql_settings, tmp_path):
    assert mysql_database.dialect_name == "mysql"
    assert set(metadata.tables) <= set(sa.inspect(mysql_database.engine).get_table_names())
    agent = agent_for(mysql_database, tmp_path)
    task = agent.create_task(CreateTaskRequest(product_info=ProductInfo(name="中文产品")))
    reopened = Database.from_settings(mysql_settings)
    try:
        restored = agent_for(reopened, tmp_path).get_task(task.task_id)
        assert restored.task_id == task.task_id
        assert restored.product_name == "中文产品"
    finally:
        reopened.close()


def test_transaction_rollback(mysql_database):
    asset_id = str(uuid.uuid4())
    with pytest.raises(RuntimeError):
        with mysql_database.transaction() as connection:
            connection.execute("""INSERT INTO assets
              (asset_id,kind,filename,media_type,byte_size,relative_path,created_at)
              VALUES (:asset_id,'image','a.png','image/png',1,'assets/a.png','t')""", {"asset_id": asset_id})
            raise RuntimeError("rollback")
    with mysql_database.transaction(write=False) as connection:
        assert connection.execute("SELECT asset_id FROM assets WHERE asset_id=:asset_id", {"asset_id": asset_id}).fetchone() is None


def test_database_error_rolls_back_all_prior_writes(mysql_database):
    sql = """INSERT INTO assets (asset_id,kind,filename,media_type,byte_size,relative_path,created_at)
             VALUES ('duplicate','image','a.png','image/png',1,'assets/a.png','t')"""
    with pytest.raises(sa.exc.IntegrityError):
        with mysql_database.transaction() as connection:
            connection.execute(sql)
            connection.execute(sql)
    with mysql_database.transaction(write=False) as connection:
        assert connection.execute("SELECT COUNT(*) AS n FROM assets").fetchone()["n"] == 0


def test_alembic_downgrade_upgrade_roundtrip(mysql_database):
    from alembic import command
    from alembic.config import Config

    configuration = Config("alembic.ini")
    with mysql_database.engine.connect() as connection:
        configuration.attributes["connection"] = connection
        command.downgrade(configuration, "base")
        assert not set(metadata.tables) & set(sa.inspect(connection).get_table_names())
        connection.commit()
        command.upgrade(configuration, "head")
        assert set(metadata.tables) <= set(sa.inspect(connection).get_table_names())


def test_task_state_transitions_mysql(mysql_database, mysql_settings, tmp_path):
    from tests.test_confirm import document

    agent = agent_for(mysql_database, tmp_path)
    task = agent.create_task(CreateTaskRequest())
    with mysql_database.transaction() as connection:
        assert publish_document(connection, task_id=task.task_id, document=document(), now=agent.clock.now().isoformat()) == 1
    reopened = Database.from_settings(mysql_settings)
    try:
        agent = agent_for(reopened, tmp_path)
        confirmed = agent.confirm(task.task_id, 1)
        assert confirmed.confirmed_plan_version == 1
        assert confirmed.status == "generating"
        assert agent.get_plan(task.task_id, 1).document.creative == "已确认创意"
    finally:
        reopened.close()
