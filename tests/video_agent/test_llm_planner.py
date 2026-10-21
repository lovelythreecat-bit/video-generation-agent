import json

import pytest

from video_agent.errors import VideoAgentError
from video_agent.llm_planner import interpret_plan
from video_agent.models import AudioOptions, ProductInfo
from video_agent.ports import PlanRequest, VideoCapabilities


def _request() -> PlanRequest:
    return PlanRequest(
        product_info=ProductInfo(name="杯子", selling_points=["轻"]),
        user_prompt="展示杯身",
        images=["img-1"],
        duration_seconds=10,
        duration_tiers=[5, 10, 15, 30],
        aspect_ratio="9:16",
        subtitles=True,
        voiceover=False,
        music=False,
        audio=AudioOptions(),
        first_frame_asset_id="img-1",
        video_capabilities=VideoCapabilities(
            native_aspects=["9:16", "16:9", "1:1"],
            max_segment_seconds=15,
            supports_first_frame=True,
            supports_reference_images=False,
            supports_tail_frame=True,
        ),
    )


def _plan() -> dict:
    return {
        "questions": None,
        "creative": "展示杯子",
        "audience": "想买杯子的人",
        "selling_points": ["轻"],
        "shared_visual": {
            "product_reference": "白色杯子",
            "scene": "桌面",
            "lighting": "柔和",
            "palette": "白",
            "style": "写实",
        },
        "shots": [
            {
                "content": "杯身",
                "action": "旋转",
                "shot_size": "中景",
                "camera": "固定",
                "scene_id": "scene-1",
                "seconds": 5,
                "subtitle_text": "很轻",
                "voiceover_text": "",
            },
            {
                "content": "杯口",
                "action": "推近",
                "shot_size": "特写",
                "camera": "推进",
                "scene_id": "scene-1",
                "seconds": 5,
                "subtitle_text": "好拿",
                "voiceover_text": "",
            },
        ],
        "music_direction": "",
        "language": "zh",
    }


def test_plan_json_becomes_contiguous_frames():
    result = interpret_plan(json.dumps(_plan()), _request(), "note")
    document = result.document
    assert document is not None
    assert document.duration_seconds == 10
    assert [(shot.start_frame, shot.end_frame) for shot in document.shots] == [(0, 150), (150, 300)]
    assert document.first_frame_asset_id == "img-1"
    assert document.model_capabilities == "note"


@pytest.mark.parametrize("blank_shots", [[0], [0, 1]])
def test_native_text_can_be_omitted_on_any_shot(blank_shots):
    payload = _plan()
    for index in blank_shots:
        payload["shots"][index]["subtitle_text"] = ""
    result = interpret_plan(json.dumps(payload), _request(), "note")
    assert result.document.subtitles is True
    assert [shot.subtitle_text for shot in result.document.shots] == [
        item["subtitle_text"] for item in payload["shots"]
    ]


def test_revision_can_enable_native_text_with_blank_shots():
    from video_agent.llm_planner import interpret_revision

    request = _request()
    current = interpret_plan(json.dumps(_plan()), request, "note").document
    current.subtitles = False
    payload = _plan()
    payload["subtitles"] = True
    payload["shots"][1]["subtitle_text"] = ""
    result = interpret_revision(json.dumps(payload), current, request)
    assert result.patch.subtitles is True
    assert result.patch.shots[0].subtitle_text == "很轻"
    assert result.patch.shots[1].subtitle_text == ""


def test_plan_rejects_seconds_that_do_not_add_up():
    payload = _plan()
    payload["shots"][1]["seconds"] = 4
    with pytest.raises(VideoAgentError) as caught:
        interpret_plan(json.dumps(payload), _request(), "note")
    assert "加起来是 9" in caught.value.message


def test_questions_stop_the_plan():
    result = interpret_plan(json.dumps({"questions": ["请确认颜色"]}), _request(), "note")
    assert result.document is None
    assert result.questions == ["请确认颜色"]


def test_integer_scene_id_is_stored_as_decimal_text():
    payload = _plan()
    payload["shots"][0]["scene_id"] = 1
    payload["shots"][1]["scene_id"] = 1
    result = interpret_plan(json.dumps(payload), _request(), "note")
    document = result.document
    assert document is not None
    assert [shot.scene_id for shot in document.shots] == ["1", "1"]


def test_whole_float_scene_id_drops_the_decimal():
    payload = _plan()
    payload["shots"][0]["scene_id"] = 1.0
    payload["shots"][1]["scene_id"] = 1.0
    result = interpret_plan(json.dumps(payload), _request(), "note")
    document = result.document
    assert document is not None
    assert [shot.scene_id for shot in document.shots] == ["1", "1"]


def test_string_scene_id_stays_unchanged():
    result = interpret_plan(json.dumps(_plan()), _request(), "note")
    document = result.document
    assert document is not None
    assert [shot.scene_id for shot in document.shots] == ["scene-1", "scene-1"]


@pytest.mark.parametrize("scene_id", [True, False, 1.5, {"n": 1}, ["1"]])
def test_non_whole_scene_id_is_rejected(scene_id):
    payload = _plan()
    payload["shots"][0]["scene_id"] = scene_id
    with pytest.raises(VideoAgentError) as caught:
        interpret_plan(json.dumps(payload), _request(), "note")
    assert caught.value.message == "文本大模型的场景编号格式不对"


