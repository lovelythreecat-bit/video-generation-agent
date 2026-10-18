from desktop_app.settings_dialog import compose_settings_edit, missing_api_gaps


def test_desktop_settings_use_database_and_reject_stale_form(tmp_path, monkeypatch):
    """A desktop form must not read/write JSON or overwrite a newer API edit."""
    import json
    from types import SimpleNamespace

    import pytest
    from cryptography.fernet import Fernet

    from video_agent.config import AdapterSetting, SettingsPatch
    from video_agent.db import Database
    from video_agent.errors import VideoAgentError
    from video_agent.settings_service import SettingsService
    from desktop_app.settings_dialog import SettingsBackend

    home = tmp_path / "isolated-desktop"
    database = Database(home)
    service = SettingsService(database, Fernet.generate_key())
    service.save_settings(SettingsPatch(planner=AdapterSetting(adapter="openai_chat", fields={
        "base_url": "https://example.invalid/v1", "model": "saved", "api_key": "stored-secret",
    })), expected_version=1)
    poison = home / "config.json"
    poison.write_text('{"invalid":"must never read this"}', encoding="utf-8")
    before = poison.read_bytes()
    backend = SettingsBackend(home, agent=SimpleNamespace(settings_service=service))
    assert backend.read().adapters["planner"].field_map()["api_key"] == "stored-secret"
    assert backend.read_current().mode == "fake"
    service.save_settings(SettingsPatch(default_duration=5), expected_version=2)
    with pytest.raises(VideoAgentError) as exc:
        backend.save(SettingsPatch(default_duration=15))
    assert exc.value.code == "CONFLICT"
    assert service.read_settings().default_duration == 5
    backend.read()
    backend.save(SettingsPatch(planner=AdapterSetting(adapter="openai_chat", fields={"api_key": "", "model": "new"})))
    assert service.read_effective_config().adapters["planner"].field_map()["api_key"] == "stored-secret"
    assert poison.read_bytes() == before
    with database.transaction(write=False) as conn:
        assert "stored-secret" not in json.dumps(conn.execute("SELECT * FROM model_profiles").fetchall())
    database.close()


def test_desktop_embedded_worker_defers_to_singleton_runtime(monkeypatch):
    import threading
    from types import SimpleNamespace

    from desktop_app import worker
    from video_agent import worker_main

    called = threading.Event()
    agent = SimpleNamespace(settings_service=object())
    stop = threading.Event()

    def leased_runtime(actual_agent, actual_stop):
        assert actual_agent is agent
        assert actual_stop is stop
        called.set()

    monkeypatch.setattr(worker_main, "run_worker", leased_runtime)
    thread = worker.start_worker(agent, stop)
    thread.join(timeout=2)
    assert called.is_set()
    assert thread.is_alive() is False


def _fields(base="https://example.invalid/v1", key="sk", model="demo"):
    return {"base_url": base, "api_key": key, "model": model}


def test_real_settings_name_the_three_apis():
    edit = compose_settings_edit(
        duration_tiers=[10],
        default_duration=10,
        mode="real",
        text=_fields(model="deepseek-chat"),
        video=_fields(base="https://ark.cn-beijing.volces.com/api/v3", model="doubao-seedance"),
        vision_enabled=True,
        vision=_fields(model="qwen-vl-max"),
        sound_on=True,
    )
    assert edit.planner.adapter == "openai_chat"
    assert edit.video.adapter == "seedance"
    assert edit.video.fields["video_provider"] == "seedance"
    assert "max_segment_seconds" not in edit.video.fields
    assert edit.visual.adapter == "openai_vision"
    assert edit.voice.adapter == "fake"
    assert edit.music.adapter == "fake"
    assert missing_api_gaps(
        mode="real",
        text=edit.planner.fields,
        video=edit.video.fields,
        vision_enabled=True,
        vision=edit.visual.fields,
        text_key_saved=False,
        video_key_saved=False,
        vision_key_saved=False,
    ) == []


