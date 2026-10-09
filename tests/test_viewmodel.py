import os
import re
from pathlib import Path

from video_agent.models import FailureView, TaskView


def _task(status: str, **kwargs) -> TaskView:
    base = dict(task_id="task-1", status=status, updated_at="2026-10-09T00:00:00+00:00")
    base.update(kwargs)
    return TaskView(**base)


def _expect(flags: str):
    from desktop_app.viewmodel import ButtonStates

    names = [
        "create",
        "submit_inputs",
        "revise_plan",
        "confirm",
        "discard_revision",
        "regenerate_full",
        "regenerate_shots",
        "retry_planning",
        "retry_generation",
        "open_artifact",
    ]
    parts = flags.split()
    assert len(parts) == len(names)
    return ButtonStates(**{name: part == "1" for name, part in zip(names, parts)})


# create submit revise confirm discard full shots retry_plan retry_gen open
CASES = [
    ("awaiting_input", dict(status="awaiting_input"), "1 1 0 0 0 0 0 0 0 0"),
    ("awaiting_input_confirmed", dict(status="awaiting_input", has_confirmed_plan=True, has_successful_artifact=True), "1 1 0 0 1 0 0 0 0 1"),
    ("awaiting_confirmation", dict(status="awaiting_confirmation"), "1 0 1 1 0 0 0 0 0 0"),
    ("awaiting_confirmation_confirmed", dict(status="awaiting_confirmation", has_confirmed_plan=True), "1 0 1 1 1 1 0 0 0 0"),
    ("awaiting_confirmation_artifact", dict(status="awaiting_confirmation", has_confirmed_plan=True, has_successful_artifact=True), "1 0 1 1 1 1 1 0 0 1"),
    ("awaiting_confirmation_no_ffmpeg", dict(status="awaiting_confirmation", has_confirmed_plan=True, has_successful_artifact=True, ffmpeg_available=False), "0 0 1 0 1 0 0 0 0 1"),
    ("succeeded", dict(status="succeeded", has_confirmed_plan=True, has_successful_artifact=True), "1 0 1 0 0 1 1 0 0 1"),
    ("succeeded_no_artifact", dict(status="succeeded", has_confirmed_plan=True), "1 0 1 0 0 1 0 0 0 0"),
    ("succeeded_no_confirmed", dict(status="succeeded", has_successful_artifact=True), "1 0 1 0 0 0 0 0 0 1"),
    ("failed_planning_no_head", dict(status="failed", failure_scope="planning"), "1 1 0 0 0 0 0 1 0 0"),
    ("failed_planning_head", dict(status="failed", failure_scope="planning", has_head=True), "1 0 1 0 0 0 0 1 0 0"),
    ("failed_planning_confirmed", dict(status="failed", failure_scope="planning", has_head=True, has_confirmed_plan=True), "1 0 1 0 1 0 0 1 0 0"),
    ("failed_planning_no_head_confirmed", dict(status="failed", failure_scope="planning", has_confirmed_plan=True), "1 1 0 0 1 0 0 1 0 0"),
    ("failed_planning_no_ffmpeg", dict(status="failed", failure_scope="planning", ffmpeg_available=False), "0 1 0 0 0 0 0 1 0 0"),
    ("failed_generation_head_confirmed", dict(status="failed", failure_scope="generation", has_head=True, head_confirmed=True, has_confirmed_plan=True, has_successful_artifact=True), "1 0 1 0 0 1 1 0 1 1"),
    ("failed_generation_head_open", dict(status="failed", failure_scope="generation", has_head=True, has_confirmed_plan=True, has_successful_artifact=True), "1 0 1 1 1 1 1 0 1 1"),
    ("failed_generation_no_artifact", dict(status="failed", failure_scope="generation", has_head=True, has_confirmed_plan=True), "1 0 1 1 1 1 0 0 1 0"),
    ("failed_generation_no_confirmed", dict(status="failed", failure_scope="generation", has_head=True), "1 0 1 1 0 0 0 0 1 0"),
    ("failed_generation_no_ffmpeg", dict(status="failed", failure_scope="generation", has_head=True, has_confirmed_plan=True, has_successful_artifact=True, ffmpeg_available=False), "0 0 1 0 1 0 0 0 0 1"),
    ("pending", dict(status="pending", has_head=True, has_confirmed_plan=True, has_successful_artifact=True), "1 0 0 0 0 0 0 0 0 1"),
    ("planning", dict(status="planning"), "1 0 0 0 0 0 0 0 0 0"),
    ("generating", dict(status="generating", has_confirmed_plan=True, has_successful_artifact=True), "1 0 0 0 0 0 0 0 0 1"),
    ("generating_no_ffmpeg", dict(status="generating", has_successful_artifact=True, ffmpeg_available=False), "0 0 0 0 0 0 0 0 0 1"),
    ("no_task", dict(status=None), "1 0 0 0 0 0 0 0 0 0"),
    ("no_task_no_ffmpeg", dict(status=None, ffmpeg_available=False), "0 0 0 0 0 0 0 0 0 0"),
    ("failed_without_scope", dict(status="failed"), "1 0 0 0 0 0 0 0 0 0"),
]


