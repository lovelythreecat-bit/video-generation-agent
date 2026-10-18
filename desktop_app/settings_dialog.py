from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import ttk

from video_agent import VideoAgentError
from video_agent.config import load_config, save_settings, wan_segment_error
from video_agent.openai_chat import probe_text, probe_vision

from desktop_app.copyable import show_message
from desktop_app.viewmodel import (
    MODE_CHOICES,
    AdapterEdit,
    SettingsEdit,
    build_settings_patch,
    mode_from_label,
    mode_label,
    settings_form_from_mapping,
)

TEXT_HELP = (
    "协议是 OpenAI Chat Completions。地址要填到能直接接 /chat/completions 的那一层。"
    "这个模型只看你填的产品文字，不看产品图片。"
    "DeepSeek：地址 https://api.deepseek.com/v1 ，模型 deepseek-chat。"
    "通义千问：地址 https://dashscope.aliyuncs.com/compatible-mode/v1 ，模型 qwen-plus。"
    "OpenAI：地址 https://api.openai.com/v1 ，模型 gpt-4o-mini。"
    "密钥留空再保存，会继续用已经保存的密钥。"
)

VIDEO_PROVIDERS = (
    ("seedance", "火山方舟 Seedance"),
    ("wan", "阿里云百炼万相"),
)

SEEDANCE_VIDEO_HELP = (
    "地址填 https://ark.cn-beijing.volces.com/api/v3 。"
    "API Key 和模型名到方舟控制台复制。"
)

WAN_VIDEO_HELP = (
    "地址填到业务空间的 /api/v1 ，例如 https://llm-xxx.cn-beijing.maas.aliyuncs.com/api/v1 。"
    "模型名自己填，例如 wan2.2-i2v-flash 。"
    "万相 3.0 填 wan3.0-video 或 wan3.0-video-prime。"
    "单段最长秒数是这一模型一次调用的上限，填 1 到 15 的整数。"
    "更长的成片会拆成多段。"
)

VISION_HELP = (
    "和文本大模型同一套 Chat Completions，模型必须能看图。"
    "选「不检查」时不会调用，成片仍会导出，结果记为未检查。"
    "通义千问：地址 https://dashscope.aliyuncs.com/compatible-mode/v1 ，模型 qwen-vl-max。"
    "OpenAI：地址 https://api.openai.com/v1 ，模型 gpt-4o。"
)

AUDIO_HELP = (
    "只有有声和无声。有声时，真实出片向视频生成里选中的供应商要声音。"
    "供应商能出声时，成片用它的音轨。不能出声时只出画面，再补本地占位声音。"
    "替身出片用本地占位音轨。无声时成片不带这路声音。字幕仍然单独开关。"
)

FAKE_MODE_HELP = (
    "不调用下面的接口。分镜和画面用本地占位数据，成片仍用本机 FFmpeg 合成。"
    "用来确认软件本身能导出。画面是静帧，不是模型生成的。"
)

REAL_MODE_HELP = (
    "分镜调用上面的文本大模型，画面调用视频生成里选中的供应商。"
    "视觉检查按你的选择。有声时向该供应商要声音。"
    "文本大模型和视频生成的地址、密钥、模型名都要填。选万相时还要填单段最长秒数。"
)


def missing_api_gaps(
    *,
    mode: str,
    text: dict[str, str],
    video: dict[str, str],
    vision_enabled: bool,
    vision: dict[str, str],
    text_key_saved: bool,
    video_key_saved: bool,
    vision_key_saved: bool,
) -> list[str]:
    if mode != "real":
        return []
    gaps = [
        _gap("文本大模型", text, text_key_saved),
        _gap("视频生成", video, video_key_saved),
    ]
    if vision_enabled:
        gaps.append(_gap("视觉大模型", vision, vision_key_saved))
    if video.get("video_provider") == "wan":
        segment_error = wan_segment_error(video.get("max_segment_seconds", ""))
        if segment_error:
            gaps.append(segment_error)
    return [item for item in gaps if item]


def _known_video_provider(video_provider: str) -> str:
    if video_provider in {"seedance", "wan"}:
        return video_provider
    return "seedance"


def _selected_video_provider(adapter: str, fields: dict[str, str]) -> str:
    if adapter in {"seedance", "wan"}:
        return adapter
    return _known_video_provider(fields.get("video_provider", ""))


def _video_provider_label(provider: str) -> str:
    for name, label in VIDEO_PROVIDERS:
        if name == provider:
            return label
    return VIDEO_PROVIDERS[0][1]


