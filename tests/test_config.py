import json
from pathlib import Path

import pytest

from video_agent.config import load_config, save_settings
from video_agent.config import AdapterSetting, SettingsPatch
from video_agent.errors import VideoAgentError


def _write(home: Path, payload: dict) -> Path:
    home.mkdir(parents=True, exist_ok=True)
    path = home / "config.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _valid() -> dict:
    return {
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
            "2:3": [1080, 1620],
        },
        "adapters": {
            "planner": {"adapter": "fake"},
            "video": {"adapter": "fake"},
            "voice": {"adapter": "fake"},
            "music": {"adapter": "fake"},
            "visual": {"adapter": "none"},
        },
    }


def test_missing_file_writes_default_config(tmp_path: Path):
    config = load_config(tmp_path)
    text = (tmp_path / "config.json").read_text(encoding="utf-8")
    saved = json.loads(text)
    assert list(saved) == [
        "mode",
        "duration_tiers",
        "default_duration",
        "max_images",
        "max_file_bytes",
        "video_bitrate",
        "audio_bitrate",
        "lease_seconds",
        "resolutions",
        "adapters",
    ]
    assert config.mode == "fake"
    assert config.duration_tiers == [5, 10, 15, 30]
    assert config.default_duration == 10
    assert config.max_images == 12
    assert config.max_file_bytes == 20971520
    assert config.video_bitrate == 8000000
    assert config.audio_bitrate == 192000
    assert config.lease_seconds == 60
    assert config.resolutions["9:16"] == (1080, 1920)
    assert config.adapters["visual"].adapter == "none"
    assert saved["adapters"]["planner"] == {"adapter": "fake"}


def test_invalid_config_is_not_rewritten(tmp_path: Path):
    cases = [
        {**_valid(), "duration_tiers": [5, 10, 15, 31]},
        {**_valid(), "duration_tiers": [10, 10]},
        {**_valid(), "duration_tiers": []},
        {**_valid(), "default_duration": 20},
        {**_valid(), "max_images": 13},
        {**_valid(), "max_images": 0},
        {**_valid(), "max_file_bytes": 20971521},
        {**_valid(), "lease_seconds": 14},
        {**_valid(), "resolutions": {**_valid()["resolutions"], "9:16": [1081, 1920]}},
        {**_valid(), "resolutions": {**_valid()["resolutions"], "9:16": [1082, 1920]}},
        {**_valid(), "resolutions": {key: value for key, value in _valid()["resolutions"].items() if key != "1:1"}},
        {**_valid(), "resolutions": {**_valid()["resolutions"], "21:9": [1080, 1920]}},
    ]
    for index, payload in enumerate(cases):
        home = tmp_path / str(index)
        path = _write(home, payload)
        before = path.read_bytes()
        with pytest.raises(VideoAgentError) as caught:
            load_config(home)
        assert caught.value.code == "VALIDATION_ERROR"
        assert path.read_bytes() == before


def test_equal_limits_and_even_same_ratio_are_valid(tmp_path: Path):
    payload = _valid()
    payload["max_images"] = 12
    payload["max_file_bytes"] = 20971520
    payload["resolutions"]["9:16"] = [540, 960]
    _write(tmp_path, payload)
    config = load_config(tmp_path)
    assert config.max_images == 12
    assert config.max_file_bytes == 20971520
    assert config.resolutions["9:16"] == (540, 960)


def test_save_merges_adapter_fields_and_keeps_blank_secrets(tmp_path: Path):
    payload = _valid()
    payload["adapters"]["planner"] = {
        "adapter": "fake",
        "base_url": "http://old",
        "api_key": "secret",
    }
    _write(tmp_path, payload)
    saved = save_settings(
        tmp_path,
        SettingsPatch(
            planner=AdapterSetting(
                adapter="fake",
                fields={"base_url": "http://new", "api_key": "", "API_TOKEN": ""},
            )
        ),
    )
    stored = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert saved.adapters["planner"].adapter == "fake"
    assert stored["adapters"]["planner"]["base_url"] == "http://new"
    assert stored["adapters"]["planner"]["api_key"] == "secret"
    assert "API_TOKEN" not in stored["adapters"]["planner"]
    assert stored["video_bitrate"] == 8000000


