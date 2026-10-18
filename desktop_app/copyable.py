from __future__ import annotations

import tkinter as tk
from tkinter import ttk


def copy_to_clipboard(widget: tk.Misc, text: str) -> None:
    widget.clipboard_clear()
    widget.clipboard_append(text)
    widget.update()


def _label_colors(widget: tk.Misc) -> tuple[str, str]:
    style = ttk.Style(widget)
    background = style.lookup("TLabel", "background") or style.lookup("TFrame", "background")
    foreground = style.lookup("TLabel", "foreground") or style.lookup("TFrame", "foreground")
    if not background:
        background = widget.winfo_toplevel().cget("bg")
    return background or "SystemButtonFace", foreground or "SystemWindowText"


class SelectableText(tk.Text):
    """Read-only text that can be selected and copied."""

    def __init__(self, master, *, height: int = 2, max_height: int = 6, field: bool = False) -> None:
        self._max_height = max_height
        if field:
            background, foreground = "white", "black"
            relief, border = "solid", 1
        else:
            background, foreground = _label_colors(master)
            relief, border = "flat", 0
        super().__init__(
            master,
            height=height,
            width=20,
            wrap="word",
            relief=relief,
            borderwidth=border,
            highlightthickness=0,
            padx=2,
            pady=1,
            background=background,
            foreground=foreground,
            insertwidth=0,
            cursor="xterm",
            state="disabled",
            exportselection=True,
        )
        self._menu = tk.Menu(self, tearoff=0)
        self._menu.add_command(label="复制", command=self.copy_selection_or_all)
        self._menu.add_command(label="全选", command=self.select_all)
        self._fitting = False
        self.bind("<Button-3>", self._popup)
        self.bind("<Control-a>", self._select_all_event)
        self.bind("<Control-A>", self._select_all_event)
        self.bind("<Configure>", self._fit_height)
        self.bind("<MouseWheel>", self._scroll)

    def set_text(self, value: str) -> None:
        text = "" if value is None else str(value)
        self.configure(state="normal")
        self.delete("1.0", "end")
        self.insert("1.0", text)
        self.configure(state="disabled")
        self._fit_height()

    def _fit_height(self, _event=None) -> None:
        if self._fitting:
            return
        self._fitting = True
        try:
            counted = self.count("1.0", "end-1c", "displaylines")
            lines = 1 if not counted or counted[0] is None else max(int(counted[0]), 1)
            target = min(lines, self._max_height)
            if int(self.cget("height")) != target:
                self.configure(height=target)
        finally:
            self._fitting = False

    def _scroll(self, event) -> str:
        self.yview_scroll(int(-event.delta / 120), "units")
        return "break"

    def get_text(self) -> str:
        return self.get("1.0", "end-1c")

    def select_all(self) -> None:
        self.tag_remove("sel", "1.0", "end")
        self.tag_add("sel", "1.0", "end-1c")
        self.focus_set()

    def copy_all(self) -> None:
        copy_to_clipboard(self, self.get_text())

    def copy_selection_or_all(self) -> None:
        try:
            selected = self.get("sel.first", "sel.last")
        except tk.TclError:
            selected = self.get_text()
        copy_to_clipboard(self, selected)

    def _select_all_event(self, _event) -> str:
        self.select_all()
        return "break"

    def _popup(self, event) -> str:
        self.focus_set()
        try:
            self._menu.tk_popup(event.x_root, event.y_root)
        finally:
            self._menu.grab_release()
        return "break"


class MessageDialog:
    def __init__(self, parent: tk.Misc, title: str, message: str) -> None:
        self.message = message
        self.window = tk.Toplevel(parent)
        self.window.title(title)
        self.window.transient(parent)
        self.window.minsize(420, 180)
        self.window.resizable(True, True)
        self.text = SelectableText(self.window, height=6, max_height=12, field=True)
        self.text.pack(fill="both", expand=True, padx=16, pady=(16, 8))
        self.text.set_text(message)
        actions = ttk.Frame(self.window)
        actions.pack(fill="x", padx=16, pady=(0, 16))
        ttk.Button(actions, text="确定", command=self.close).pack(side="right")
        ttk.Button(actions, text="复制", command=self.copy).pack(side="right", padx=(0, 8))
        self.window.bind("<Escape>", lambda _event: self.close())
        self.window.bind("<Return>", lambda _event: self.close())
        self.window.protocol("WM_DELETE_WINDOW", self.close)
        self.window.update_idletasks()
        self._place_near(parent)
        self._previous_grab = parent.grab_current()
        self.window.grab_set()
        self.text.focus_set()
        self.text.select_all()

    def copy(self) -> None:
        copy_to_clipboard(self.window, self.message)

    def close(self) -> None:
        previous = self._previous_grab
        self.window.destroy()
        if previous is None:
            return
        try:
            if previous.winfo_exists():
                previous.grab_set()
        except tk.TclError:
            return

    def _place_near(self, parent: tk.Misc) -> None:
        parent.update_idletasks()
        width = max(self.window.winfo_reqwidth(), 480)
        height = max(self.window.winfo_reqheight(), 200)
        origin_x = parent.winfo_rootx() + max((parent.winfo_width() - width) // 2, 0)
        origin_y = parent.winfo_rooty() + max((parent.winfo_height() - height) // 2, 40)
        self.window.geometry(f"{width}x{height}+{origin_x}+{origin_y}")


def show_message(parent: tk.Misc, title: str, message: str) -> None:
    dialog = MessageDialog(parent, title, message)
    dialog.window.wait_window()
