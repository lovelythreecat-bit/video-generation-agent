from pathlib import Path

import pytest

from video_agent import VideoAgentError as ExportedError
from video_agent.errors import VideoAgentError


def test_error_exposes_contract_attributes():
    error = VideoAgentError(
        "VALIDATION_ERROR",
        "时长不在档位内",
        stage="queued",
        retryable=False,
        task_id="task-1",
        generation_id="gen-1",
        plan_version=2,
        fields=["duration_seconds"],
        shot_ids=["s1"],
        details={"duration_tiers": [5, 10, 15, 30]},
    )
    assert isinstance(error, Exception)
    assert error.code == "VALIDATION_ERROR"
    assert error.message == "时长不在档位内"
    assert error.stage == "queued"
    assert error.retryable is False
    assert error.task_id == "task-1"
    assert error.generation_id == "gen-1"
    assert error.plan_version == 2
    assert error.fields == ["duration_seconds"]
    assert error.shot_ids == ["s1"]
    assert error.details == {"duration_tiers": [5, 10, 15, 30]}


def test_error_defaults_stay_unset():
    error = VideoAgentError("NOT_FOUND", "任务不存在")
    assert error.stage is None
    assert error.retryable is False
    assert error.task_id is None
    assert error.generation_id is None
    assert error.plan_version is None
    assert error.fields is None
    assert error.shot_ids is None
    assert error.details is None


def test_package_exports_the_same_error():
    assert ExportedError is VideoAgentError


def test_gitignore_ignores_the_data_directory():
    text = Path(".gitignore").read_text(encoding="utf-8")
    assert ".video-agent/" in text.splitlines()
