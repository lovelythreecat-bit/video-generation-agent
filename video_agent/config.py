import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from video_agent.errors import VideoAgentError

ASPECT_RATIOS: tuple[str, ...] = (
    "9:16",
    "16:9",
    "1:1",
    "4:5",
    "3:4",
    "4:3",
    "3:2",
    "2:3",
)

RATIO_PAIRS: dict[str, tuple[int, int]] = {
    "9:16": (9, 16),
    "16:9": (16, 9),
    "1:1": (1, 1),
    "4:5": (4, 5),
    "3:4": (3, 4),
    "4:3": (4, 3),
    "3:2": (3, 2),
    "2:3": (2, 3),
}

ADAPTER_ROLES: tuple[str, ...] = ("planner", "video", "voice", "music", "visual")

DEFAULT_CONFIG_JSON = """{
  "mode": "fake",
  "duration_tiers": [5, 10, 15, 30],
  "default_duration": 10,
  "max_images": 12,
  "max_file_bytes": 20971520,
  "video_bitrate": 8000000,
  "audio_bitrate": 192000,
  "lease_seconds": 60,
  "resolutions": {
    "9:16": [1080, 1920],
    "16:9": [1920, 1080],
    "1:1": [1080, 1080],
    "4:5": [1080, 1350],
    "3:4": [1080, 1440],
    "4:3": [1440, 1080],
    "3:2": [1620, 1080],
    "2:3": [1080, 1620]
  },
  "adapters": {
    "planner": {"adapter": "fake"},
    "video": {"adapter": "fake"},
    "voice": {"adapter": "fake"},
    "music": {"adapter": "fake"},
    "visual": {"adapter": "none"}
  }
}
"""


class AdapterConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    adapter: str

    def field_map(self) -> dict[str, str]:
        extra = self.model_extra or {}
        return {str(key): str(value) for key, value in extra.items()}


class AdapterSetting(BaseModel):
    adapter: str
    fields: dict[str, str] = Field(default_factory=dict)


class SettingsPatch(BaseModel):
    duration_tiers: list[int] | None = None
    default_duration: int | None = None
    mode: Literal["fake", "real"] | None = None
    planner: AdapterSetting | None = None
    video: AdapterSetting | None = None
    voice: AdapterSetting | None = None
    music: AdapterSetting | None = None
    visual: AdapterSetting | None = None


class AppConfig(BaseModel):
    mode: Literal["fake", "real"]
    duration_tiers: list[int]
    default_duration: int
    max_images: int
    max_file_bytes: int
    video_bitrate: int
    audio_bitrate: int
    lease_seconds: int
    resolutions: dict[str, tuple[int, int]]
    adapters: dict[str, AdapterConfig]


def _fail(message: str) -> None:
    raise VideoAgentError("VALIDATION_ERROR", message)


def _plain_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _fail(f"{label} 必须是整数")
    return int(value)


def _is_secret(name: str) -> bool:
    lowered = name.lower()
    return "key" in lowered or "token" in lowered or "secret" in lowered


def _validate_data(data: object) -> AppConfig:
    if not isinstance(data, dict):
        _fail("配置必须是 JSON 对象")
    mode = data.get("mode")
    if mode not in ("fake", "real"):
        _fail("模式只能是 fake 或 real")
    raw_tiers = data.get("duration_tiers")
    if not isinstance(raw_tiers, list) or len(raw_tiers) == 0:
        _fail("时长档不能为空")
    tiers: list[int] = []
    for item in raw_tiers:
        number = _plain_int(item, "时长档")
        if number < 1 or number > 30:
            _fail("时长档必须是 1 到 30 的整数")
        tiers.append(number)
    if len(set(tiers)) != len(tiers):
        _fail("时长档不能重复")
    default_duration = _plain_int(data.get("default_duration"), "默认时长")
    if default_duration not in tiers:
        _fail("默认时长必须属于时长档")
    max_images = _plain_int(data.get("max_images"), "图片张数上限")
    if max_images < 1 or max_images > 12:
        _fail("图片张数上限必须在 1 到 12 之间")
    max_file_bytes = _plain_int(data.get("max_file_bytes"), "单文件上限")
    if max_file_bytes < 1 or max_file_bytes > 20971520:
        _fail("单文件上限必须在 1 到 20971520 之间")
    video_bitrate = _plain_int(data.get("video_bitrate"), "视频码率")
    audio_bitrate = _plain_int(data.get("audio_bitrate"), "音频码率")
    if video_bitrate < 1 or audio_bitrate < 1:
        _fail("码率必须是正整数")
    lease_seconds = _plain_int(data.get("lease_seconds"), "租约秒数")
    if lease_seconds < 15:
        _fail("租约不得低于 15 秒")
    raw_resolutions = data.get("resolutions")
    if not isinstance(raw_resolutions, dict) or set(raw_resolutions) != set(ASPECT_RATIOS):
        _fail("不能增删比例档")
    resolutions: dict[str, tuple[int, int]] = {}
    for ratio in ASPECT_RATIOS:
        pair = raw_resolutions[ratio]
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            _fail("分辨率必须是宽和高")
        width = _plain_int(pair[0], "宽度")
        height = _plain_int(pair[1], "高度")
        ratio_w, ratio_h = RATIO_PAIRS[ratio]
        if width <= 0 or height <= 0 or width % 2 != 0 or height % 2 != 0:
            _fail("覆盖尺寸时宽和高都必须是正偶数，并且比例不变")
        if width * ratio_h != height * ratio_w:
            _fail("覆盖尺寸时宽和高都必须是正偶数，并且比例不变")
        resolutions[ratio] = (width, height)
    raw_adapters = data.get("adapters")
    if not isinstance(raw_adapters, dict) or set(raw_adapters) != set(ADAPTER_ROLES):
        _fail("适配器必须包含规划、视频、配音、音乐和视觉检查")
    adapters: dict[str, AdapterConfig] = {}
    for role in ADAPTER_ROLES:
        entry = raw_adapters[role]
        if not isinstance(entry, dict) or not isinstance(entry.get("adapter"), str) or entry.get("adapter") == "":
            _fail("适配器名称不能为空")
        adapters[role] = AdapterConfig.model_validate(entry)
    return AppConfig(
        mode=mode,
        duration_tiers=tiers,
        default_duration=default_duration,
        max_images=max_images,
        max_file_bytes=max_file_bytes,
        video_bitrate=video_bitrate,
        audio_bitrate=audio_bitrate,
        lease_seconds=lease_seconds,
        resolutions=resolutions,
        adapters=adapters,
    )


