import base64
import io
import json
import shutil
import subprocess

import pytest
from PIL import Image

from video_agent.errors import VideoAgentError
from video_agent.media import probe, still_clip
from video_agent.models import SharedVisual
from video_agent.ports import ShotRequest
from video_agent.wan_video import WanVideo

_SYNTHESIS = "/services/aigc/video-generation/video-synthesis"
_BASE = "https://llm.example.invalid/api/v1"


def _image(path):
    Image.new("RGB", (16, 16), "white").save(path)


def _request(path, key="idem-1", *, frame_count=90, with_audio=False, width=1080, height=1920) -> ShotRequest:
    return ShotRequest(
        generation_id="g1",
        shot_id="s1",
        prompt="杯子缓慢旋转",
        shared_visual=SharedVisual(
            product_reference="白杯",
            scene="桌面",
            lighting="柔和",
            palette="白",
            style="写实",
        ),
        first_frame_path=path,
        reference_paths=[],
        tail_frame_path=None,
        frame_count=frame_count,
        aspect_ratio="9:16",
        fit="native",
        width=width,
        height=height,
        provider_idempotency_key=key,
        with_audio=with_audio,
    )


def _wan(tmp_path, transport, *, model="wan2.2-i2v-flash", byte_transport=None, base_url=_BASE, max_segment_seconds="5"):
    return WanVideo(
        base_url=base_url,
        api_key="sk-test",
        model=model,
        max_segment_seconds=max_segment_seconds,
        state_path=tmp_path / "provider-state" / "wan-tasks.json",
        transport=transport,
        byte_transport=byte_transport,
    )


def _need_ffmpeg() -> None:
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.skip("ffmpeg and ffprobe are required")


def _capture(payload):
    calls = []

    def transport(method, url, *, headers, body, timeout):
        calls.append({"method": method, "url": url, "headers": headers, "body": body, "timeout": timeout})
        return 200, payload

    return calls, transport


def test_capabilities_use_configured_segment_limit():
    video = WanVideo(
        base_url=_BASE,
        api_key="sk-test",
        model="wan2.6-i2v-flash",
        max_segment_seconds="15",
    )
    caps = video.capabilities()
    assert caps.native_aspects == ["9:16", "16:9", "1:1", "4:5", "3:4", "4:3", "3:2", "2:3"]
    assert caps.max_segment_seconds == 15
    assert caps.supports_first_frame is True
    assert caps.supports_reference_images is False
    assert caps.supports_tail_frame is False
    assert WanVideo(base_url=_BASE, api_key="k", model="m", max_segment_seconds="1").capabilities().max_segment_seconds == 1


@pytest.mark.parametrize("raw", ["", "0", "16", "1.5", "-3", "  "])
def test_invalid_segment_seconds_are_rejected(raw):
    with pytest.raises(VideoAgentError) as caught:
        WanVideo(base_url=_BASE, api_key="k", model="wan2.2", max_segment_seconds=raw)
    assert caught.value.code == "VALIDATION_ERROR"
    if raw.strip() == "":
        assert caught.value.message == "视频生成还没填 单段最长秒数"
    else:
        assert "要填 1 到 15 的整数" in caught.value.message


