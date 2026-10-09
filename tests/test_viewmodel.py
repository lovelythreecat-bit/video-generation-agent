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


def _document(shot_ids=("s1",)):
    from video_agent.models import PlanDocument, SharedVisual, ShotPlan

    shots = []
    frame = 0
    for shot_id in shot_ids:
        shots.append(
            ShotPlan(
                shot_id=shot_id,
                content=f"{shot_id}内容",
                action="转动",
                shot_size="中景",
                camera="固定",
                scene_id="scene-a",
                start_frame=frame,
                end_frame=frame + 150,
                subtitle_text="字幕",
                voiceover_text="配音",
            )
        )
        frame += 150
    return PlanDocument(
        creative="厨房里的玻璃杯",
        audience="家庭",
        selling_points=["耐热"],
        shared_visual=SharedVisual(
            product_reference="透明杯",
            scene="厨房台面",
            lighting="白天",
            palette="浅色",
            style="写实",
        ),
        shots=shots,
        duration_seconds=10,
        aspect_ratio="9:16",
        fit="native",
        subtitles=True,
        voiceover=True,
        music=True,
        first_frame_source="selected",
        first_frame_asset_id="img-1",
        model_capabilities="9:16 原生",
    )


def _plan(version: int, shot_ids=("s1",)):
    from video_agent.models import PlanView

    return PlanView(
        task_id="task-1",
        plan_version=version,
        is_head=True,
        confirmed=False,
        abandoned=False,
        document=_document(shot_ids),
    )


def _artifact(plan_version: int, version: int):
    from video_agent.models import ArtifactView

    return ArtifactView(
        artifact_id=f"art-{version}",
        task_id="task-1",
        generation_id=f"gen-{version}",
        plan_version=plan_version,
        version=version,
        duration_seconds=10,
        aspect_ratio="9:16",
        width=1080,
        height=1920,
        quality_status="not_assessed",
        quality={"reason": "visual adapter is none"},
        created_at="2026-10-09T00:00:00+00:00",
    )


def _demand(**kwargs):
    from desktop_app.viewmodel import DemandEdit

    base = dict(
        name="玻璃杯",
        category="家居",
        selling_points=["耐热", "透明"],
        specs="300ml",
        user_prompt="厨房台面，白天",
        images=["img-1"],
        first_frame_asset_id="img-1",
        duration_seconds=10,
        aspect_ratio="9:16",
        subtitles=True,
        voiceover=True,
        music=False,
        language="中文",
        voice_preference="平稳",
        music_direction="",
        music_asset_id=None,
    )
    base.update(kwargs)
    return DemandEdit(**base)


def _plan_edit(**kwargs):
    from desktop_app.viewmodel import PlanEdit
    from video_agent.models import ShotTextPatch

    base = dict(
        duration_seconds=15,
        aspect_ratio="16:9",
        fit="pad",
        subtitles=True,
        voiceover=False,
        music=True,
        language="中文",
        voice_preference="",
        music_direction="轻快",
        music_asset_id=None,
        clear_music_asset=False,
        first_frame_asset_id="img-1",
        clear_first_frame=False,
        shot_texts=[ShotTextPatch(shot_id="s1", content="杯口特写")],
    )
    base.update(kwargs)
    return PlanEdit(**base)


def test_create_request_sends_fields_prompt_and_optional_first_frame():
    from desktop_app.viewmodel import FIRST_FRAME_NONE, build_create_request, first_frame_asset_id

    request = build_create_request(_demand())
    assert request.product_info.name == "玻璃杯"
    assert request.product_info.selling_points == ["耐热", "透明"]
    assert request.user_prompt == "厨房台面，白天"
    assert request.images == ["img-1"]
    assert request.first_frame_asset_id == "img-1"
    assert request.video_options.duration_seconds == 10
    assert request.video_options.music is False
    assert request.audio_options.language == "中文"
    omitted = build_create_request(_demand(first_frame_asset_id=None, images=[]))
    assert omitted.first_frame_asset_id is None
    assert omitted.images == []
    assert first_frame_asset_id(FIRST_FRAME_NONE) is None


def test_blank_instruction_submits_patch_and_text_submits_only_instruction():
    from desktop_app.viewmodel import build_plan_change

    spoken = build_plan_change("  把镜头改成手持特写  ", _plan_edit())
    assert spoken.instruction == "把镜头改成手持特写"
    assert spoken.patch is None

    silent = build_plan_change("   ", _plan_edit(clear_first_frame=True, first_frame_asset_id=None))
    assert silent.instruction is None
    assert silent.patch is not None
    assert silent.patch.duration_seconds == 15
    assert silent.patch.aspect_ratio == "16:9"
    assert silent.patch.fit == "pad"
    assert silent.patch.shots is None
    assert silent.patch.shot_texts[0].content == "杯口特写"
    assert silent.patch.clear_first_frame is True
    assert silent.patch.first_frame_asset_id is None


def test_input_patch_omits_images_until_the_user_picks_again():
    from desktop_app.viewmodel import build_input_patch

    kept = build_input_patch(_demand(), images_changed=False)
    assert kept.images is None
    assert kept.user_prompt == "厨房台面，白天"
    changed = build_input_patch(_demand(images=[], first_frame_asset_id=None), images_changed=True)
    assert changed.images == []
    assert changed.clear_first_frame is True