def _adapter_body(adapter: AdapterConfig) -> dict:
    body: dict[str, object] = {"adapter": adapter.adapter}
    extra = adapter.model_extra or {}
    for key, value in extra.items():
        body[str(key)] = value
    return body


def config_to_dict(config: AppConfig) -> dict:
    return {
        "mode": config.mode,
        "duration_tiers": list(config.duration_tiers),
        "default_duration": config.default_duration,
        "max_images": config.max_images,
        "max_file_bytes": config.max_file_bytes,
        "video_bitrate": config.video_bitrate,
        "audio_bitrate": config.audio_bitrate,
        "lease_seconds": config.lease_seconds,
        "resolutions": {
            ratio: [config.resolutions[ratio][0], config.resolutions[ratio][1]]
            for ratio in ASPECT_RATIOS
        },
        "adapters": {role: _adapter_body(config.adapters[role]) for role in ADAPTER_ROLES},
    }


def snapshot_config(config: AppConfig) -> dict:
    return {"mode": config.mode, "adapters": config_to_dict(config)["adapters"]}


def _dump(config: AppConfig) -> str:
    return json.dumps(config_to_dict(config), ensure_ascii=False, indent=2) + "\n"


def load_config(home: Path) -> AppConfig:
    home.mkdir(parents=True, exist_ok=True)
    path = home / "config.json"
    if not path.exists():
        path.write_text(DEFAULT_CONFIG_JSON, encoding="utf-8")
        return _validate_data(json.loads(DEFAULT_CONFIG_JSON))
    raw = path.read_bytes()
    try:
        data = json.loads(raw.decode("utf-8"))
        return _validate_data(data)
    except VideoAgentError:
        raise
    except (UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise VideoAgentError("VALIDATION_ERROR", "配置无法读取或不符合规则") from exc


def _load_real_adapters() -> dict:
    try:
        from video_agent.factory import REAL_ADAPTERS
    except ImportError:
        return {}
    if not isinstance(REAL_ADAPTERS, dict):
        return {}
    return dict(REAL_ADAPTERS)


def _assert_real_mode(adapters: dict[str, AdapterConfig]) -> None:
    registry = _load_real_adapters()
    missing: list[str] = []
    for role in ("planner", "video"):
        name = adapters[role].adapter
        if name in {"fake", "none"} or name not in registry:
            missing.append(role)
            continue
        entry = registry.get(name)
        required: list[str] = []
        if isinstance(entry, dict):
            raw_required = entry.get("required", [])
            if isinstance(raw_required, list):
                required = [str(item) for item in raw_required]
        fields = adapters[role].field_map()
        for field in required:
            if fields.get(field, "") == "":
                missing.append(f"{role}.{field}")
    if missing:
        raise VideoAgentError(
            "VALIDATION_ERROR",
            "没有已注册的真实规划或视频适配器",
            details={"missing": missing},
        )
    for role in ("voice", "music", "visual"):
        name = adapters[role].adapter
        if name not in {"fake", "none"} and name not in registry:
            raise VideoAgentError("VALIDATION_ERROR", f"{role} 适配器未注册")


def _merge_adapter(current: AdapterConfig, patch: AdapterSetting) -> AdapterConfig:
    merged = current.field_map()
    for key, value in patch.fields.items():
        if _is_secret(key) and value == "":
            continue
        merged[key] = value
    body: dict[str, object] = {"adapter": patch.adapter}
    body.update(merged)
    return AdapterConfig.model_validate(body)


def save_settings(home: Path, patch: SettingsPatch) -> AppConfig:
    path = home / "config.json"
    current = load_config(home)
    before = path.read_bytes()
    data = config_to_dict(current)
    if patch.duration_tiers is not None:
        data["duration_tiers"] = list(patch.duration_tiers)
    if patch.default_duration is not None:
        data["default_duration"] = patch.default_duration
    if patch.mode is not None:
        data["mode"] = patch.mode
    adapters = {role: current.adapters[role] for role in ADAPTER_ROLES}
    for role in ADAPTER_ROLES:
        role_patch = getattr(patch, role)
        if role_patch is not None:
            adapters[role] = _merge_adapter(adapters[role], role_patch)
    data["adapters"] = {role: _adapter_body(adapters[role]) for role in ADAPTER_ROLES}
    try:
        updated = _validate_data(data)
        if updated.mode == "real":
            _assert_real_mode(updated.adapters)
    except VideoAgentError:
        if path.exists() and path.read_bytes() != before:
            path.write_bytes(before)
        raise
    path.write_text(_dump(updated), encoding="utf-8")
    return updated