@pytest.mark.parametrize("model", ["wan2.2-i2v-flash", "wanx2.1-turbo"])
def test_short_wan22_submits_duration_five_without_audio(tmp_path, model):
    image = tmp_path / "frame.png"
    tail = tmp_path / "tail.png"
    _image(image)
    _image(tail)
    calls, transport = _capture({"id": "ignored", "output": {"task_id": "task-22", "id": "other"}})
    video = _wan(tmp_path, transport, model=model)
    request = _request(image, with_audio=True).model_copy(
        update={"tail_frame_path": tail, "reference_paths": [tail]}
    )
    submitted = video.submit_shot(request)
    assert submitted.external_task_id == "task-22"
    assert submitted.uncertain is False
    call = calls[0]
    assert call["method"] == "POST"
    assert call["url"] == _BASE + _SYNTHESIS
    assert call["timeout"] == 60
    assert call["headers"]["Authorization"] == "Bearer sk-test"
    assert call["headers"]["Content-Type"] == "application/json"
    assert call["headers"]["X-DashScope-Async"] == "enable"
    body = call["body"]
    assert set(body) == {"model", "input", "parameters"}
    assert body["model"] == model
    assert body["input"]["prompt"] == "杯子缓慢旋转"
    assert set(body["input"]) == {"prompt", "img_url"}
    encoded = body["input"]["img_url"].removeprefix("data:image/jpeg;base64,")
    assert encoded != body["input"]["img_url"]
    opened = Image.open(io.BytesIO(base64.b64decode(encoded)))
    assert opened.format == "JPEG"
    assert max(opened.size) <= 1280
    assert json.dumps(body).count("data:image/jpeg;base64,") == 1
    assert set(body["parameters"]) == {"resolution", "duration", "prompt_extend", "watermark"}
    assert body["parameters"]["resolution"] == "720P"
    assert body["parameters"]["duration"] == 5
    assert body["parameters"]["prompt_extend"] is False
    assert body["parameters"]["watermark"] is False


@pytest.mark.parametrize("with_audio", [True, False])
def test_wan26_six_seconds_rounds_up_and_follows_audio(tmp_path, with_audio):
    image = tmp_path / "frame.png"
    _image(image)
    calls, transport = _capture({"output": {"task_id": "task-26"}})
    video = _wan(tmp_path, transport, model="wan2.6-i2v-flash")
    submitted = video.submit_shot(_request(image, frame_count=180, with_audio=with_audio))
    assert submitted.external_task_id == "task-26"
    body = calls[0]["body"]
    assert body["model"] == "wan2.6-i2v-flash"
    assert set(body["input"]) == {"prompt", "img_url"}
    parameters = body["parameters"]
    assert parameters["duration"] == 10
    assert parameters["audio"] is with_audio


def test_wan25_also_sends_audio(tmp_path):
    image = tmp_path / "frame.png"
    _image(image)
    calls, transport = _capture({"output": {"task_id": "task-25"}})
    video = _wan(tmp_path, transport, model="Wan2.5-I2V")
    video.submit_shot(_request(image, frame_count=90, with_audio=True))
    body = calls[0]["body"]
    assert set(body["input"]) == {"prompt", "img_url"}
    assert body["parameters"]["duration"] == 5
    assert body["parameters"]["audio"] is True


def test_wan22_six_seconds_submits_shot_seconds(tmp_path):
    image = tmp_path / "frame.png"
    _image(image)
    calls, transport = _capture({"output": {"task_id": "task-long"}})
    video = _wan(tmp_path, transport, model="wan2.2")
    video.submit_shot(_request(image, frame_count=180, with_audio=True))
    body = calls[0]["body"]
    assert set(body["input"]) == {"prompt", "img_url"}
    parameters = body["parameters"]
    assert parameters["duration"] == 6
    assert "audio" not in parameters


@pytest.mark.parametrize("model", ["custom-video", "wan2.7-i2v"])
def test_other_model_submits_shot_seconds_without_audio(tmp_path, model):
    image = tmp_path / "frame.png"
    _image(image)
    calls, transport = _capture({"task_id": "task-other"})
    video = _wan(tmp_path, transport, model=model)
    submitted = video.submit_shot(_request(image, frame_count=120, with_audio=True))
    assert submitted.external_task_id == "task-other"
    body = calls[0]["body"]
    assert body["model"] == model
    assert set(body["input"]) == {"prompt", "img_url"}
    parameters = body["parameters"]
    assert parameters["duration"] == 4
    assert "audio" not in parameters


def _assert_jpeg_data_url(url: str) -> None:
    encoded = url.removeprefix("data:image/jpeg;base64,")
    assert encoded != url
    opened = Image.open(io.BytesIO(base64.b64decode(encoded)))
    assert opened.format == "JPEG"
    assert max(opened.size) <= 1280


