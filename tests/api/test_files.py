import asyncio
from types import SimpleNamespace

import pytest

from api_app.routes.files import read_asset_upload
from video_agent.errors import VideoAgentError
from video_agent.files import LocalFileStore

from .conftest import upload


def test_image_and_audio_upload(api, auth, png):
    asset_id = upload(api, auth, png)
    response = api.client.get(f"/api/v1/assets/{asset_id}/content", headers=auth)
    assert response.status_code == 200 and response.content == png
    audio = b"RIFF" + (36).to_bytes(4, "little") + b"WAVEfmt " + b"\x00" * 24
    response = api.client.post("/api/v1/assets", headers=auth, data={"kind": "audio"}, files={"file": ("song.wav", audio, "text/plain")})
    assert response.status_code == 201 and response.json()["media_type"] == "audio/wav"
    assert "path" not in response.text


def test_oversized_upload_rejected_before_full_read():
    seen = []
    prefix = b'--x\r\nContent-Disposition: form-data; name="file"; filename="a.png"\r\n\r\n'
    async def stream():
        for chunk in (prefix, b"12345", b"67890", b"should-not-be-consumed"):
            seen.append(chunk)
            yield chunk
    request = SimpleNamespace(headers={"content-type": "multipart/form-data; boundary=x", "content-length": "1"}, stream=stream)
    with pytest.raises(VideoAgentError) as error:
        asyncio.run(read_asset_upload(request, 4))
    assert error.value.code == "FILE_TOO_LARGE"
    assert seen == [prefix, b"12345"]


def test_oversize_http_413_ignores_forged_content_length(api, auth, admin):
    revision = api.settings.read_settings().configuration_revision
    from video_agent.config import SettingsPatch
    api.settings.save_settings(SettingsPatch(max_file_bytes=4), expected_version=revision)
    response = api.client.post("/api/v1/assets", headers={**auth, "Content-Length": "1"}, data={"kind": "image"}, files={"file": ("a.png", b"12345", "image/png")})
    assert response.status_code == 413
    with api.agent.db.transaction() as connection:
        assert connection.execute("SELECT COUNT(*) AS n FROM assets").fetchone()["n"] == 0
        assert connection.execute("SELECT COUNT(*) AS n FROM work_items").fetchone()["n"] == 0


def test_unknown_or_escaped_file_path_rejected(api, auth, png, tmp_path):
    assert api.client.get("/api/v1/assets/missing/content", headers=auth).status_code == 404
    assert api.client.get("/api/v1/artifacts/missing/content", headers=auth).status_code == 404
    asset_id = upload(api, auth, png)
    with api.agent.db.transaction() as connection:
        connection.execute("UPDATE assets SET relative_path=:path WHERE asset_id=:id", {"path": "../outside.png", "id": asset_id})
    response = api.client.get(f"/api/v1/assets/{asset_id}/content", headers=auth)
    assert response.status_code in {404, 422}
    assert str(tmp_path) not in response.text
    for path in (str(tmp_path / "inside.png"), "C:/Windows/win.ini", "\\\\server\\share\\file", "C:relative.txt", "assets/a.png:secret"):
        with pytest.raises(VideoAgentError):
            LocalFileStore(tmp_path).path_for(path)


@pytest.mark.parametrize("filename", ["NUL.png", "a.png:secret", "bad?.png", "a. "])
def test_malicious_filename_is_rejected(api, auth, png, filename):
    response = api.client.post("/api/v1/assets", headers=auth, data={"kind": "image"}, files={"file": (filename, png, "image/png")})
    assert response.status_code == 422


def test_video_range_download(api, auth, tmp_path, monkeypatch):
    # The range contract is tested independently of the real FFmpeg end-to-end case.
    path = tmp_path / "range.mp4"
    path.write_bytes(bytes(range(100)))
    monkeypatch.setattr(api.agent, "artifact_path", lambda artifact_id: path)
    response = api.client.get("/api/v1/artifacts/example/content", headers={**auth, "Range": "bytes=3-7"})
    assert response.status_code == 206
    assert response.content == bytes(range(3, 8))
    assert response.headers["content-range"] == "bytes 3-7/100"
    assert api.client.get("/api/v1/artifacts/example/content", headers={**auth, "Range": "bytes=100-200"}).status_code == 416