def test_button_states_cover_contract_table():
    from desktop_app.viewmodel import button_states

    required = {
        "awaiting_input",
        "awaiting_input_confirmed",
        "awaiting_confirmation",
        "awaiting_confirmation_confirmed",
        "awaiting_confirmation_no_ffmpeg",
        "succeeded",
        "succeeded_no_artifact",
        "failed_planning_no_head",
        "failed_planning_head",
        "failed_planning_confirmed",
        "failed_generation_head_confirmed",
        "failed_generation_head_open",
        "failed_generation_no_ffmpeg",
        "pending",
        "planning",
        "generating",
    }
    assert required <= {name for name, _kwargs, _flags in CASES}
    for name, kwargs, flags in CASES:
        params = dict(
            status=None,
            failure_scope=None,
            has_head=False,
            head_confirmed=False,
            has_confirmed_plan=False,
            has_successful_artifact=False,
            ffmpeg_available=True,
        )
        params.update(kwargs)
        assert button_states(**params) == _expect(flags), name


def test_button_states_for_reads_only_task_fields():
    from desktop_app.viewmodel import button_states_for

    draft = _task("failed", failure_scope="generation", head_plan_version=3, confirmed_plan_version=2)
    got = button_states_for(draft, has_successful_artifact=True, ffmpeg_available=True)
    assert got.confirm is True
    assert got.discard_revision is True
    assert got.retry_generation is True
    assert got.regenerate_shots is True

    same = _task("failed", failure_scope="generation", head_plan_version=2, confirmed_plan_version=2)
    locked = button_states_for(same, has_successful_artifact=True, ffmpeg_available=True)
    assert locked.confirm is False
    assert locked.discard_revision is False

    unpublished = _task("failed", failure_scope="planning", head_plan_version=None, confirmed_plan_version=None)
    retry = button_states_for(unpublished, has_successful_artifact=False, ffmpeg_available=True)
    assert retry.retry_planning is True
    assert retry.submit_inputs is True
    assert retry.revise_plan is False

    assert button_states_for(None, has_successful_artifact=False, ffmpeg_available=True).create is True
    assert button_states_for(None, has_successful_artifact=False, ffmpeg_available=False).create is False


def test_bottom_failure_and_middle_questions_stay_apart():
    from desktop_app.viewmodel import failure_text, question_text

    failed = _task(
        "failed",
        failure_scope="generation",
        failure=FailureView(
            code="FIRST_FRAME_NOT_IMPLEMENTED",
            message="未实现首帧",
            stage="preparing_first_frame",
            retryable=False,
        ),
        questions=["不要显示在底部"],
    )
    assert failure_text(failed) == ("FIRST_FRAME_NOT_IMPLEMENTED", "未实现首帧", "preparing_first_frame")
    assert question_text(failed) == ""

    waiting = _task("awaiting_input", questions=["缺少产品图片", "请指定语言"])
    assert failure_text(waiting) == ("", "", "")
    assert question_text(waiting) == "缺少产品图片\n请指定语言"
    assert failure_text(None) == ("", "", "")
    assert question_text(None) == ""

    no_stage = _task(
        "failed",
        failure=FailureView(code="STATE_CONFLICT", message="冲突", stage=None, retryable=False),
    )
    assert failure_text(no_stage) == ("STATE_CONFLICT", "冲突", "")