@pytest.mark.parametrize("model", ["wan3.0-video", "wan3.0-video-prime"])
def test_wan30_submits_first_frame_media(tmp_path, model):
    image = tmp_path / "frame.png"
    _image(image)
    calls, transport = _capture({"output": {"task_id": "task-30"}})
    video = _wan(tmp_path, transport, model=model)
    video.submit_shot(_request(image, frame_count=90, with_audio=False))
    body = calls[0]["body"]
    assert set(body) == {"model", "input", "parameters"}
    assert body["model"] == model
    assert body["input"]["prompt"] == "杯子缓慢旋转"
    assert set(body["input"]) == {"prompt", "media"}
    assert "img_url" not in body["input"]
    media = body["input"]["media"]
    assert len(media) == 1
    assert set(media[0]) == {"type", "url"}
    assert media[0]["type"] == "first_frame"
    _assert_jpeg_data_url(media[0]["url"])
    assert json.dumps(body).count("data:image/jpeg;base64,") == 1
    parameters = body["parameters"]
    assert set(parameters) == {"resolution", "duration", "prompt_extend", "watermark", "audio"}
    assert parameters["resolution"] == "720P"
    assert parameters["duration"] == 3
    assert parameters["prompt_extend"] is False
    assert parameters["watermark"] is False
    assert parameters["audio"] is False
    assert "ratio" not in parameters


def test_wan30_mixed_case_submits_first_frame_media(tmp_path):
    image = tmp_path / "frame.png"
    _image(image)
    calls, transport = _capture({"output": {"task_id": "task-30-case"}})
    video = _wan(tmp_path, transport, model="Wan3.0-Video")
    video.submit_shot(_request(image, with_audio=True))
    body = calls[0]["body"]
    assert body["model"] == "Wan3.0-Video"
    assert set(body["input"]) == {"prompt", "media"}
    assert body["input"]["media"][0]["type"] == "first_frame"
    assert body["input"]["media"][0]["url"].startswith("data:image/jpeg;base64,")
    assert "img_url" not in body["input"]
    assert "ratio" not in body["parameters"]


@pytest.mark.parametrize("with_audio", [True, False])
def test_wan30_one_second_submits_duration_two_and_audio(tmp_path, with_audio):
    image = tmp_path / "frame.png"
    _image(image)
    calls, transport = _capture({"output": {"task_id": "task-30-short"}})
    video = _wan(tmp_path, transport, model="wan3.0-video")
    video.submit_shot(_request(image, frame_count=30, with_audio=with_audio))
    parameters = calls[0]["body"]["parameters"]
    assert parameters["duration"] == 2
    assert parameters["audio"] is with_audio


def test_wan30_six_seconds_submits_shot_seconds(tmp_path):
    image = tmp_path / "frame.png"
    _image(image)
    calls, transport = _capture({"output": {"task_id": "task-30-six"}})
    video = _wan(tmp_path, transport, model="wan3.0-video-prime")
    video.submit_shot(_request(image, frame_count=180, with_audio=True))
    parameters = calls[0]["body"]["parameters"]
    assert parameters["duration"] == 6
    assert parameters["audio"] is True


@pytest.mark.parametrize(("frame_count", "duration"), [(60, 2), (900, 30), (930, 31)])
def test_wan30_duration_keeps_bounds(tmp_path, frame_count, duration):
    image = tmp_path / "frame.png"
    _image(image)
    calls, transport = _capture({"output": {"task_id": "task-30-bound"}})
    video = _wan(tmp_path, transport, model="wan3.0-video")
    video.submit_shot(_request(image, frame_count=frame_count, with_audio=False))
    parameters = calls[0]["body"]["parameters"]
    assert parameters["duration"] == duration
    assert parameters["audio"] is False


