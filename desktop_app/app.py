from __future__ import annotations

import threading
import tkinter as tk
from pathlib import Path
from tkinter import ttk

from video_agent.config import load_config
from video_agent.media import ffmpeg_available

from desktop_app.viewmodel import (
    POLL_INTERVAL_MS,
    WINDOW_TITLE,
    button_states_for,
    command_enabled,
    failure_text,
    ffmpeg_label,
    format_execution,
    format_task_row,
    home_label,
    mode_label,
    question_text,
    should_poll,
)


class MainWindow(tk.Tk):
    def __init__(self, agent, home: Path, ffmpeg_ok: bool, mode: str, stop_event: threading.Event) -> None:
        super().__init__()
        self.agent = agent
        self.home = home
        self.ffmpeg_ok = ffmpeg_ok
        self.mode = mode
        self.stop_event = stop_event
        self.current = None
        self.artifacts = []
        self.selected_id: str | None = None
        self._task_ids: list[str] = []
        self._busy = False
        self._poll_job = None
        self._buttons: dict[str, ttk.Button] = {}
        self.title(WINDOW_TITLE)
        self.geometry("1100x760")
        self._build()
        self.refresh_tasks()
        self.protocol("WM_DELETE_WINDOW", self._close)

    def _build(self) -> None:
        self.columnconfigure(1, weight=1)
        self.rowconfigure(1, weight=1)
        top = ttk.Frame(self, padding=8)
        top.grid(row=0, column=0, columnspan=2, sticky="ew")
        self.home_text = ttk.Label(top, text=home_label(self.home))
        self.home_text.pack(side="left", padx=(0, 12))
        self.ffmpeg_text = ttk.Label(top, text=ffmpeg_label(self.ffmpeg_ok))
        self.ffmpeg_text.pack(side="left", padx=(0, 12))
        self.mode_text = ttk.Label(top, text=mode_label(self.mode))
        self.mode_text.pack(side="left", padx=(0, 12))
        self.settings_button = ttk.Button(top, text="设置", command=self._open_settings)
        self.settings_button.pack(side="left")

        left = ttk.LabelFrame(self, text="任务", padding=8)
        left.grid(row=1, column=0, sticky="nsw", padx=8, pady=8)
        self.task_list = tk.Listbox(left, width=42, exportselection=False)
        self.task_list.pack(fill="both", expand=True)
        self.task_list.bind("<<ListboxSelect>>", self._on_select)

        middle = ttk.Frame(self, padding=8)
        middle.grid(row=1, column=1, sticky="nsew", padx=8, pady=8)
        middle.rowconfigure(1, weight=1)
        middle.columnconfigure(0, weight=1)
        question_box = ttk.LabelFrame(middle, text="需要补充", padding=8)
        question_box.grid(row=0, column=0, sticky="ew")
        self.question_text = ttk.Label(question_box, text="", justify="left")
        self.question_text.pack(anchor="w")
        body = ttk.Panedwindow(middle, orient="vertical")
        body.grid(row=1, column=0, sticky="nsew", pady=8)
        demand = ttk.LabelFrame(body, text="需求", padding=8)
        plan = ttk.LabelFrame(body, text="方案", padding=8)
        execution = ttk.LabelFrame(body, text="执行", padding=8)
        body.add(demand, weight=1)
        body.add(plan, weight=1)
        body.add(execution, weight=1)
        self.demand_slot = demand
        self.plan_slot = plan
        self.execution_slot = execution
        self.execution_text = ttk.Label(execution, text="", justify="left")
        self.execution_text.pack(anchor="w")
        button_row = ttk.Frame(middle)
        button_row.grid(row=2, column=0, sticky="ew")
        labels = (
            ("create", "创建任务"),
            ("submit_inputs", "补充信息"),
            ("revise_plan", "提交修改"),
            ("confirm", "确认当前方案"),
            ("discard_revision", "放弃未确认草稿"),
            ("regenerate_full", "整条重生成"),
            ("regenerate_shots", "按镜头重生成"),
            ("retry_planning", "重试失败的规划"),
            ("retry_generation", "重试失败的执行"),
        )
        for name, text in labels:
            button = ttk.Button(button_row, text=text)
            button.pack(side="left", padx=2, pady=2)
            self._buttons[name] = button

        bottom = ttk.LabelFrame(self, text="最近一次错误", padding=8)
        bottom.grid(row=2, column=0, columnspan=2, sticky="ew", padx=8, pady=8)
        self.code_value = ttk.Label(bottom, text="")
        self.message_value = ttk.Label(bottom, text="")
        self.stage_value = ttk.Label(bottom, text="")
        ttk.Label(bottom, text="错误码").grid(row=0, column=0, sticky="w")
        self.code_value.grid(row=0, column=1, sticky="w", padx=8)
        ttk.Label(bottom, text="说明").grid(row=1, column=0, sticky="w")
        self.message_value.grid(row=1, column=1, sticky="w", padx=8)
        ttk.Label(bottom, text="阶段").grid(row=2, column=0, sticky="w")
        self.stage_value.grid(row=2, column=1, sticky="w", padx=8)
        self._apply_buttons()

    def _open_settings(self) -> None:
        from desktop_app.settings_dialog import open_settings

        open_settings(self, self.home, on_saved=self._apply_saved_mode)

    def _apply_saved_mode(self, mode: str) -> None:
        self.mode = mode
        self.mode_text.configure(text=mode_label(mode))

    def refresh_tasks(self) -> None:
        selected = self.selected_id
        self.task_list.delete(0, "end")
        self._task_ids = []
        for summary in self.agent.list_tasks():
            self._task_ids.append(summary.task_id)
            self.task_list.insert("end", format_task_row(summary))
        if selected in self._task_ids:
            self.task_list.selection_set(self._task_ids.index(selected))

    def _on_select(self, _event=None) -> None:
        if self._busy:
            return
        picked = self.task_list.curselection()
        if not picked:
            return
        self.selected_id = self._task_ids[picked[0]]
        self.reload_selected()

    def reload_selected(self) -> None:
        if self.selected_id is None:
            return
        self.current = self.agent.get_task(self.selected_id)
        self.artifacts = self.agent.list_artifacts(self.selected_id)
        self._render_status()
        self._ensure_poll()

    def _render_status(self) -> None:
        code, message, stage = failure_text(self.current)
        self.code_value.configure(text=code)
        self.message_value.configure(text=message)
        self.stage_value.configure(text=stage)
        self.question_text.configure(text=question_text(self.current))
        if self.current is None:
            self.execution_text.configure(text="")
        else:
            self.execution_text.configure(text=format_execution(self.current))
        self._apply_buttons()

    def _apply_buttons(self) -> None:
        states = button_states_for(
            self.current,
            has_successful_artifact=bool(self.artifacts),
            ffmpeg_available=self.ffmpeg_ok,
        )
        for name, button in self._buttons.items():
            enabled = command_enabled(getattr(states, name), self._busy)
            button.configure(state="normal" if enabled else "disabled")
        if self._busy:
            self.settings_button.configure(state="disabled")
        else:
            self.settings_button.configure(state="normal")

    def _ensure_poll(self) -> None:
        if self._poll_job is not None:
            return
        if self.current is not None and should_poll(self.current.status):
            self._poll_job = self.after(POLL_INTERVAL_MS, self._poll)

    def _poll(self) -> None:
        self._poll_job = None
        if not self._busy and self.selected_id is not None:
            self.refresh_tasks()
            self.reload_selected()
            return
        self._ensure_poll()

    def _close(self) -> None:
        if self._poll_job is not None:
            self.after_cancel(self._poll_job)
        self.stop_event.set()
        self.destroy()


def run(agent, home: Path, stop_event: threading.Event) -> None:
    config = load_config(home)
    window = MainWindow(agent, home, ffmpeg_available(), config.mode, stop_event)
    window.mainloop()