def test_error_dialog_text_includes_code_message_stage():
    from desktop_app.viewmodel import error_dialog_text

    assert error_dialog_text("VALIDATION_ERROR", "时长不在档位内", "planning") == "VALIDATION_ERROR\n时长不在档位内\nplanning"
    assert error_dialog_text("NOT_FOUND", "没有这条任务", None) == "NOT_FOUND\n没有这条任务\n"


def test_poll_interval_and_idle_sleep():
    from desktop_app.viewmodel import IDLE_SLEEP_SECONDS, POLL_INTERVAL_MS, should_poll

    assert POLL_INTERVAL_MS == 1000
    assert IDLE_SLEEP_SECONDS == 1
    for status in ("pending", "planning", "generating"):
        assert should_poll(status) is True
    for status in ("awaiting_input", "awaiting_confirmation", "succeeded", "failed", None, ""):
        assert should_poll(status) is False


def test_each_click_gets_a_new_visible_ascii_key():
    from desktop_app.viewmodel import new_idempotency_key

    first = new_idempotency_key()
    second = new_idempotency_key()
    assert first != second
    assert re.fullmatch(r"[\x21-\x7e]{1,128}", first)


def test_command_stays_disabled_until_the_call_returns():
    from desktop_app.viewmodel import command_enabled

    assert command_enabled(True, False) is True
    assert command_enabled(True, True) is False
    assert command_enabled(False, False) is False


def test_resolve_home_uses_env_then_cwd(monkeypatch, tmp_path):
    from desktop_app.viewmodel import resolve_home

    target = tmp_path / "from-env"
    monkeypatch.setenv("VIDEO_AGENT_HOME", str(target))
    assert resolve_home() == target
    monkeypatch.delenv("VIDEO_AGENT_HOME", raising=False)
    monkeypatch.chdir(tmp_path)
    assert resolve_home() == tmp_path / ".video-agent"
    assert "VIDEO_AGENT_HOME" not in os.environ or os.environ.get("VIDEO_AGENT_HOME") != str(tmp_path / ".video-agent")


def test_window_title_and_region_copy():
    from pathlib import Path

    from desktop_app.viewmodel import (
        WINDOW_TITLE,
        ffmpeg_label,
        format_execution,
        format_task_row,
        home_label,
        mode_label,
    )
    from video_agent.models import TaskSummary, TaskView

    assert WINDOW_TITLE == "视频生成测试台"
    assert ffmpeg_label(True) == "FFmpeg：可用"
    assert ffmpeg_label(False) == "FFmpeg：缺少 FFmpeg"
    assert mode_label("fake") == "模式：替身"
    assert mode_label("real") == "模式：真实"
    assert home_label(Path("D:/agent-data")) == "数据目录：D:/agent-data"
    summary = TaskSummary(task_id="abc", status="succeeded", product_name="玻璃杯", updated_at="2026-10-09T00:00:00+00:00")
    assert format_task_row(summary) == "玻璃杯  succeeded  abc"
    unnamed = TaskSummary(task_id="abc", status="pending", product_name=None, updated_at="2026-10-09T00:00:00+00:00")
    assert format_task_row(unnamed) == "未命名  pending  abc"
    task = TaskView(
        task_id="abc",
        status="generating",
        stage="generating_shots",
        stage_detail="正在生成第 1 镜",
        completed_shots=0,
        total_shots=1,
        updated_at="2026-10-09T00:00:00+00:00",
    )
    text = format_execution(task)
    assert "generating_shots" in text
    assert "0/1" in text
    assert "正在生成第 1 镜" in text
