from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

from video_agent import VideoAgentError
from video_agent.config import load_config, save_settings

from desktop_app.viewmodel import (
    MODE_CHOICES,
    SETTINGS_ROLES,
    AdapterEdit,
    SettingsEdit,
    build_settings_patch,
    error_dialog_text,
    fields_to_text,
    format_duration_tiers,
    mode_from_label,
    mode_label,
    parse_duration_tiers,
    parse_field_lines,
    settings_form_from_mapping,
)


class SettingsDialog(tk.Toplevel):
    def __init__(self, parent, home: Path, on_saved) -> None:
        super().__init__(parent)
        self.home = home
        self.on_saved = on_saved
        self.title("设置")
        self.transient(parent)
        self.grab_set()
        config = load_config(home)
        edit = settings_form_from_mapping(config)
        self.tiers_var = tk.StringVar(value=format_duration_tiers(edit.duration_tiers))
        self.default_var = tk.StringVar(value=str(edit.default_duration))
        self.mode_var = tk.StringVar(value=mode_label(edit.mode).removeprefix("模式："))
        self.adapter_vars = {}
        self.field_texts = {}
        frame = ttk.Frame(self, padding=12)
        frame.pack(fill="both", expand=True)
        self._row(frame, "时长档位", self.tiers_var, 0)
        self._row(frame, "默认时长", self.default_var, 1)
        ttk.Label(frame, text="模式").grid(row=2, column=0, sticky="w")
        ttk.Combobox(frame, textvariable=self.mode_var, values=[text for _mode, text in MODE_CHOICES], state="readonly").grid(row=2, column=1, sticky="ew")
        role_edits = {
            "planner": edit.planner,
            "video": edit.video,
            "voice": edit.voice,
            "music": edit.music,
            "visual": edit.visual,
        }
        row_index = 3
        for role, title in SETTINGS_ROLES:
            item = role_edits[role]
            ttk.Label(frame, text=title).grid(row=row_index, column=0, sticky="nw", pady=4)
            box = ttk.Frame(frame)
            box.grid(row=row_index, column=1, sticky="ew", pady=4)
            variable = tk.StringVar(value=item.adapter)
            ttk.Entry(box, textvariable=variable).pack(fill="x")
            text = tk.Text(box, height=4, width=48)
            text.pack(fill="x")
            text.insert("1.0", fields_to_text(item.fields))
            self.adapter_vars[role] = variable
            self.field_texts[role] = text
            row_index += 1
        ttk.Label(frame, text="留空的密钥保持原值。此处不编辑码率、分辨率、图片张数和单文件上限。").grid(row=row_index, column=0, columnspan=2, sticky="w")
        actions = ttk.Frame(frame)
        actions.grid(row=row_index + 1, column=0, columnspan=2, sticky="e", pady=8)
        ttk.Button(actions, text="保存", command=self._save).pack(side="left", padx=4)
        ttk.Button(actions, text="取消", command=self.destroy).pack(side="left")
        frame.columnconfigure(1, weight=1)

    def _row(self, frame, label: str, variable: tk.StringVar, row: int) -> None:
        ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", pady=2)
        ttk.Entry(frame, textvariable=variable).grid(row=row, column=1, sticky="ew", pady=2)

    def _collect(self) -> SettingsEdit:
        def adapter(role: str) -> AdapterEdit:
            return AdapterEdit(
                adapter=self.adapter_vars[role].get(),
                fields=parse_field_lines(self.field_texts[role].get("1.0", "end")),
            )

        return SettingsEdit(
            duration_tiers=parse_duration_tiers(self.tiers_var.get()),
            default_duration=int(self.default_var.get().strip()),
            mode=mode_from_label(self.mode_var.get()),
            planner=adapter("planner"),
            video=adapter("video"),
            voice=adapter("voice"),
            music=adapter("music"),
            visual=adapter("visual"),
        )

    def _save(self) -> None:
        try:
            edit = self._collect()
            saved = save_settings(self.home, build_settings_patch(edit))
        except ValueError:
            messagebox.showerror("设置未保存", "时长档和默认时长要写成整数", parent=self)
            return
        except VideoAgentError as exc:
            messagebox.showerror("设置未保存", error_dialog_text(exc.code, exc.message, exc.stage), parent=self)
            return
        self.on_saved(saved.mode)
        self.destroy()


def open_settings(parent, home: Path, on_saved) -> None:
    SettingsDialog(parent, home, on_saved)
