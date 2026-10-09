from __future__ import annotations

import os
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from video_agent import VideoAgentError
from video_agent.config import load_config
from video_agent.media import ffmpeg_available
from video_agent.models import ShotTextPatch

from desktop_app.viewmodel import (
    ASPECT_RATIOS,
    FIRST_FRAME_NONE,
    POLL_INTERVAL_MS,
    WINDOW_TITLE,
    DemandEdit,
    PlanEdit,
    build_create_request,
    build_input_patch,
    build_plan_change,
    build_regenerate_full,
    build_regenerate_shots,
    button_states_for,
    command_enabled,
    error_dialog_text,
    failure_text,
    ffmpeg_label,
    first_frame_asset_id,
    first_frame_label,
    format_artifact,
    format_execution,
    format_plan,
    format_task_row,
    home_label,
    mode_label,
    new_idempotency_key,
    parse_selling_points,
    question_text,
    shot_checkbox_items,
    shot_plan_version,
    should_poll,
)


class MainWindow(tk.Tk):
    def __init__(self, agent, home: Path, ffmpeg_ok: bool, mode: str, duration_tiers: list[int], default_duration: int, max_images: int, stop_event: threading.Event) -> None:
        super().__init__()
        self.agent = agent
        self.home = home
        self.ffmpeg_ok = ffmpeg_ok
        self.mode = mode
        self.duration_tiers = list(duration_tiers)
        self.default_duration = default_duration
        self.max_images = max_images
        self.stop_event = stop_event
        self.current = None
        self.artifacts = []
        self.head_plan = None
        self.selected_id = None
        self._task_ids = []
        self._busy = False
        self._poll_job = None
        self._buttons = {}
        self._open_buttons = []
        self._image_ids = []
        self._image_names = []
        self._images_changed = False
        self._music_asset_id = None
        self._shot_rows = []
        self._shot_checks = {}
        self._check_artifact_id = None
        self.title(WINDOW_TITLE)
        self.geometry("1180x820")
        self._build()
        self.refresh_tasks()
        self._apply_buttons()
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
        middle.grid(row=1, column=1, sticky="nsew")
        middle.rowconfigure(0, weight=1)
        middle.columnconfigure(0, weight=1)
        canvas = tk.Canvas(middle, highlightthickness=0)
        scroll = ttk.Scrollbar(middle, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scroll.set)
        canvas.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        body = ttk.Frame(canvas)
        body_id = canvas.create_window((0, 0), window=body, anchor="nw")
        body.bind("<Configure>", lambda _event: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(body_id, width=event.width))

        question_box = ttk.LabelFrame(body, text="需要补充", padding=8)
        question_box.pack(fill="x", pady=4)
        self.question_text = ttk.Label(question_box, text="", justify="left")
        self.question_text.pack(anchor="w")

        demand = ttk.LabelFrame(body, text="需求", padding=8)
        demand.pack(fill="x", pady=4)
        self.name_var = tk.StringVar()
        self.category_var = tk.StringVar()
        self.specs_var = tk.StringVar()
        self.duration_var = tk.StringVar(value=str(self.default_duration))
        self.aspect_var = tk.StringVar(value="9:16")
        self.subtitles_var = tk.BooleanVar(value=True)
        self.voiceover_var = tk.BooleanVar(value=True)
        self.music_var = tk.BooleanVar(value=True)
        self.language_var = tk.StringVar()
        self.voice_pref_var = tk.StringVar()
        self.music_dir_var = tk.StringVar()
        self.first_frame_var = tk.StringVar(value=FIRST_FRAME_NONE)
        self._entry(demand, "产品名称", self.name_var)
        self._entry(demand, "品类", self.category_var)
        ttk.Label(demand, text="卖点（一行一条）").pack(anchor="w")
        self.selling_text = tk.Text(demand, height=3, width=80)
        self.selling_text.pack(fill="x")
        self._entry(demand, "规格", self.specs_var)
        ttk.Label(demand, text="自由描述").pack(anchor="w")
        self.prompt_text = tk.Text(demand, height=3, width=80)
        self.prompt_text.pack(fill="x")
        self.duration_box = self._combo(demand, "时长档位", self.duration_var, [str(item) for item in self.duration_tiers])
        self._combo(demand, "画面比例", self.aspect_var, list(ASPECT_RATIOS))
        switches = ttk.Frame(demand)
        switches.pack(anchor="w", pady=2)
        ttk.Checkbutton(switches, text="字幕", variable=self.subtitles_var).pack(side="left")
        ttk.Checkbutton(switches, text="配音", variable=self.voiceover_var).pack(side="left")
        ttk.Checkbutton(switches, text="音乐", variable=self.music_var).pack(side="left")
        self._entry(demand, "配音语言", self.language_var)
        self._entry(demand, "配音偏好", self.voice_pref_var)
        self._entry(demand, "音乐说明", self.music_dir_var)
        picture_row = ttk.Frame(demand)
        picture_row.pack(fill="x", pady=2)
        ttk.Button(picture_row, text="添加图片", command=self._add_images).pack(side="left")
        ttk.Button(picture_row, text="移除选中图片", command=self._remove_image).pack(side="left", padx=4)
        ttk.Button(picture_row, text="清空图片", command=self._clear_images).pack(side="left")
        ttk.Button(picture_row, text="选择音乐文件", command=self._add_music).pack(side="left", padx=4)
        self.image_list = tk.Listbox(demand, height=4, exportselection=False)
        self.image_list.pack(fill="x")
        self.first_frame_box = self._combo(demand, "首帧", self.first_frame_var, [FIRST_FRAME_NONE])
        self.music_label = ttk.Label(demand, text="已登记音乐：")
        self.music_label.pack(anchor="w")
        self.clear_music_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(demand, text="清除已登记音乐", variable=self.clear_music_var).pack(anchor="w")
        demand_buttons = ttk.Frame(demand)
        demand_buttons.pack(anchor="w", pady=4)
        self._buttons["create"] = ttk.Button(demand_buttons, text="创建任务", command=self._create_clicked)
        self._buttons["create"].pack(side="left", padx=2)
        self._buttons["submit_inputs"] = ttk.Button(demand_buttons, text="补充信息", command=self._submit_inputs_clicked)
        self._buttons["submit_inputs"].pack(side="left", padx=2)

        plan = ttk.LabelFrame(body, text="方案", padding=8)
        plan.pack(fill="x", pady=4)
        self.plan_summary = tk.Text(plan, height=10, width=80, state="disabled")
        self.plan_summary.pack(fill="x")
        self.plan_duration_var = tk.StringVar(value=str(self.default_duration))
        self.plan_aspect_var = tk.StringVar(value="9:16")
        self.plan_fit_var = tk.StringVar(value="pad")
        self.plan_subtitles_var = tk.BooleanVar(value=True)
        self.plan_voiceover_var = tk.BooleanVar(value=True)
        self.plan_music_var = tk.BooleanVar(value=True)
        self.plan_language_var = tk.StringVar()
        self.plan_voice_var = tk.StringVar()
        self.plan_music_dir_var = tk.StringVar()
        self.instruction_var = tk.StringVar()
        self.plan_duration_box = self._combo(plan, "方案时长", self.plan_duration_var, [str(item) for item in self.duration_tiers])
        self._combo(plan, "方案比例", self.plan_aspect_var, list(ASPECT_RATIOS))
        self._combo(plan, "比例适配", self.plan_fit_var, ["native", "pad", "crop"])
        plan_switches = ttk.Frame(plan)
        plan_switches.pack(anchor="w")
        ttk.Checkbutton(plan_switches, text="字幕", variable=self.plan_subtitles_var).pack(side="left")
        ttk.Checkbutton(plan_switches, text="配音", variable=self.plan_voiceover_var).pack(side="left")
        ttk.Checkbutton(plan_switches, text="音乐", variable=self.plan_music_var).pack(side="left")
        self._entry(plan, "方案语言", self.plan_language_var)
        self._entry(plan, "方案配音偏好", self.plan_voice_var)
        self._entry(plan, "方案音乐说明", self.plan_music_dir_var)
        ttk.Label(plan, text="镜头文案").pack(anchor="w")
        self.shot_editor = ttk.Frame(plan)
        self.shot_editor.pack(fill="x")
        self._entry(plan, "自然语言修改", self.instruction_var)
        plan_buttons = ttk.Frame(plan)
        plan_buttons.pack(anchor="w", pady=4)
        self._buttons["revise_plan"] = ttk.Button(plan_buttons, text="提交修改", command=self._revise_clicked)
        self._buttons["revise_plan"].pack(side="left", padx=2)
        self._buttons["confirm"] = ttk.Button(plan_buttons, text="确认当前方案", command=self._confirm_clicked)
        self._buttons["confirm"].pack(side="left", padx=2)
        self._buttons["discard_revision"] = ttk.Button(plan_buttons, text="放弃未确认草稿", command=self._discard_clicked)
        self._buttons["discard_revision"].pack(side="left", padx=2)

        execution = ttk.LabelFrame(body, text="执行", padding=8)
        execution.pack(fill="x", pady=4)
        self.execution_text = ttk.Label(execution, text="", justify="left")
        self.execution_text.pack(anchor="w")
        self.artifact_frame = ttk.Frame(execution)
        self.artifact_frame.pack(fill="x", pady=4)
        ttk.Label(execution, text="按镜头重生成（勾选来自最新成功成品的方案）").pack(anchor="w")
        self.shot_checks_frame = ttk.Frame(execution)
        self.shot_checks_frame.pack(fill="x")
        exec_buttons = ttk.Frame(execution)
        exec_buttons.pack(anchor="w", pady=4)
        self._buttons["regenerate_full"] = ttk.Button(exec_buttons, text="整条重生成", command=self._regenerate_full_clicked)
        self._buttons["regenerate_full"].pack(side="left", padx=2)
        self._buttons["regenerate_shots"] = ttk.Button(exec_buttons, text="按镜头重生成", command=self._regenerate_shots_clicked)
        self._buttons["regenerate_shots"].pack(side="left", padx=2)
        self._buttons["retry_planning"] = ttk.Button(exec_buttons, text="重试失败的规划", command=self._retry_planning_clicked)
        self._buttons["retry_planning"].pack(side="left", padx=2)
        self._buttons["retry_generation"] = ttk.Button(exec_buttons, text="重试失败的执行", command=self._retry_generation_clicked)
        self._buttons["retry_generation"].pack(side="left", padx=2)

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

    def _entry(self, parent, label: str, variable: tk.StringVar) -> None:
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=1)
        ttk.Label(row, text=label, width=16).pack(side="left")
        ttk.Entry(row, textvariable=variable).pack(side="left", fill="x", expand=True)

    def _combo(self, parent, label: str, variable: tk.StringVar, values: list[str]) -> ttk.Combobox:
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=1)
        ttk.Label(row, text=label, width=16).pack(side="left")
        box = ttk.Combobox(row, textvariable=variable, values=values, state="readonly")
        box.pack(side="left", fill="x", expand=True)
        return box

    def _open_settings(self) -> None:
        from desktop_app.settings_dialog import open_settings

        open_settings(self, self.home, on_saved=self._apply_saved_mode)

    def _apply_saved_mode(self, mode: str) -> None:
        self.mode = mode
        self.mode_text.configure(text=mode_label(mode))
        config = load_config(self.home)
        self.duration_tiers = list(config.duration_tiers)
        self.default_duration = int(config.default_duration)
        self.max_images = int(config.max_images)
        values = [str(item) for item in self.duration_tiers]
        self.duration_box.configure(values=values)
        self.plan_duration_box.configure(values=values)
        if self.duration_var.get() not in values and values:
            self.duration_var.set(values[0])
        if self.plan_duration_var.get() not in values and values:
            self.plan_duration_var.set(values[0])

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
        self._images_changed = False
        self.reload_selected(refill_form=True)

    def reload_selected(self, *, refill_form: bool) -> None:
        if self.selected_id is None:
            return
        previous = self.current
        self.current = self.agent.get_task(self.selected_id)
        self.artifacts = self.agent.list_artifacts(self.selected_id)
        self._render_status()
        self._render_artifacts(reset_checks=False)
        version_changed = previous is None or previous.task_id != self.current.task_id or previous.head_plan_version != self.current.head_plan_version
        if refill_form or version_changed:
            self._refill_forms()
            self._render_artifacts(reset_checks=True)
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

    def _load_head(self):
        task = self.current
        if task is None or task.head_plan_version is None:
            return None
        return self.agent.get_plan(task.task_id, task.head_plan_version)

    def _load_display_plan(self):
        head = self._load_head()
        if head is not None:
            return head
        task = self.current
        if task is None or task.confirmed_plan_version is None:
            return None
        return self.agent.get_plan(task.task_id, task.confirmed_plan_version)

    def _refill_forms(self) -> None:
        task = self.current
        if task is None:
            return
        info = task.product_info
        self.name_var.set(info.name or task.product_name or "")
        self.category_var.set(info.category or "")
        self.specs_var.set(info.specs or "")
        self.selling_text.delete("1.0", "end")
        self.selling_text.insert("1.0", "\n".join(info.selling_points))
        self.prompt_text.delete("1.0", "end")
        self.prompt_text.insert("1.0", task.user_prompt)
        self.instruction_var.set("")
        self.clear_music_var.set(False)
        self._image_ids = list(task.image_asset_ids)
        self._image_names = list(task.image_asset_ids)
        self.image_list.delete(0, "end")
        for image_id, name in zip(self._image_ids, self._image_names):
            self.image_list.insert("end", first_frame_label(name, image_id))
        plan = self._load_display_plan()
        self.head_plan = self._load_head()
        self._set_plan_summary(plan)
        if plan is None:
            video = task.video_options
            audio = task.audio_options
            duration = video.duration_seconds if video.duration_seconds is not None else self.default_duration
            self.duration_var.set(str(duration))
            self.aspect_var.set(video.aspect_ratio)
            self.subtitles_var.set(video.subtitles)
            self.voiceover_var.set(video.voiceover)
            self.music_var.set(video.music)
            self.language_var.set(audio.language or "")
            self.voice_pref_var.set(audio.voice_preference)
            self.music_dir_var.set(audio.music_direction)
            self._music_asset_id = audio.music_asset_id
            self.music_label.configure(text=f"已登记音乐：{audio.music_asset_id or ''}")
            self.plan_duration_var.set(str(self.default_duration))
            self.plan_aspect_var.set("9:16")
            self.plan_fit_var.set("pad")
            source = "selected" if task.first_frame_asset_id else "generate"
            self._set_first_frame(task.first_frame_asset_id, source)
            self._rebuild_shot_editors(None)
            return
        doc = plan.document
        self.duration_var.set(str(doc.duration_seconds))
        self.aspect_var.set(doc.aspect_ratio)
        self.subtitles_var.set(doc.subtitles)
        self.voiceover_var.set(doc.voiceover)
        self.music_var.set(doc.music)
        self.language_var.set(doc.language or "")
        self.voice_pref_var.set(doc.voice_preference)
        self.music_dir_var.set(doc.music_direction)
        self.plan_duration_var.set(str(doc.duration_seconds))
        self.plan_aspect_var.set(doc.aspect_ratio)
        self.plan_fit_var.set(doc.fit)
        self.plan_subtitles_var.set(doc.subtitles)
        self.plan_voiceover_var.set(doc.voiceover)
        self.plan_music_var.set(doc.music)
        self.plan_language_var.set(doc.language or "")
        self.plan_voice_var.set(doc.voice_preference)
        self.plan_music_dir_var.set(doc.music_direction)
        self._music_asset_id = doc.music_asset_id
        self.music_label.configure(text=f"已登记音乐：{doc.music_asset_id or ''}")
        self._set_first_frame(doc.first_frame_asset_id, doc.first_frame_source)
        self._rebuild_shot_editors(self.head_plan)

    def _set_plan_summary(self, plan) -> None:
        self.plan_summary.configure(state="normal")
        self.plan_summary.delete("1.0", "end")
        if plan is None:
            self.plan_summary.insert("1.0", "还没有方案")
        else:
            self.plan_summary.insert("1.0", format_plan(plan))
        self.plan_summary.configure(state="disabled")

    def _set_first_frame(self, asset_id: str | None, source: str) -> None:
        values = [FIRST_FRAME_NONE]
        values.extend(first_frame_label(name, image_id) for image_id, name in zip(self._image_ids, self._image_names))
        chosen = FIRST_FRAME_NONE
        if source != "generate" and asset_id:
            chosen = first_frame_label("已登记首帧", asset_id)
            known = {image_id: name for image_id, name in zip(self._image_ids, self._image_names)}
            if asset_id in known:
                chosen = first_frame_label(known[asset_id], asset_id)
            if chosen not in values:
                values.append(chosen)
        self.first_frame_box.configure(values=values)
        self.first_frame_var.set(chosen)

    def _rebuild_shot_editors(self, plan) -> None:
        for child in self.shot_editor.winfo_children():
            child.destroy()
        self._shot_rows = []
        if plan is None:
            return
        for shot in plan.document.shots:
            row = ttk.Frame(self.shot_editor)
            row.pack(fill="x", pady=1)
            ttk.Label(row, text=shot.shot_id, width=8).pack(side="left")
            fields = {}
            for key, label, value, width in (
                ("content", "内容", shot.content, 16),
                ("action", "动作", shot.action, 10),
                ("shot_size", "景别", shot.shot_size, 8),
                ("camera", "运镜", shot.camera, 8),
                ("scene_id", "场景", shot.scene_id, 8),
                ("subtitle_text", "字幕", shot.subtitle_text, 12),
                ("voiceover_text", "配音", shot.voiceover_text, 12),
            ):
                ttk.Label(row, text=label).pack(side="left")
                variable = tk.StringVar(value=value)
                ttk.Entry(row, textvariable=variable, width=width).pack(side="left")
                fields[key] = variable
            fields["shot_id"] = shot.shot_id
            self._shot_rows.append(fields)

    def _render_artifacts(self, *, reset_checks: bool) -> None:
        newest = self.artifacts[0] if self.artifacts else None
        newest_id = None if newest is None else newest.artifact_id
        for child in self.artifact_frame.winfo_children():
            child.destroy()
        self._open_buttons = []
        for artifact in self.artifacts:
            row = ttk.Frame(self.artifact_frame)
            row.pack(fill="x", pady=1)
            ttk.Label(row, text=format_artifact(artifact)).pack(side="left")
            button = ttk.Button(row, text="打开成片", command=lambda artifact_id=artifact.artifact_id: self._open_artifact(artifact_id))
            button.pack(side="left", padx=4)
            self._open_buttons.append(button)
        if reset_checks or newest_id != self._check_artifact_id:
            self._check_artifact_id = newest_id
            for child in self.shot_checks_frame.winfo_children():
                child.destroy()
            self._shot_checks = {}
            version = shot_plan_version(self.artifacts)
            if self.current is not None and version is not None:
                plan = self.agent.get_plan(self.current.task_id, version)
                for shot_id, label in shot_checkbox_items(plan):
                    variable = tk.BooleanVar(value=False)
                    ttk.Checkbutton(self.shot_checks_frame, text=label, variable=variable).pack(anchor="w")
                    self._shot_checks[shot_id] = variable
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
        open_enabled = command_enabled(states.open_artifact, False)
        for button in self._open_buttons:
            button.configure(state="normal" if open_enabled else "disabled")
        self.settings_button.configure(state="disabled" if self._busy else "normal")

    def _invoke(self, call) -> None:
        if self._busy:
            return
        self._busy = True
        self._apply_buttons()
        self.update_idletasks()
        try:
            call(new_idempotency_key())
        except VideoAgentError as exc:
            messagebox.showerror("操作被拒绝", error_dialog_text(exc.code, exc.message, exc.stage), parent=self)
        finally:
            self._busy = False
            self.refresh_tasks()
            if self.selected_id:
                self.reload_selected(refill_form=True)
            else:
                self._apply_buttons()

    def _demand_edit(self) -> DemandEdit:
        return DemandEdit(
            name=self.name_var.get(),
            category=self.category_var.get(),
            selling_points=parse_selling_points(self.selling_text.get("1.0", "end")),
            specs=self.specs_var.get(),
            user_prompt=self.prompt_text.get("1.0", "end").strip(),
            images=list(self._image_ids),
            first_frame_asset_id=first_frame_asset_id(self.first_frame_var.get()),
            duration_seconds=int(self.duration_var.get()),
            aspect_ratio=self.aspect_var.get(),
            subtitles=bool(self.subtitles_var.get()),
            voiceover=bool(self.voiceover_var.get()),
            music=bool(self.music_var.get()),
            language=self.language_var.get(),
            voice_preference=self.voice_pref_var.get(),
            music_direction=self.music_dir_var.get(),
            music_asset_id=self._music_asset_id,
        )

    def _plan_edit(self) -> PlanEdit:
        label = self.first_frame_var.get()
        clear_first = label == FIRST_FRAME_NONE
        texts = []
        for row in self._shot_rows:
            texts.append(
                ShotTextPatch(
                    shot_id=row["shot_id"],
                    content=row["content"].get(),
                    action=row["action"].get(),
                    shot_size=row["shot_size"].get(),
                    camera=row["camera"].get(),
                    scene_id=row["scene_id"].get(),
                    subtitle_text=row["subtitle_text"].get(),
                    voiceover_text=row["voiceover_text"].get(),
                )
            )
        return PlanEdit(
            duration_seconds=int(self.plan_duration_var.get()),
            aspect_ratio=self.plan_aspect_var.get(),
            fit=self.plan_fit_var.get(),
            subtitles=bool(self.plan_subtitles_var.get()),
            voiceover=bool(self.plan_voiceover_var.get()),
            music=bool(self.plan_music_var.get()),
            language=self.plan_language_var.get(),
            voice_preference=self.plan_voice_var.get(),
            music_direction=self.plan_music_dir_var.get(),
            music_asset_id=self._music_asset_id,
            clear_music_asset=bool(self.clear_music_var.get()),
            first_frame_asset_id=None if clear_first else first_frame_asset_id(label),
            clear_first_frame=clear_first,
            shot_texts=texts,
        )

    def _show_error(self, exc: VideoAgentError) -> None:
        messagebox.showerror("操作被拒绝", error_dialog_text(exc.code, exc.message, exc.stage), parent=self)

    def _add_images(self) -> None:
        paths = filedialog.askopenfilenames(
            parent=self,
            title="选择产品图片",
            filetypes=[("图片", "*.jpg *.jpeg *.png *.webp"), ("全部", "*.*")],
        )
        for path in paths:
            if len(self._image_ids) >= self.max_images:
                messagebox.showerror("图片过多", f"最多 {self.max_images} 张产品图片", parent=self)
                break
            try:
                asset_id = self.agent.register_asset(filename=Path(path).name, kind="image", path=Path(path))
            except VideoAgentError as exc:
                self._show_error(exc)
                break
            self._image_ids.append(asset_id)
            self._image_names.append(Path(path).name)
            self._images_changed = True
        self._refresh_image_list()

    def _refresh_image_list(self) -> None:
        self.image_list.delete(0, "end")
        for image_id, name in zip(self._image_ids, self._image_names):
            self.image_list.insert("end", first_frame_label(name, image_id))
        values = [FIRST_FRAME_NONE]
        values.extend(first_frame_label(name, image_id) for image_id, name in zip(self._image_ids, self._image_names))
        current = self.first_frame_var.get()
        self.first_frame_box.configure(values=values)
        if current not in values:
            self.first_frame_var.set(FIRST_FRAME_NONE)

    def _remove_image(self) -> None:
        picked = self.image_list.curselection()
        if not picked:
            return
        index = picked[0]
        del self._image_ids[index]
        del self._image_names[index]
        self._images_changed = True
        self._refresh_image_list()

    def _clear_images(self) -> None:
        self._image_ids = []
        self._image_names = []
        self._images_changed = True
        self._refresh_image_list()

    def _add_music(self) -> None:
        path = filedialog.askopenfilename(
            parent=self,
            title="选择音乐",
            filetypes=[("音频", "*.mp3 *.wav *.m4a"), ("全部", "*.*")],
        )
        if not path:
            return
        try:
            self._music_asset_id = self.agent.register_asset(filename=Path(path).name, kind="audio", path=Path(path))
        except VideoAgentError as exc:
            self._show_error(exc)
            return
        self.clear_music_var.set(False)
        self.music_label.configure(text=f"已登记音乐：{self._music_asset_id}")

    def _create_clicked(self) -> None:
        def call(key: str) -> None:
            view = self.agent.create_task(build_create_request(self._demand_edit()), idempotency_key=key)
            self.selected_id = view.task_id
        self._invoke(call)

    def _submit_inputs_clicked(self) -> None:
        def call(key: str) -> None:
            if self.current is None:
                return
            patch = build_input_patch(self._demand_edit(), images_changed=self._images_changed)
            self.agent.submit_inputs(self.current.task_id, patch, idempotency_key=key)
            self._images_changed = False
        self._invoke(call)

    def _revise_clicked(self) -> None:
        def call(key: str) -> None:
            if self.current is None:
                return
            change = build_plan_change(self.instruction_var.get(), self._plan_edit())
            self.agent.revise_plan(self.current.task_id, change, idempotency_key=key)
        self._invoke(call)

    def _confirm_clicked(self) -> None:
        def call(key: str) -> None:
            if self.current is None or self.current.head_plan_version is None:
                return
            self.agent.confirm(self.current.task_id, self.current.head_plan_version, idempotency_key=key)
        self._invoke(call)

    def _discard_clicked(self) -> None:
        def call(key: str) -> None:
            if self.current is None:
                return
            self.agent.discard_revision(self.current.task_id, idempotency_key=key)
        self._invoke(call)

    def _regenerate_full_clicked(self) -> None:
        def call(key: str) -> None:
            if self.current is None:
                return
            self.agent.regenerate(self.current.task_id, build_regenerate_full(), idempotency_key=key)
        self._invoke(call)

    def _regenerate_shots_clicked(self) -> None:
        def call(key: str) -> None:
            if self.current is None or not self.artifacts:
                return
            shot_ids = [shot_id for shot_id, variable in self._shot_checks.items() if variable.get()]
            self.agent.regenerate(
                self.current.task_id,
                build_regenerate_shots(self.artifacts[0].artifact_id, shot_ids),
                idempotency_key=key,
            )
        self._invoke(call)

    def _retry_planning_clicked(self) -> None:
        def call(key: str) -> None:
            if self.current is None:
                return
            self.agent.retry_planning(self.current.task_id, idempotency_key=key)
        self._invoke(call)

    def _retry_generation_clicked(self) -> None:
        def call(key: str) -> None:
            if self.current is None or self.current.generation_id is None:
                return
            self.agent.retry_generation(self.current.generation_id, idempotency_key=key)
        self._invoke(call)

    def _open_artifact(self, artifact_id: str) -> None:
        try:
            path = self.agent.artifact_path(artifact_id)
            os.startfile(str(path))
        except VideoAgentError as exc:
            self._show_error(exc)

    def _ensure_poll(self) -> None:
        if self._poll_job is not None:
            return
        if self.current is not None and should_poll(self.current.status):
            self._poll_job = self.after(POLL_INTERVAL_MS, self._poll)

    def _poll(self) -> None:
        self._poll_job = None
        if not self._busy and self.selected_id is not None:
            self.refresh_tasks()
            self.reload_selected(refill_form=False)
            return
        self._ensure_poll()

    def _close(self) -> None:
        if self._poll_job is not None:
            self.after_cancel(self._poll_job)
        self.stop_event.set()
        self.destroy()


def run(agent, home: Path, stop_event: threading.Event) -> None:
    config = load_config(home)
    window = MainWindow(
        agent,
        home,
        ffmpeg_available(),
        config.mode,
        list(config.duration_tiers),
        int(config.default_duration),
        int(config.max_images),
        stop_event,
    )
    window.mainloop()