def test_same_idempotency_key_does_not_post_again(tmp_path):
    image = tmp_path / "frame.png"
    _image(image)
    posts = {"count": 0}

    def transport(method, url, *, headers, body, timeout):
        assert method == "POST"
        posts["count"] += 1
        return 200, {"output": {"task_id": "task-once"}}

    video = _wan(tmp_path, transport)
    request = _request(image, key="same-key", width=32, height=48)
    assert video.submit_shot(request).external_task_id == "task-once"
    assert video.submit_shot(request).external_task_id == "task-once"
    assert posts["count"] == 1
    stored = json.loads((tmp_path / "provider-state" / "wan-tasks.json").read_text(encoding="utf-8"))
    assert stored["same-key"] == {"task_id": "task-once", "width": 32, "height": 48}

    def refuse(method, url, *, headers, body, timeout):
        raise AssertionError("same idempotency key must not POST")

    restored = _wan(tmp_path, refuse)
    assert restored.submit_shot(request).external_task_id == "task-once"


def test_submit_url_is_not_repeated_and_query_strips_it(tmp_path):
    image = tmp_path / "frame.png"
    _image(image)
    calls, transport = _capture({"output": {"task_id": "task-url"}})
    base = _BASE + _SYNTHESIS
    video = _wan(tmp_path, transport, base_url=base + "/")
    video.submit_shot(_request(image))
    video.query(external_task_id="task-url", provider_idempotency_key="idem-1")
    assert calls[0]["method"] == "POST"
    assert calls[0]["url"] == base
    assert calls[0]["timeout"] == 60
    assert calls[1]["method"] == "GET"
    assert calls[1]["url"] == _BASE + "/tasks/task-url"
    assert calls[1]["timeout"] == 30
    assert "X-DashScope-Async" not in calls[1]["headers"]


def test_post_network_error_is_uncertain(tmp_path):
    image = tmp_path / "frame.png"
    _image(image)

    def transport(method, url, *, headers, body, timeout):
        raise TimeoutError("timed out")

    video = _wan(tmp_path, transport)
    with pytest.raises(VideoAgentError) as caught:
        video.submit_shot(_request(image))
    assert caught.value.code == "PROVIDER_SUBMISSION_UNCERTAIN"
    assert caught.value.retryable is True


def test_query_reads_only_output_task_status():
    def run(payload, status_code=200):
        def transport(method, url, *, headers, body, timeout):
            assert method == "GET"
            assert url == _BASE + "/tasks/task-9"
            assert timeout == 30
            return status_code, payload

        video = WanVideo(base_url=_BASE, api_key="sk-test", model="wan2.6", max_segment_seconds="5", transport=transport)
        return video.query(external_task_id="task-9", provider_idempotency_key="idem")

    assert run({"output": {}, "task_status": "SUCCEEDED"}).state == "pending"
    assert run({"output": {"task_status": "PENDING"}}).state == "pending"
    assert run({"output": {"task_status": "RUNNING"}}).state == "pending"
    assert run({"output": {"task_status": "SUCCEEDED"}}).state == "succeeded"
    failed = run({"output": {"task_status": "FAILED", "message": "坏了", "code": "Ignored"}})
    assert failed.state == "failed"
    assert failed.error_code == "PROVIDER_REJECTED"
    assert failed.message == "坏了"
    assert run({"output": {"task_status": "CANCELED", "code": "CancelCode"}}).message == "CancelCode"
    assert run({"output": {"task_status": "CANCELLED"}}).state == "failed"
    assert run({"output": {"task_status": "FAILED"}, "message": "顶层原因"}).message == "顶层原因"
    long_message = "字" * 400
    assert run({"output": {"task_status": "FAILED", "message": long_message}}).message == "字" * 300
    assert run({"output": {"task_status": "UNKNOWN"}}).state == "absent"
    assert run({"message": "missing"}, status_code=404).state == "absent"


def test_missing_first_frame_is_rejected(tmp_path):
    def transport(method, url, *, headers, body, timeout):
        raise AssertionError("missing first frame must not POST")

    video = _wan(tmp_path, transport)
    for path in (None, tmp_path / "missing.png"):
        with pytest.raises(VideoAgentError) as caught:
            video.submit_shot(_request(path))
        assert caught.value.code == "PROVIDER_REJECTED"
        assert caught.value.message == "视频生成需要首帧图片"
        assert caught.value.retryable is True


