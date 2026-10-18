from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from video_agent.db import Connection, Database
from video_agent.runtime import RuntimeSettings


@pytest.mark.parametrize("version", ["5.7.17-log", "5.7.44", "8.0.36", "8.4.2"])
def test_database_accepts_supported_mysql_versions(monkeypatch, version):
    engine = MagicMock()
    engine.connect.return_value.__enter__.return_value.execute.return_value.scalar_one.return_value = version
    monkeypatch.setattr("video_agent.db.sa.create_engine", lambda *args, **kwargs: engine)

    database = Database.from_settings(RuntimeSettings())
    assert database.dialect_name == "mysql"
    engine.dispose.assert_not_called()
    database.close()
    engine.dispose.assert_called_once()


@pytest.mark.parametrize("version", ["5.6.51", "10.11.6-MariaDB", "unrecognized"])
def test_database_rejects_unsupported_servers_and_closes_engine(monkeypatch, version):
    engine = MagicMock()
    engine.connect.return_value.__enter__.return_value.execute.return_value.scalar_one.return_value = version
    monkeypatch.setattr("video_agent.db.sa.create_engine", lambda *args, **kwargs: engine)

    with pytest.raises(ValueError, match="MySQL 5.7"):
        Database.from_settings(RuntimeSettings())
    engine.dispose.assert_called_once()


@pytest.mark.parametrize(
    "dialect,version,write,skip_locked,expected",
    [
        ("mysql", (5, 7, 17), True, True, " FOR UPDATE"),
        ("mysql", (8, 0, 0), True, True, " FOR UPDATE"),
        ("mysql", (8, 0, 36), True, True, " FOR UPDATE SKIP LOCKED"),
        ("mysql", (8, 4, 2), True, False, " FOR UPDATE"),
        ("mysql", (5, 7, 17), False, True, ""),
        ("sqlite", (3, 45, 0), True, True, ""),
    ],
)
def test_locking_sql_uses_server_capabilities(dialect, version, write, skip_locked, expected):
    raw = SimpleNamespace(dialect=SimpleNamespace(name=dialect, server_version_info=version))
    connection = Connection(raw, write=write)
    sql = "SELECT * FROM work_items LIMIT 1"
    assert connection.for_update(sql, skip_locked=skip_locked) == sql + expected
