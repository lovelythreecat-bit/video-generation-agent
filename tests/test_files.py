import io
from pathlib import Path

import pytest
from PIL import Image

from video_agent.errors import VideoAgentError
from video_agent.files import LocalFileStore


def _image(fmt: str) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (2, 2), (10, 20, 30)).save(buffer, format=fmt)
    return buffer.getvalue()


def _wav() -> bytes:
    return b"RIFF" + (36).to_bytes(4, "little") + b"WAVE" + b"fmt " + b"\x00" * 24


def test_register_image_keeps_bytes_under_the_asset_directory(tmp_path: Path):
    store = LocalFileStore(tmp_path)
    stored = store.register(filename="../shot.png", kind="image", data=_image("PNG"), max_bytes=1000)
    assert stored.kind == "image"
    assert stored.media_type == "image/png"
    assert stored.filename == "shot.png"
    assert stored.relative_path == f"assets/{stored.asset_id}/shot.png"
    assert store.read_bytes(stored.relative_path) == _image("PNG")
    assert store.path_for(stored.relative_path).is_relative_to(tmp_path.resolve())


def test_register_accepts_jpeg_webp_and_audio_magic(tmp_path: Path):
    store = LocalFileStore(tmp_path)
    jpeg = store.register(filename="a.jpg", kind="image", data=_image("JPEG"), max_bytes=1000)
    webp = store.register(filename="a.webp", kind="image", data=_image("WEBP"), max_bytes=1000)
    wav = store.register(filename="a.wav", kind="audio", data=_wav(), max_bytes=1000)
    mp3 = store.register(filename="a.mp3", kind="audio", data=b"ID3" + b"\x00" * 10, max_bytes=1000)
    m4a = store.register(
        filename="a.m4a",
        kind="audio",
        data=(20).to_bytes(4, "big") + b"ftyp" + b"M4A " + b"\x00\x00\x00\x00",
        max_bytes=1000,
    )
    assert jpeg.media_type == "image/jpeg"
    assert webp.media_type == "image/webp"
    assert wav.media_type == "audio/wav"
    assert mp3.media_type == "audio/mpeg"
    assert m4a.media_type == "audio/mp4"


def test_invalid_register_leaves_no_file(tmp_path: Path):
    store = LocalFileStore(tmp_path)
    cases = [
        ("image", b"not-an-image", 1000),
        ("audio", _image("PNG"), 1000),
        ("image", _wav(), 1000),
        ("image", _image("PNG"), 4),
    ]
    for kind, data, limit in cases:
        with pytest.raises(VideoAgentError) as caught:
            store.register(filename="bad.bin", kind=kind, data=data, max_bytes=limit)
        assert caught.value.code == "VALIDATION_ERROR"
    assert list(tmp_path.rglob("*")) == []


def test_path_for_rejects_escape(tmp_path: Path):
    store = LocalFileStore(tmp_path)
    with pytest.raises(VideoAgentError) as caught:
        store.path_for("../outside.txt")
    assert caught.value.code == "VALIDATION_ERROR"
