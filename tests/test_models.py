from video_agent.models import (
    AudioOptions,
    PlanChange,
    PlanPatch,
    ProductInfo,
    RegenerateRequest,
    TaskView,
    VideoOptions,
)
from video_agent.ports import DEFAULT_VIDEO_CAPABILITIES, PlanRequest, Planner


def test_request_defaults_match_the_contract():
    options = VideoOptions()
    assert options.duration_seconds is None
    assert options.aspect_ratio == "9:16"
    assert options.subtitles is True
    assert options.voiceover is True
    assert options.music is True
    audio = AudioOptions()
    assert audio.language is None
    assert audio.voice_preference == ""
    assert audio.music_direction == ""
    assert audio.music_asset_id is None
    product = ProductInfo()
    assert product.selling_points == []
    assert product.extra == {}
    change = PlanChange(instruction="改成桌面场景")
    assert change.patch is None
    other = PlanChange(patch=PlanPatch(duration_seconds=15))
    assert other.instruction is None
    request = RegenerateRequest(kind="full")
    assert request.artifact_id is None
    assert request.shot_ids == []


def test_task_view_round_trip_keeps_field_names():
    view = TaskView(
        task_id="task-1",
        status="awaiting_input",
        questions=["缺少产品图片"],
        updated_at="2026-10-09T03:00:00+00:00",
    )
    payload = view.model_dump(mode="json")
    assert list(TaskView.model_fields) == [
        "task_id",
        "status",
        "stage",
        "stage_detail",
        "failure_scope",
        "failure",
        "questions",
        "external_user_id",
        "external_request_id",
        "head_plan_version",
        "confirmed_plan_version",
        "generation_id",
        "completed_shots",
        "total_shots",
        "product_name",
        "product_info",
        "user_prompt",
        "video_options",
        "audio_options",
        "image_asset_ids",
        "first_frame_asset_id",
        "updated_at",
    ]
    assert TaskView.model_validate(payload) == view


def test_default_video_capabilities_match_the_fake_video_contract():
    caps = DEFAULT_VIDEO_CAPABILITIES
    assert caps.native_aspects == ["9:16"]
    assert caps.max_segment_seconds == 30
    assert caps.supports_first_frame is True
    assert caps.supports_reference_images is True
    assert caps.supports_tail_frame is True
    request = PlanRequest(
        product_info=ProductInfo(name="瓶子"),
        user_prompt="",
        images=[],
        duration_seconds=10,
        duration_tiers=[5, 10, 15, 30],
        aspect_ratio="9:16",
        subtitles=True,
        voiceover=True,
        music=True,
        audio=AudioOptions(),
        first_frame_asset_id=None,
        video_capabilities=caps,
    )
    assert request.duration_seconds == 10
    assert isinstance(Planner, type)