@pytest.mark.parametrize("scene_id", [None, "", "   "])
def test_blank_scene_id_is_rejected(scene_id):
    payload = _plan()
    payload["shots"][0]["scene_id"] = scene_id
    with pytest.raises(VideoAgentError) as caught:
        interpret_plan(json.dumps(payload), _request(), "note")
    assert caught.value.message == "文本大模型没有给出场景编号"


def test_missing_scene_id_is_rejected():
    payload = _plan()
    del payload["shots"][0]["scene_id"]
    with pytest.raises(VideoAgentError) as caught:
        interpret_plan(json.dumps(payload), _request(), "note")
    assert caught.value.message == "文本大模型没有给出场景编号"


@pytest.mark.parametrize(
    ("field", "label"),
    [
        ("content", "镜头内容"),
        ("action", "镜头动作"),
        ("shot_size", "景别"),
        ("camera", "运镜"),
        ("subtitle_text", "字幕"),
        ("voiceover_text", "配音"),
    ],
)
def test_numeric_shot_text_is_still_rejected(field, label):
    payload = _plan()
    payload["shots"][0][field] = 1
    with pytest.raises(VideoAgentError) as caught:
        interpret_plan(json.dumps(payload), _request(), "note")
    assert caught.value.message == f"文本大模型的{label}格式不对"


def _repair_planner(responses):
    from video_agent.llm_planner import OpenAIChatPlanner
    calls = []

    def transport(method, url, **kwargs):
        calls.append(kwargs['body'])
        response = responses[min(len(calls) - 1, len(responses) - 1)]
        if isinstance(response, int):
            return response, {'error': {'message': 'provider rejected'}}
        return 200, {'choices': [{'message': {'content': json.dumps(response)}}]}

    return OpenAIChatPlanner(base_url='https://example.test/v1', api_key='test-key',
                             model='planner', transport=transport), calls


def test_planner_repairs_nine_second_output_for_five_second_request():
    request = _request()
    request.duration_seconds = 5
    bad = _plan()
    bad['shots'][1]['seconds'] = 4
    good = _plan()
    good['shots'] = good['shots'][:1]
    planner, calls = _repair_planner([bad, good])
    result = planner.plan(request)
    assert result.document.duration_seconds == 5
    assert result.document.shots[-1].end_frame == 150
    assert len(calls) == 2
    initial, repair = [json.loads(call['messages'][1]['content']) for call in calls]
    assert repair['brief'] == initial['brief']
    assert repair['task'] == 'plan'
    assert '9' in repair['repair']['validation_error']
    assert json.loads(repair['repair']['previous_output']) == bad


def test_planner_repair_is_bounded_and_does_not_accept_invalid_timing():
    bad = _plan()
    bad['shots'][1]['seconds'] = 4
    planner, calls = _repair_planner([bad])
    with pytest.raises(VideoAgentError, match='加起来是 9'):
        planner.plan(_request())
    assert len(calls) == 2


def test_planner_repair_preserves_revision_and_current_plan():
    request = _request()
    current = interpret_plan(json.dumps(_plan()), request, 'note').document
    bad = _plan()
    bad['shots'][1]['seconds'] = 4
    planner, calls = _repair_planner([bad, _plan()])
    result = planner.revise(current, '加强杯口特写', request)
    assert result.patch.shots[-1].end_frame == 300
    assert len(calls) == 2
    initial, repair = [json.loads(call['messages'][1]['content']) for call in calls]
    for key in ('task', 'instruction', 'current_plan', 'brief', 'allowed_durations',
                'allowed_aspect_ratios'):
        assert repair[key] == initial[key]


@pytest.mark.parametrize('response', [_plan(), {'questions': ['请确认颜色']}])
def test_valid_planner_response_does_not_trigger_repair(response):
    planner, calls = _repair_planner([response])
    planner.plan(_request())
    assert len(calls) == 1


def test_planner_http_rejection_is_not_a_malformed_output_repair():
    planner, calls = _repair_planner([401])
    with pytest.raises(VideoAgentError):
        planner.plan(_request())
    assert len(calls) == 1


def test_revision_repair_keeps_requested_duration_change():
    request = _request()
    current = interpret_plan(json.dumps(_plan()), request, 'note').document
    bad = _plan()
    bad['duration_seconds'] = 5
    bad['shots'][1]['seconds'] = 4
    good = _plan()
    good['duration_seconds'] = 5
    good['subtitles'] = False
    good['shots'] = good['shots'][:1]
    planner, calls = _repair_planner([bad, good])
    result = planner.revise(current, '改为5秒，关闭字幕', request)
    assert result.patch.duration_seconds == 5
    assert result.patch.subtitles is False
    assert result.patch.shots[-1].end_frame == 150
    repair = json.loads(calls[1]['messages'][1]['content'])
    assert repair['instruction'] == '改为5秒，关闭字幕'
    guidance = repair['repair']['instruction']
    assert '严格遵守 brief 的总时长' not in guidance
    assert 'instruction' in guidance and 'allowed_durations' in guidance
