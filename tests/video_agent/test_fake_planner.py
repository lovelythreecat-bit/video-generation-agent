from video_agent.models import AudioOptions, ProductInfo
from video_agent.ports import PlanRequest, VideoCapabilities


def _request(**overrides) -> PlanRequest:
    payload = dict(
        product_info=ProductInfo(name="杯子", selling_points=["耐热"]),
        user_prompt="放在桌上",
        images=["img-1"],
        duration_seconds=10,
        duration_tiers=[5, 10, 15, 30],
        aspect_ratio="9:16",
        subtitles=True,
        voiceover=True,
        music=True,
        audio=AudioOptions(),
        first_frame_asset_id=None,
        video_capabilities=VideoCapabilities(
            native_aspects=["9:16"],
            max_segment_seconds=30,
            supports_first_frame=True,
            supports_reference_images=True,
            supports_tail_frame=True,
        ),
    )
    payload.update(overrides)
    return PlanRequest(**payload)


def test_plan_copies_selling_points_and_makes_one_shot():
    from video_agent.fake_planner import FakePlanner

    document = FakePlanner().plan(_request()).document
    assert document is not None
    assert document.selling_points == ["耐热"]
    assert document.duration_seconds == 10
    assert document.fit == "native"
    assert document.aspect_ratio == "9:16"
    assert len(document.shots) == 1
    shot = document.shots[0]
    assert shot.shot_id == "s1"
    assert shot.start_frame == 0
    assert shot.end_frame == 300
    assert shot.subtitle_text == "耐热"
    assert shot.voiceover_text == "杯子"
    assert document.first_frame_source == "generate"
    assert document.first_frame_asset_id is None


def test_plan_does_not_invent_claims():
    from video_agent.fake_planner import FakePlanner

    document = FakePlanner().plan(_request()).document
    assert document is not None
    text = document.model_dump_json()
    for word in ("价格", "功效", "认证", "最好", "第一", "100%", "绝对"):
        assert word not in text


def test_non_native_aspect_uses_pad():
    from video_agent.fake_planner import FakePlanner

    document = FakePlanner().plan(_request(aspect_ratio="1:1")).document
    assert document is not None
    assert document.fit == "pad"
    assert document.aspect_ratio == "1:1"


def test_prompt_seconds_outside_tiers_do_not_change_duration():
    from video_agent.fake_planner import FakePlanner

    document = FakePlanner().plan(_request(user_prompt="做成12秒和8秒")).document
    assert document is not None
    assert document.duration_seconds == 10
    assert "5、10、15、30" in document.model_capabilities
    assert "12、8" in document.model_capabilities


def test_closed_switches_create_no_text_dependency():
    from video_agent.fake_planner import FakePlanner

    document = FakePlanner().plan(
        _request(subtitles=False, voiceover=False, music=False, audio=AudioOptions(music_asset_id="m1"))
    ).document
    assert document is not None
    assert document.shots[0].subtitle_text == ""
    assert document.shots[0].voiceover_text == ""
    assert document.music_direction == ""
    assert document.music_asset_id is None


def test_selected_first_frame_and_music_asset_are_copied():
    from video_agent.fake_planner import FakePlanner

    document = FakePlanner().plan(
        _request(
            first_frame_asset_id="img-1",
            audio=AudioOptions(music_direction="钢琴", music_asset_id="m1"),
        )
    ).document
    assert document is not None
    assert document.first_frame_source == "selected"
    assert document.first_frame_asset_id == "img-1"
    assert document.music_direction == "钢琴"
    assert document.music_asset_id == "m1"


def test_language_conflict_blocks_only_when_speech_or_subtitles_are_on():
    from video_agent.fake_planner import FakePlanner

    blocked = FakePlanner().plan(_request(product_info=ProductInfo(name="杯子"), user_prompt="red cup on a table"))
    assert blocked.document is None
    assert blocked.questions is not None
    assert "中文" in blocked.questions[0]
    assert "英文" in blocked.questions[0]

    allowed = FakePlanner().plan(
        _request(
            product_info=ProductInfo(name="杯子"),
            user_prompt="red cup on a table",
            subtitles=False,
            voiceover=False,
        )
    )
    assert allowed.document is not None
    assert allowed.questions is None


def test_revise_understands_tier_aspect_and_rejects_unknown_text():
    from video_agent.fake_planner import FakePlanner

    planner = FakePlanner()
    request = _request()
    current = planner.plan(request).document
    assert current is not None
    changed = planner.revise(current, "改成15秒", request)
    assert changed.patch is not None
    assert changed.patch.duration_seconds == 15
    rejected = planner.revise(current, "改成12秒", request)
    assert rejected.patch is None
    assert rejected.questions is not None
    assert "5、10、15、30" in rejected.questions[0]
    padded = planner.revise(current, "改成1:1", request)
    assert padded.patch is not None
    assert padded.patch.aspect_ratio == "1:1"
    assert padded.patch.fit == "pad"
    native = planner.revise(current, "改成9:16", request)
    assert native.patch is not None
    assert native.patch.fit == "native"
    unknown = planner.revise(current, "随便写写", request)
    assert unknown.patch is None
    assert unknown.questions is None


def _apply(document, patch):
    from video_agent.state import apply_patch

    return apply_patch(
        document,
        patch,
        duration_tiers=[5, 10, 15, 30],
        native_aspects=["9:16"],
        image_asset_ids=["img-1"],
        audio_asset_ids=set(),
    )


def test_explicit_audio_language_skips_conflict_and_detection():
    from video_agent.fake_planner import FakePlanner

    result = FakePlanner().plan(
        _request(
            product_info=ProductInfo(name="杯子"),
            user_prompt="red cup on a table",
            audio=AudioOptions(language="en"),
        )
    )
    assert result.questions is None
    assert result.document is not None
    assert result.document.language == "en"


def test_enabling_subtitles_fills_blank_copy_apply_patch_accepts():
    from video_agent.fake_planner import FakePlanner

    planner = FakePlanner()
    request = _request(subtitles=False, voiceover=False, music=False)
    current = planner.plan(request).document
    assert current is not None
    revised = planner.revise(current, "开启字幕", request)
    assert revised.patch is not None
    assert revised.patch.subtitles is True
    assert revised.patch.shots is None
    updated = _apply(current, revised.patch)
    assert updated.subtitles is True
    assert updated.shots[0].subtitle_text == "耐热"


def test_enabling_voiceover_fills_blank_copy_apply_patch_accepts():
    from video_agent.fake_planner import FakePlanner

    planner = FakePlanner()
    request = _request(subtitles=False, voiceover=False, music=False)
    current = planner.plan(request).document
    assert current is not None
    revised = planner.revise(current, "开启配音", request)
    assert revised.patch is not None
    updated = _apply(current, revised.patch)
    assert updated.voiceover is True
    assert updated.shots[0].voiceover_text == "杯子"


def test_enabling_music_with_empty_audio_sets_default_direction():
    from video_agent.fake_planner import FakePlanner

    planner = FakePlanner()
    request = _request(subtitles=False, voiceover=False, music=False, audio=AudioOptions())
    current = planner.plan(request).document
    assert current is not None
    revised = planner.revise(current, "开启音乐", request)
    assert revised.patch is not None
    assert revised.patch.music_direction == "平稳背景"
    updated = _apply(current, revised.patch)
    assert updated.music is True
    assert updated.music_direction == "平稳背景"