def test_missing_task_id_is_uncertain(tmp_path):
    image = tmp_path / "frame.png"
    _image(image)

    def transport(method, url, *, headers, body, timeout):
        return 200, {"output": {"id": "  "}, "request_id": "r1"}

    video = _wan(tmp_path, transport)
    with pytest.raises(VideoAgentError) as caught:
        video.submit_shot(_request(image))
    assert caught.value.code == "PROVIDER_SUBMISSION_UNCERTAIN"
    assert caught.value.retryable is True


def test_download_without_url_is_temporary(tmp_path):
    image = tmp_path / "frame.png"
    _image(image)

    def transport(method, url, *, headers, body, timeout):
        if method == "POST":
            return 200, {"output": {"task_id": "task-empty"}}
        return 200, {"output": {"task_status": "SUCCEEDED"}}

    video = _wan(tmp_path, transport)
    video.submit_shot(_request(image, width=32, height=48))
    with pytest.raises(VideoAgentError) as caught:
        video.download("task-empty")
    assert caught.value.code == "PROVIDER_TEMPORARY"
    assert caught.value.retryable is True


def _clip_bytes(tmp_path, image, *, with_audio: bool) -> bytes:
    silent = tmp_path / "silent.mp4"
    still_clip(image, 6, 64, 96, silent, video_bitrate=200_000)
    if not with_audio:
        return silent.read_bytes()
    mixed = tmp_path / "mixed.mp4"
    subprocess.run(
        [
            "ffmpeg", "-y", "-i", str(silent),
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
            "-t", "0.2", "-c:v", "copy", "-c:a", "aac", "-shortest", str(mixed),
        ],
        check=True,
        capture_output=True,
        stdin=subprocess.DEVNULL,
    )
    return mixed.read_bytes()


def _assert_scaled(tmp_path, data: bytes, *, has_audio: bool) -> None:
    dest = tmp_path / ("scaled-audio.mp4" if has_audio else "scaled-silent.mp4")
    dest.write_bytes(data)
    info = probe(dest)
    assert info.width == 32
    assert info.height == 48
    assert info.has_audio is has_audio
    assert info.video_codec == "h264"
    if has_audio:
        assert info.audio_codec == "aac"
    else:
        assert info.audio_codec is None
    completed = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=pix_fmt,avg_frame_rate", "-of", "json", str(dest),
        ],
        check=True,
        capture_output=True,
        stdin=subprocess.DEVNULL,
    )
    stream = json.loads(completed.stdout.decode("utf-8"))["streams"][0]
    assert stream["pix_fmt"] == "yuv420p"
    num, den = str(stream["avg_frame_rate"]).split("/")
    assert int(num) / int(den) == 30


@pytest.mark.parametrize("with_audio", [True, False])
def test_download_scales_to_32_by_48(tmp_path, with_audio):
    _need_ffmpeg()
    image = tmp_path / "frame.png"
    _image(image)
    source = _clip_bytes(tmp_path, image, with_audio=with_audio)
    source_info = probe(tmp_path / ("mixed.mp4" if with_audio else "silent.mp4"))
    assert (source_info.width, source_info.height) == (64, 96)
    assert source_info.has_audio is with_audio

    def transport(method, url, *, headers, body, timeout):
        if method == "POST":
            return 200, {"output": {"task_id": "task-dl"}}
        assert timeout == 30
        assert "X-DashScope-Async" not in headers
        return 200, {"output": {"video_url": "https://cdn.example/a.mp4"}}

    def byte_transport(method, url, *, headers, body, timeout):
        assert method == "GET"
        assert url == "https://cdn.example/a.mp4"
        assert timeout == 120
        return 200, source

    video = _wan(tmp_path, transport, byte_transport=byte_transport)
    request = _request(image, width=32, height=48)
    assert video.submit_shot(request).external_task_id == "task-dl"
    _assert_scaled(tmp_path, video.download("task-dl"), has_audio=with_audio)