def test_real_settings_list_blank_text_fields_and_keep_saved_key():
    gaps = missing_api_gaps(
        mode="real",
        text={"base_url": "", "api_key": "", "model": ""},
        video=_fields(),
        vision_enabled=False,
        vision={"base_url": "", "api_key": "", "model": ""},
        text_key_saved=True,
        video_key_saved=False,
        vision_key_saved=False,
    )
    assert gaps == ["文本大模型还没填 接口根地址、模型名"]


def test_fake_mode_does_not_require_api_fields():
    edit = compose_settings_edit(
        duration_tiers=[10],
        default_duration=10,
        mode="fake",
        text={"base_url": "", "api_key": "", "model": ""},
        video={"base_url": "", "api_key": "", "model": ""},
        vision_enabled=False,
        vision={"base_url": "", "api_key": "", "model": ""},
        sound_on=False,
    )
    assert edit.mode == "fake"
    assert edit.planner.adapter == "fake"
    assert edit.video.adapter == "fake"
    assert edit.voice.adapter == "none"
    assert edit.music.adapter == "none"
    assert missing_api_gaps(
        mode=edit.mode,
        text=edit.planner.fields,
        video=edit.video.fields,
        vision_enabled=False,
        vision=edit.visual.fields,
        text_key_saved=False,
        video_key_saved=False,
        vision_key_saved=False,
    ) == []


def test_real_wan_writes_provider_and_segment_limit():
    edit = compose_settings_edit(
        duration_tiers=[10],
        default_duration=10,
        mode="real",
        text=_fields(model="deepseek-chat"),
        video=_fields(base="https://llm-xxx.cn-beijing.maas.aliyuncs.com/api/v1", model="wan2.2-i2v-flash"),
        vision_enabled=False,
        vision={"base_url": "", "api_key": "", "model": ""},
        sound_on=False,
        video_provider="wan",
        max_segment_seconds="5",
    )
    assert edit.video.adapter == "wan"
    assert edit.video.fields["video_provider"] == "wan"
    assert edit.video.fields["max_segment_seconds"] == "5"


def test_real_wan_segment_limit_must_be_1_to_15():
    empty = compose_settings_edit(
        duration_tiers=[10],
        default_duration=10,
        mode="real",
        text=_fields(model="deepseek-chat"),
        video=_fields(),
        vision_enabled=False,
        vision={"base_url": "", "api_key": "", "model": ""},
        sound_on=False,
        video_provider="wan",
        max_segment_seconds="",
    )
    empty_gaps = missing_api_gaps(
        mode="real",
        text=empty.planner.fields,
        video=empty.video.fields,
        vision_enabled=False,
        vision=empty.visual.fields,
        text_key_saved=False,
        video_key_saved=False,
        vision_key_saved=False,
    )
    assert any("还没填 单段最长秒数" in item for item in empty_gaps)

    too_long = compose_settings_edit(
        duration_tiers=[10],
        default_duration=10,
        mode="real",
        text=_fields(model="deepseek-chat"),
        video=_fields(),
        vision_enabled=False,
        vision={"base_url": "", "api_key": "", "model": ""},
        sound_on=False,
        video_provider="wan",
        max_segment_seconds="16",
    )
    long_gaps = missing_api_gaps(
        mode="real",
        text=too_long.planner.fields,
        video=too_long.video.fields,
        vision_enabled=False,
        vision=too_long.visual.fields,
        text_key_saved=False,
        video_key_saved=False,
        vision_key_saved=False,
    )
    assert any("要填 1 到 15 的整数" in item for item in long_gaps)


def test_fake_mode_stores_provider_without_requiring_api_fields():
    edit = compose_settings_edit(
        duration_tiers=[10],
        default_duration=10,
        mode="fake",
        text={"base_url": "", "api_key": "", "model": ""},
        video={"base_url": "", "api_key": "", "model": ""},
        vision_enabled=False,
        vision={"base_url": "", "api_key": "", "model": ""},
        sound_on=False,
        video_provider="wan",
        max_segment_seconds="",
    )
    assert edit.video.adapter == "fake"
    assert edit.video.fields["video_provider"] == "wan"
    assert missing_api_gaps(
        mode=edit.mode,
        text=edit.planner.fields,
        video=edit.video.fields,
        vision_enabled=False,
        vision=edit.visual.fields,
        text_key_saved=False,
        video_key_saved=False,
        vision_key_saved=False,
    ) == []
