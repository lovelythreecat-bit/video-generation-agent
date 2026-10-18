from PIL import Image

from video_agent.models import SharedVisual
from video_agent.ports import ShotRequest
from video_agent.seedance_video import SeedanceVideo


def _image(path):
    Image.new("RGB", (16, 16), "white").save(path)


def _request(path, key="idem-1") -> ShotRequest:
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
        frame_count=150,
        aspect_ratio="9:16",
        fit="native",
        width=1080,
        height=1920,
        provider_idempotency_key=key,
    )


def test_submit_query_and_download_use_task_id(tmp_path):
    image = tmp_path / "frame.png"
    _image(image)
    calls = []

    def transport(method, url, *, headers, body, timeout):
        calls.append((method, url, body, headers["Authorization"]))
        if method == "POST":
            return 200, {"id": "cgt-1"}
        return 200, {"id": "cgt-1", "status": "succeeded", "content": {"video_url": "https://cdn.example/a.mp4"}}

    def byte_transport(method, url, *, headers, body, timeout):
        assert method == "GET"
        assert url == "https://cdn.example/a.mp4"
        return 200, b"mp4-bytes"

    video = SeedanceVideo(
        base_url="https://ark.cn-beijing.volces.com/api/v3",
        api_key="ark-test",
        model="doubao-seedance",
        state_path=tmp_path / "provider-state" / "seedance-tasks.json",
        transport=transport,
        byte_transport=byte_transport,
    )
    submitted = video.submit_shot(_request(image))
    assert submitted.external_task_id == "cgt-1"
    assert calls[0][0] == "POST"
    assert calls[0][1].endswith("/contents/generations/tasks")
    assert calls[0][2]["model"] == "doubao-seedance"
    assert calls[0][2]["duration"] == 5
    assert calls[0][2]["generate_audio"] is False
    assert calls[0][2]["content"][1]["role"] == "first_frame"
    assert calls[0][3] == "Bearer ark-test"
    assert video.query(external_task_id="cgt-1", provider_idempotency_key="idem-1").state == "succeeded"
    assert video.download("cgt-1") == b"mp4-bytes"
    stored = (tmp_path / "provider-state" / "seedance-tasks.json").read_text(encoding="utf-8")
    assert "idem-1" in stored

    def only_saved(method, url, *, headers, body, timeout):
        assert "cgt-1" in url
        return 200, {"id": "cgt-1", "status": "running"}

    restored = SeedanceVideo(
        base_url="https://ark.cn-beijing.volces.com/api/v3",
        api_key="ark-test",
        model="doubao-seedance",
        state_path=tmp_path / "provider-state" / "seedance-tasks.json",
        transport=only_saved,
    )
    assert restored.query(external_task_id=None, provider_idempotency_key="idem-1").state == "pending"


def test_sound_on_requests_model_audio(tmp_path):
    image = tmp_path / "frame.png"
    _image(image)

    def transport(method, url, *, headers, body, timeout):
        assert body["generate_audio"] is True
        return 200, {"id": "cgt-audio"}

    video = SeedanceVideo(
        base_url="https://ark.cn-beijing.volces.com/api/v3",
        api_key="ark-test",
        model="doubao-seedance",
        transport=transport,
    )
    request = _request(image).model_copy(update={"with_audio": True})
    assert video.submit_shot(request).external_task_id == "cgt-audio"


def test_missing_task_is_absent():
    def transport(method, url, *, headers, body, timeout):
        return 404, {"message": "missing"}

    video = SeedanceVideo(
        base_url="https://ark.cn-beijing.volces.com/api/v3",
        api_key="ark-test",
        model="doubao-seedance",
        transport=transport,
    )
    result = video.query(external_task_id="missing", provider_idempotency_key="none")
    assert result.state == "absent"
