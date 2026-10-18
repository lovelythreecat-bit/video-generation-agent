"""Actual MySQL only. Tests fail explicitly if no independent DB is configured."""

import os
import re

import pytest
from alembic import command
from alembic.config import Config

from video_agent.db import Database
from video_agent.db_schema import metadata
from video_agent.config_store import metadata as settings_metadata, seed_default_settings
from video_agent.provider_jobs import metadata as worker_metadata
from video_agent.runtime import RuntimeSettings


@pytest.fixture(scope="session")
def mysql_settings(tmp_path_factory):
    required = ("HOST", "PORT", "NAME", "USER", "PASSWORD")
    missing = ["VIDEO_AGENT_TEST_DB_" + key for key in required if "VIDEO_AGENT_TEST_DB_" + key not in os.environ]
    if missing:
        pytest.fail("Actual MySQL integration requires independent test configuration: " + ", ".join(missing), pytrace=False)
    name = os.environ["VIDEO_AGENT_TEST_DB_NAME"]
    if not re.fullmatch(r"(?:test_[A-Za-z0-9_]+|[A-Za-z0-9_]+_test)", name):
        pytest.fail("Refusing to modify database: TEST_DB_NAME must start with test_ or end with _test", pytrace=False)
    if name == os.environ.get("VIDEO_AGENT_DB_NAME", "video_agent"):
        pytest.fail("Integration test database must differ from production database", pytrace=False)
    return RuntimeSettings(
        db_host=os.environ["VIDEO_AGENT_TEST_DB_HOST"], db_port=int(os.environ["VIDEO_AGENT_TEST_DB_PORT"]),
        db_name=name, db_user=os.environ["VIDEO_AGENT_TEST_DB_USER"], db_password=os.environ["VIDEO_AGENT_TEST_DB_PASSWORD"],
        home=tmp_path_factory.mktemp("mysql-files"), business_token="test-business-token", admin_token="test-admin-token",
    )


@pytest.fixture(scope="session")
def _mysql_database(mysql_settings):
    database = Database.from_settings(mysql_settings)
    configuration = Config("alembic.ini")
    with database.engine.connect() as connection:
        configuration.attributes["connection"] = connection
        command.upgrade(configuration, "head")
    yield database
    database.close()


@pytest.fixture
def mysql_database(_mysql_database):
    # Strict guard above precedes all mutation; never clear a production DB.
    with _mysql_database.transaction() as connection:
        # Use only the current application's explicit metadata. Preserve the
        # migration version and unrelated tables, and reset encrypted settings
        # before a test can instantiate a service with its own master key.
        for schema in (worker_metadata, metadata, settings_metadata):
            for table in reversed(schema.sorted_tables):
                connection.execute(f"DELETE FROM {table.name}")
        seed_default_settings(connection)
    yield _mysql_database