def _video_help(provider: str) -> str:
    if provider == "wan":
        return WAN_VIDEO_HELP
    return SEEDANCE_VIDEO_HELP


def compose_settings_edit(
    *,
    duration_tiers: list[int],
    default_duration: int,
    mode: str,
    text: dict[str, str],
    video: dict[str, str],
    vision_enabled: bool,
    vision: dict[str, str],
    sound_on: bool,
    video_provider: str = "seedance",
    max_segment_seconds: str = "",
) -> SettingsEdit:
    provider = _known_video_provider(video_provider)
    if mode == "real":
        planner_name = "openai_chat"
        video_name = provider
    else:
        planner_name = "fake"
        video_name = "fake"
    video_fields = dict(video)
    video_fields["video_provider"] = provider
    if provider == "wan":
        video_fields["max_segment_seconds"] = max_segment_seconds
    visual_name = "openai_vision" if vision_enabled else "none"
    return SettingsEdit(
        duration_tiers=list(duration_tiers),
        default_duration=default_duration,
        mode=mode,
        planner=AdapterEdit(adapter=planner_name, fields=dict(text)),
        video=AdapterEdit(adapter=video_name, fields=video_fields),
        voice=AdapterEdit(adapter="fake" if sound_on else "none", fields={}),
        music=AdapterEdit(adapter="fake" if sound_on else "none", fields={}),
        visual=AdapterEdit(adapter=visual_name, fields=dict(vision)),
    )


def _gap(title: str, fields: dict[str, str], key_saved: bool) -> str:
    labels = []
    if fields.get("base_url", "").strip() == "":
        labels.append("接口根地址")
    if fields.get("api_key", "").strip() == "" and not key_saved:
        labels.append("API Key")
    if fields.get("model", "").strip() == "":
        labels.append("模型名")
    if not labels:
        return ""
    return title + "还没填 " + "、".join(labels)


def _settings_error_text(code: str, message: str, stage: str | None) -> str:
    lines = [message, f"错误码：{code}"]
    if stage:
        lines.append(f"阶段：{stage}")
    return "\n".join(lines)


class SettingsBackend:
    """Database-backed desktop form with optimistic concurrency.

    The explicit no-agent path retains isolated legacy form/unit tests only.
    """
    def __init__(self, home: Path, *, agent=None) -> None:
        self.home = home
        self.service = getattr(agent, "settings_service", None)
        self.version = None

    def read(self):
        if self.service is None:
            return load_config(self.home)
        snapshot = self.service.snapshot()
        self.version = snapshot["configuration_revision"]
        return self.service.codec.decode(snapshot)

    def read_current(self):
        if self.service is None:
            return load_config(self.home)
        return self.service.read_effective_config()

    def save(self, patch):
        if self.service is None:
            return save_settings(self.home, patch)
        if self.version is None:
            self.read()
        saved = self.service.save_settings(patch, expected_version=self.version)
        self.version += 1
        return saved


