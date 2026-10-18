import io
import uuid
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Literal

from PIL import Image, UnidentifiedImageError

from video_agent.errors import VideoAgentError


@dataclass(frozen=True)
class StoredAsset:
    asset_id: str
    kind: str
    filename: str
    media_type: str
    byte_size: int
    relative_path: str


class LocalFileStore:
    def __init__(self, home: Path) -> None:
        self.home = home

    def path_for(self, relative_path: str) -> Path:
        root = self.home.resolve()
        if Path(relative_path).is_absolute() or PureWindowsPath(relative_path).drive or PureWindowsPath(relative_path).root or ":" in relative_path or any(ord(char) < 32 for char in relative_path):
            raise VideoAgentError("VALIDATION_ERROR", "只接受相对文件路径")
        target = (self.home / relative_path).resolve()
        if not target.is_relative_to(root):
            raise VideoAgentError("VALIDATION_ERROR", "文件路径越界")
        return target

    def exists(self, relative_path: str) -> bool:
        return self.path_for(relative_path).is_file()

    def read_bytes(self, relative_path: str) -> bytes:
        return self.path_for(relative_path).read_bytes()

    def write_bytes(self, relative_path: str, data: bytes) -> Path:
        target = self.path_for(relative_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + ".partial")
        temporary.write_bytes(data)
        temporary.replace(target)
        return target

    def register(
        self,
        *,
        filename: str,
        kind: Literal["image", "audio"],
        data: bytes,
        max_bytes: int,
    ) -> StoredAsset:
        if kind not in ("image", "audio"):
            raise VideoAgentError("VALIDATION_ERROR", "种类只能是 image 或 audio", fields=["kind"])
        name = filename.replace("\\", "/").rsplit("/", 1)[-1]
        if name in {"", ".", ".."} or len(name.encode("utf-8")) > 255 or any(ord(char) < 32 or char in '<>:"|?*' for char in name) or name.endswith((" ", ".")) or name.split(".", 1)[0].upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}:
            raise VideoAgentError("VALIDATION_ERROR", "文件名无效", fields=["filename"])
        if len(data) < 1 or len(data) > max_bytes:
            raise VideoAgentError("VALIDATION_ERROR", "文件超过大小限制或为空", fields=["data"])
        media_type = _media_type(kind, data)
        asset_id = str(uuid.uuid4())
        relative_path = f"assets/{asset_id}/{name}"
        self.write_bytes(relative_path, data)
        return StoredAsset(
            asset_id=asset_id,
            kind=kind,
            filename=name,
            media_type=media_type,
            byte_size=len(data),
            relative_path=relative_path,
        )


def _media_type(kind: str, data: bytes) -> str:
    image_type = _image_type(data)
    audio_type = _audio_type(data)
    if kind == "image":
        if image_type is None:
            raise VideoAgentError("VALIDATION_ERROR", "图片只接受 JPEG、PNG 或 WebP", fields=["data"])
        try:
            with Image.open(io.BytesIO(data)) as image:
                image.load()
        except (UnidentifiedImageError, OSError, ValueError) as exc:
            raise VideoAgentError("VALIDATION_ERROR", "图片无法解码", fields=["data"]) from exc
        return image_type
    if audio_type is None:
        raise VideoAgentError("VALIDATION_ERROR", "音频只接受 MP3、WAV 或 M4A", fields=["data"])
    return audio_type


def _image_type(data: bytes) -> str | None:
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _audio_type(data: bytes) -> str | None:
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "audio/wav"
    if data.startswith(b"ID3") and len(data) >= 10:
        return "audio/mpeg"
    if len(data) >= 2 and data[0] == 0xFF and data[1] & 0xE0 == 0xE0:
        return "audio/mpeg"
    if len(data) >= 12 and data[4:8] == b"ftyp" and data[8:12] in {b"M4A ", b"isom", b"iso2", b"mp41", b"mp42", b"M4B "}:
        return "audio/mp4"
    return None