def test_real_mode_fails_without_registered_adapters_and_does_not_write(tmp_path: Path):
    _write(tmp_path, _valid())
    before = (tmp_path / "config.json").read_bytes()
    with pytest.raises(VideoAgentError) as caught:
        save_settings(tmp_path, SettingsPatch(mode="real"))
    assert caught.value.code == "VALIDATION_ERROR"
    assert "没有已注册的真实规划或视频适配器" in caught.value.message
    assert (tmp_path / "config.json").read_bytes() == before


def test_illegal_settings_patch_does_not_write(tmp_path: Path):
    _write(tmp_path, _valid())
    before = (tmp_path / "config.json").read_bytes()
    with pytest.raises(VideoAgentError) as caught:
        save_settings(tmp_path, SettingsPatch(duration_tiers=[5, 10], default_duration=15))
    assert caught.value.code == "VALIDATION_ERROR"
    assert (tmp_path / "config.json").read_bytes() == before


def test_real_mode_saves_text_and_video_endpoints(tmp_path: Path):
    _write(tmp_path, _valid())
    saved = save_settings(
        tmp_path,
        SettingsPatch(
            mode="real",
            planner=AdapterSetting(
                adapter="openai_chat",
                fields={
                    "base_url": "https://api.deepseek.com/v1",
                    "api_key": "sk-test",
                    "model": "deepseek-chat",
                },
            ),
            video=AdapterSetting(
                adapter="seedance",
                fields={
                    "base_url": "https://ark.cn-beijing.volces.com/api/v3",
                    "api_key": "ark-test",
                    "model": "doubao-seedance",
                },
            ),
        ),
    )
    assert saved.mode == "real"
    assert saved.adapters["planner"].adapter == "openai_chat"
    assert saved.adapters["planner"].field_map()["model"] == "deepseek-chat"
    assert saved.adapters["video"].field_map()["api_key"] == "ark-test"


def test_real_mode_names_the_missing_text_key(tmp_path: Path):
    _write(tmp_path, _valid())
    before = (tmp_path / "config.json").read_bytes()
    with pytest.raises(VideoAgentError) as caught:
        save_settings(
            tmp_path,
            SettingsPatch(
                mode="real",
                planner=AdapterSetting(
                    adapter="openai_chat",
                    fields={"base_url": "https://api.deepseek.com/v1", "api_key": "", "model": "deepseek-chat"},
                ),
                video=AdapterSetting(
                    adapter="seedance",
                    fields={
                        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
                        "api_key": "ark-test",
                        "model": "doubao-seedance",
                    },
                ),
            ),
        )
    assert caught.value.code == "VALIDATION_ERROR"
    assert "文本大模型还没填 API Key" in caught.value.message
    assert (tmp_path / "config.json").read_bytes() == before


def test_wan_segment_rejects_out_of_range(tmp_path: Path):
    _write(tmp_path, _valid())
    before = (tmp_path / "config.json").read_bytes()
    planner = AdapterSetting(
        adapter="openai_chat",
        fields={
            "base_url": "https://api.deepseek.com/v1",
            "api_key": "sk-test",
            "model": "deepseek-chat",
        },
    )
    for raw in ("0", "1.5"):
        with pytest.raises(VideoAgentError) as caught:
            save_settings(
                tmp_path,
                SettingsPatch(
                    mode="real",
                    planner=planner,
                    video=AdapterSetting(
                        adapter="wan",
                        fields={
                            "base_url": "https://llm.example.invalid/api/v1",
                            "api_key": "sk-wan",
                            "model": "wan2.2-i2v-flash",
                            "max_segment_seconds": raw,
                        },
                    ),
                ),
            )
        assert caught.value.code == "VALIDATION_ERROR"
        assert "要填 1 到 15 的整数" in caught.value.message
        assert (tmp_path / "config.json").read_bytes() == before


def test_unreadable_json_is_not_rewritten(tmp_path: Path):
    path = tmp_path / "config.json"
    path.write_text("{", encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(VideoAgentError) as caught:
        load_config(tmp_path)
    assert caught.value.code == "VALIDATION_ERROR"
    assert path.read_bytes() == before


def test_public_settings_reject_boolean_numbers_and_repr_hides_nested_credentials():
    from pydantic import ValidationError
    from video_agent.config import validate_config
    with pytest.raises(ValidationError):
        SettingsPatch(max_images=True)
    payload = _valid()
    payload['adapters']['planner']['headers'] = {'Authorization': 'nested-credential'}
    config = validate_config(payload)
    assert 'nested-credential' not in repr(config)
    assert 'nested-credential' not in str(config)