class SettingsDialog(tk.Toplevel):
    def __init__(self, parent, home: Path, on_saved, *, agent=None) -> None:
        super().__init__(parent)
        self.home = home
        self.backend = SettingsBackend(home, agent=agent)
        self.on_saved = on_saved
        self.title("设置")
        self.minsize(640, 520)
        self.geometry("760x820")
        self.transient(parent)
        self.grab_set()
        config = self.backend.read()
        edit = settings_form_from_mapping(config)
        self._tiers = list(edit.duration_tiers)
        self._hints: list[ttk.Label] = []
        self._text_key_saved = _saved_key(config, "planner")
        self._video_key_saved = _saved_key(config, "video")
        self._vision_key_saved = _saved_key(config, "visual")
        self.mode_help = {"替身": FAKE_MODE_HELP, "真实": REAL_MODE_HELP}
        self._build(edit)
        self._refresh_tiers()
        self._show_mode_help()

    def _build(self, edit: SettingsEdit) -> None:
        actions = ttk.Frame(self, padding=(16, 8, 16, 12))
        actions.pack(side="bottom", fill="x")
        ttk.Button(actions, text="取消", command=self.destroy).pack(side="right")
        ttk.Button(actions, text="保存设置", command=self._save).pack(side="right", padx=(0, 8))

        outer = ttk.Frame(self)
        outer.pack(fill="both", expand=True)
        canvas = tk.Canvas(outer, highlightthickness=0)
        scroll = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scroll.set)
        canvas.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        frame = ttk.Frame(canvas, padding=16)
        window_id = canvas.create_window((0, 0), window=frame, anchor="nw")
        frame.bind("<Configure>", lambda _event: canvas.configure(scrollregion=canvas.bbox("all")))

        def fit_width(event) -> None:
            canvas.itemconfigure(window_id, width=event.width)
            wrap = max(event.width - 48, 280)
            for hint in self._hints:
                hint.configure(wraplength=wrap)

        canvas.bind("<Configure>", fit_width)
        self.bind("<MouseWheel>", lambda event: canvas.yview_scroll(int(-event.delta / 120), "units"))

        self._hint(
            frame,
            "不填接口时，出片方式用「替身」：不调用大模型，本机 FFmpeg 导出一条测试片，画面是静帧。"
            "要真实出片，出片方式改成「真实」，并填写文本大模型和视频生成。视觉检查可以不填。",
        )

        mode = ttk.LabelFrame(frame, text="出片方式", padding=12)
        mode.pack(fill="x", pady=(12, 0))
        mode_row = ttk.Frame(mode)
        mode_row.pack(fill="x")
        ttk.Label(mode_row, text="用哪种方式").pack(side="left")
        self.mode_var = tk.StringVar(value=mode_label(edit.mode).removeprefix("模式："))
        ttk.Combobox(
            mode_row,
            textvariable=self.mode_var,
            values=[text for _mode, text in MODE_CHOICES],
            state="readonly",
            width=12,
        ).pack(side="left", padx=8)
        self.mode_var.trace_add("write", lambda *_args: self._show_mode_help())
        self.mode_help_label = self._hint(mode, "")

        self.text_base, self.text_key, self.text_model = self._api_block(
            frame,
            "文本大模型（写分镜）",
            TEXT_HELP,
            edit.planner.fields,
            self._text_key_saved,
            self._test_text,
            "测试文本接口",
        )
        self.video_base, self.video_key, self.video_model = self._video_block(frame, edit)
        vision = ttk.LabelFrame(frame, text="视觉大模型（检查成片里的产品像不像）", padding=12)
        vision.pack(fill="x", pady=(12, 0))
        self._hint(vision, VISION_HELP)
        self.vision_var = tk.StringVar(value="调用视觉大模型" if edit.visual.adapter == "openai_vision" else "不检查")
        self._choice(vision, "检查方式", self.vision_var, ["不检查", "调用视觉大模型"])
        self.vision_base, self.vision_key, self.vision_model = self._api_fields(
            vision, edit.visual.fields, self._vision_key_saved
        )
        ttk.Button(vision, text="测试视觉接口", command=self._test_vision).pack(anchor="w", pady=(8, 0))

        audio = ttk.LabelFrame(frame, text="声音", padding=12)
        audio.pack(fill="x", pady=(12, 0))
        self._hint(audio, AUDIO_HELP)
        self.sound_var = tk.StringVar(value="无声" if edit.voice.adapter == "none" and edit.music.adapter == "none" else "有声")
        self._choice(audio, "成片声音", self.sound_var, ["有声", "无声"])

        duration = ttk.LabelFrame(frame, text="成片时长", padding=12)
        duration.pack(fill="x", pady=(12, 0))
        self._hint(duration, "新建任务时只能选这些秒数。每一档是 1 到 30 的整数，不能重复。默认秒数必须是其中一档。保存后只影响之后新建的任务。")
        self.tier_list = tk.Listbox(duration, height=4, exportselection=False)
        self.tier_list.pack(fill="x", pady=(6, 0))
        add_row = ttk.Frame(duration)
        add_row.pack(fill="x", pady=(8, 0))
        ttk.Label(add_row, text="要添加的秒数").pack(side="left")
        self.new_tier_var = tk.StringVar()
        add_entry = ttk.Entry(add_row, textvariable=self.new_tier_var, width=8)
        add_entry.pack(side="left", padx=8)
        add_entry.bind("<Return>", lambda _event: self._add_tier())
        ttk.Button(add_row, text="添加", command=self._add_tier).pack(side="left")
        ttk.Button(add_row, text="去掉选中的秒数", command=self._remove_tier).pack(side="left", padx=(8, 0))
        default_row = ttk.Frame(duration)
        default_row.pack(fill="x", pady=(8, 0))
        ttk.Label(default_row, text="新建任务默认选中").pack(side="left")
        self.default_var = tk.StringVar(value=self._tier_label(edit.default_duration))
        self.default_box = ttk.Combobox(default_row, textvariable=self.default_var, state="readonly", width=12)
        self.default_box.pack(side="left", padx=8)

    def _api_block(self, parent, title, help_text, fields, key_saved, command, button_text):
        block = ttk.LabelFrame(parent, text=title, padding=12)
        block.pack(fill="x", pady=(12, 0))
        self._hint(block, help_text)
        entries = self._api_fields(block, fields, key_saved)
        if command is not None:
            ttk.Button(block, text=button_text, command=command).pack(anchor="w", pady=(8, 0))
        return entries

    def _video_block(self, parent, edit: SettingsEdit):
        block = ttk.LabelFrame(parent, text="视频生成（按分镜出画面）", padding=12)
        block.pack(fill="x", pady=(12, 0))
        provider = _selected_video_provider(edit.video.adapter, edit.video.fields)
        self.video_help_label = self._hint(block, _video_help(provider))
        self.video_provider_var = tk.StringVar(value=_video_provider_label(provider))
        self.video_provider_var.trace_add("write", lambda *_args: self._sync_video_provider())
        self._choice(
            block,
            "供应商",
            self.video_provider_var,
            [label for _name, label in VIDEO_PROVIDERS],
            box_width=22,
        )
        entries = self._api_fields(block, edit.video.fields, self._video_key_saved)
        self.segment_var = tk.StringVar(value=edit.video.fields.get("max_segment_seconds", ""))
        self._segment_row = ttk.Frame(block)
        ttk.Label(self._segment_row, text="单段最长秒数", width=14).pack(side="left")
        ttk.Entry(self._segment_row, textvariable=self.segment_var).pack(side="left", fill="x", expand=True)
        self._segment_shown = False
        self._sync_video_provider()
        return entries

    def _sync_video_provider(self) -> None:
        provider = self._video_provider_value()
        self.video_help_label.configure(text=_video_help(provider))
        show_segment = provider == "wan"
        if show_segment and not self._segment_shown:
            self._segment_row.pack(fill="x", pady=(6, 0))
        elif not show_segment and self._segment_shown:
            self._segment_row.pack_forget()
        self._segment_shown = show_segment

    def _video_provider_value(self) -> str:
        for provider, label in VIDEO_PROVIDERS:
            if self.video_provider_var.get() == label:
                return provider
        return "seedance"

    def _api_fields(self, parent, fields: dict[str, str], key_saved: bool):
        base = self._entry(parent, "接口根地址", fields.get("base_url", ""))
        key = self._entry(parent, "API Key", "", secret=True)
        if key_saved:
            self._hint(parent, "API Key 已保存。这一栏留空再保存，会继续用原来的密钥。")
        model = self._entry(parent, "模型名", fields.get("model", ""))
        return base, key, model

    def _entry(self, parent, label: str, value: str, secret: bool = False) -> tk.StringVar:
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=(6, 0))
        ttk.Label(row, text=label, width=12).pack(side="left")
        variable = tk.StringVar(value=value)
        ttk.Entry(row, textvariable=variable, show="*" if secret else "").pack(side="left", fill="x", expand=True)
        return variable

    def _choice(self, parent, label: str, variable: tk.StringVar, values: list[str], box_width: int = 18) -> None:
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=(6, 0))
        ttk.Label(row, text=label, width=12).pack(side="left")
        ttk.Combobox(row, textvariable=variable, values=values, state="readonly", width=box_width).pack(side="left")

    def _hint(self, parent, text: str) -> ttk.Label:
        label = ttk.Label(parent, text=text, justify="left", wraplength=640)
        label.pack(anchor="w", pady=(4, 0))
        self._hints.append(label)
        return label

    def _tier_label(self, number: int) -> str:
        return f"{number} 秒"

    def _refresh_tiers(self) -> None:
        self.tier_list.delete(0, "end")
        labels = [self._tier_label(number) for number in self._tiers]
        for label in labels:
            self.tier_list.insert("end", label)
        self.default_box.configure(values=labels)
        if self.default_var.get() not in labels and labels:
            self.default_var.set(labels[0])

    def _add_tier(self) -> None:
        raw = self.new_tier_var.get().strip()
        try:
            number = int(raw)
        except ValueError:
            show_message(self, "秒数没加上", "要写成整数，例如 10。")
            return
        if number < 1 or number > 30:
            show_message(self, "秒数没加上", "每一档只能是 1 到 30 秒。")
            return
        if number in self._tiers:
            show_message(self, "秒数没加上", f"{number} 秒已经在列表里。")
            return
        self._tiers.append(number)
        self.new_tier_var.set("")
        self._refresh_tiers()

    def _remove_tier(self) -> None:
        selection = self.tier_list.curselection()
        if not selection:
            show_message(self, "还没选中", "先在列表里点一下要去掉的秒数。")
            return
        if len(self._tiers) == 1:
            show_message(self, "秒数没去掉", "至少保留一个秒数，新建任务时要从这里选时长。")
            return
        del self._tiers[selection[0]]
        self._refresh_tiers()

    def _show_mode_help(self) -> None:
        self.mode_help_label.configure(text=self.mode_help.get(self.mode_var.get(), ""))

    def _values(self, base: tk.StringVar, key: tk.StringVar, model: tk.StringVar) -> dict[str, str]:
        return {"base_url": base.get().strip(), "api_key": key.get().strip(), "model": model.get().strip()}

    def _collect(self) -> SettingsEdit:
        if not self._tiers:
            raise ValueError("至少保留一个秒数。")
        default_text = self.default_var.get().removesuffix(" 秒").strip()
        try:
            default_duration = int(default_text)
        except ValueError as exc:
            raise ValueError("默认秒数要从上面的列表里选。") from exc
        if default_duration not in self._tiers:
            raise ValueError("默认秒数必须是可选秒数里的一档。")
        try:
            mode = mode_from_label(self.mode_var.get())
        except ValueError as exc:
            raise ValueError("出片方式要从列表里选。") from exc
        return compose_settings_edit(
            duration_tiers=list(self._tiers),
            default_duration=default_duration,
            mode=mode,
            text=self._values(self.text_base, self.text_key, self.text_model),
            video=self._values(self.video_base, self.video_key, self.video_model),
            vision_enabled=self.vision_var.get() == "调用视觉大模型",
            vision=self._values(self.vision_base, self.vision_key, self.vision_model),
            sound_on=self.sound_var.get() != "无声",
            video_provider=self._video_provider_value(),
            max_segment_seconds=self.segment_var.get().strip(),
        )

    def _save(self) -> None:
        try:
            edit = self._collect()
        except ValueError as exc:
            show_message(self, "设置未保存", str(exc))
            return
        gaps = missing_api_gaps(
            mode=edit.mode,
            text=edit.planner.fields,
            video=edit.video.fields,
            vision_enabled=edit.visual.adapter == "openai_vision",
            vision=edit.visual.fields,
            text_key_saved=self._text_key_saved,
            video_key_saved=self._video_key_saved,
            vision_key_saved=self._vision_key_saved,
        )
        if gaps:
            show_message(self, "设置未保存", "\n".join(gaps))
            return
        try:
            saved = self.backend.save(build_settings_patch(edit))
        except VideoAgentError as exc:
            show_message(self, "设置未保存", _settings_error_text(exc.code, exc.message, exc.stage))
            return
        self.on_saved(saved.mode)
        self.destroy()

    def _test_text(self) -> None:
        self._probe("文本接口", self._values(self.text_base, self.text_key, self.text_model), self._text_key_saved, probe_text)

    def _test_vision(self) -> None:
        self._probe(
            "视觉接口",
            self._values(self.vision_base, self.vision_key, self.vision_model),
            self._vision_key_saved,
            probe_vision,
        )

    def _probe(self, title: str, fields: dict[str, str], key_saved: bool, caller) -> None:
        key = fields["api_key"] or (self._saved_secret(title) if key_saved else "")
        if fields["base_url"] == "" or fields["model"] == "" or key == "":
            show_message(self, title + "还没测", f"{title}要先填接口根地址、API Key 和模型名。")
            return
        try:
            self.configure(cursor="watch")
            self.update_idletasks()
            reply = caller(base_url=fields["base_url"], api_key=key, model=fields["model"])
        except VideoAgentError as exc:
            show_message(self, title + "没有调通", exc.message)
            return
        except Exception as exc:
            show_message(self, title + "没有调通", str(exc))
            return
        finally:
            self.configure(cursor="")
        show_message(self, title + "有响应", reply[:300])

    def _saved_secret(self, title: str) -> str:
        config = self.backend.read_current()
        role = "visual" if title.startswith("视觉") else "planner"
        return _raw_key(config, role)


def _saved_key(config, role: str) -> bool:
    return _raw_key(config, role) != ""


def _raw_key(config, role: str) -> str:
    adapters = getattr(config, "adapters")
    item = adapters[role] if isinstance(adapters, dict) else getattr(adapters, role)
    fields = item.field_map() if hasattr(item, "field_map") else {}
    return str(fields.get("api_key", "")).strip()


def open_settings(parent, home: Path, on_saved, *, agent=None) -> None:
    SettingsDialog(parent, home, on_saved, agent=agent)