def test_shot_choices_come_from_newest_artifact_plan():
    from desktop_app.viewmodel import build_regenerate_full, build_regenerate_shots, shot_checkbox_items, shot_plan_version

    artifacts = [_artifact(plan_version=4, version=2), _artifact(plan_version=2, version=1)]
    assert shot_plan_version(artifacts) == 4
    assert shot_plan_version([]) is None
    labels = shot_checkbox_items(_plan(4, ("s1", "s2")))
    assert [item[0] for item in labels] == ["s1", "s2"]
    assert "s1内容" in labels[0][1]
    partial = build_regenerate_shots("art-2", ["s1", "s2"])
    assert partial.kind == "shots"
    assert partial.artifact_id == "art-2"
    assert partial.shot_ids == ["s1", "s2"]
    assert build_regenerate_full().kind == "full"
    assert build_regenerate_full().shot_ids == []


def test_plan_and_artifact_text_include_contract_fields():
    from desktop_app.viewmodel import format_artifact, format_plan, parse_selling_points

    text = format_plan(_plan(3))
    for piece in ("厨房里的玻璃杯", "透明杯", "厨房台面", "白天", "浅色", "写实", "s1", "中景", "固定", "scene-a", "[0,150)", "selected", "native"):
        assert piece in text
    line = format_artifact(_artifact(3, 2))
    assert "v2" in line
    assert "10s" in line
    assert "1080×1920" in line
    assert "not_assessed" in line
    assert parse_selling_points(" 耐热 \n\n透明\n") == ["耐热", "透明"]


def test_settings_form_hides_secrets_and_patch_keeps_empty_string():
    from desktop_app.viewmodel import (
        AdapterEdit,
        SettingsEdit,
        adapter_field_map,
        build_settings_patch,
        fields_to_text,
        is_secret_field,
        parse_duration_tiers,
        parse_field_lines,
        redact_secret_fields,
        settings_form_from_mapping,
    )

    assert is_secret_field("api_key") is True
    assert is_secret_field("API_TOKEN") is True
    assert is_secret_field("client_secret") is True
    assert is_secret_field("model") is False
    shown = redact_secret_fields({"api_key": "abc123", "model": "demo"})
    assert shown == {"api_key": "", "model": "demo"}
    text = fields_to_text(shown)
    assert "abc123" not in text
    assert parse_field_lines(text)["api_key"] == ""
    assert parse_field_lines(text)["model"] == "demo"

    class Adapter:
        adapter = "fake"
        api_key = "abc123"
        model = "demo"

    name, fields = adapter_field_map(Adapter())
    assert name == "fake"
    assert fields["api_key"] == "abc123"
    assert adapter_field_map({"adapter": "fake", "fields": {"api_key": "abc123", "model": "demo"}})[1]["model"] == "demo"

    config = {
        "mode": "fake",
        "duration_tiers": [5, 10, 15, 30],
        "default_duration": 10,
        "adapters": {
            "planner": {"adapter": "fake", "api_key": "abc123", "model": "demo"},
            "video": {"adapter": "fake"},
            "voice": {"adapter": "fake"},
            "music": {"adapter": "fake"},
            "visual": {"adapter": "none"},
        },
    }
    edit = settings_form_from_mapping(config)
    assert "abc123" not in repr(edit)
    assert edit.planner.fields["api_key"] == ""
    assert edit.planner.fields["model"] == "demo"
    assert edit.mode == "fake"
    assert parse_duration_tiers("5, 10, 15, 30") == [5, 10, 15, 30]
    patch = build_settings_patch(
        SettingsEdit(
            duration_tiers=[5, 10, 15],
            default_duration=15,
            mode="fake",
            planner=AdapterEdit(adapter="fake", fields={"api_key": "", "model": "demo"}),
            video=AdapterEdit(adapter="fake", fields={}),
            voice=AdapterEdit(adapter="fake", fields={}),
            music=AdapterEdit(adapter="fake", fields={}),
            visual=AdapterEdit(adapter="none", fields={}),
        )
    )
    assert set(patch.model_fields) == {
        "duration_tiers",
        "default_duration",
        "mode",
        "planner",
        "video",
        "voice",
        "music",
        "visual",
    }
    assert patch.planner.fields["api_key"] == ""
    assert patch.default_duration == 15
    dumped = patch.model_dump()
    assert "video_bitrate" not in dumped
    assert "resolutions" not in dumped
    assert "max_images" not in dumped
    assert "max_file_bytes" not in dumped


def test_worker_recovers_before_tick_and_sleeps_one_second_when_idle():
    import threading

    from video_agent.models import TickResult

    from desktop_app.viewmodel import IDLE_SLEEP_SECONDS
    from desktop_app.worker import start_worker

    order = []

    class Agent:
        def __init__(self) -> None:
            self.ticks = 0

        def recover(self) -> int:
            order.append("recover")
            return 0

        def tick(self) -> TickResult:
            self.ticks += 1
            order.append("tick" if self.ticks == 1 else "tick-again")
            return TickResult(did_work=self.ticks == 1)

    stop = threading.Event()

    def sleep(seconds: float) -> None:
        order.append(seconds)
        stop.set()

    thread = start_worker(Agent(), stop, sleep=sleep)
    thread.join(timeout=2)
    assert order == ["recover", "tick", "tick-again", 1]
    assert IDLE_SLEEP_SECONDS == 1
    assert thread.is_alive() is False
