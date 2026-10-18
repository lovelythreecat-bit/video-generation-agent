import tkinter as tk
from threading import Event

import pytest

from desktop_app.copyable import MessageDialog, SelectableText


@pytest.fixture(scope="module")
def window(tmp_path_factory):
    from desktop_app.app import MainWindow

    class Agent:
        def list_tasks(self):
            return []

    try:
        host = MainWindow(Agent(), tmp_path_factory.mktemp("home"), True, "fake", [10], 10, 4, Event())
    except tk.TclError as exc:
        pytest.skip(f"无法创建窗口：{exc}")
    host.withdraw()
    yield host
    host.destroy()


def test_selectable_text_keeps_full_message_and_copies_it(window):
    message = "PROVIDER_REJECTED\n接口拒绝了这次生成，原因是模型名不存在"
    host = tk.Toplevel(window)
    box = SelectableText(host, height=2, max_height=4)
    box.pack(fill="x")
    try:
        box.set_text(message)
        assert box.get_text() == message
        assert str(box.cget("state")) == "disabled"
        box.insert("end", "多余")
        assert box.get_text() == message
        box.copy_all()
        assert window.clipboard_get() == message
        box.select_all()
        box.copy_selection_or_all()
        assert window.clipboard_get() == message
    finally:
        host.destroy()


def test_message_dialog_copies_error_text_and_restores_grab(window):
    host = tk.Toplevel(window)
    host.grab_set()
    dialog = MessageDialog(host, "操作被拒绝", "VALIDATION_ERROR\n时长不在档位内\nplanning")
    try:
        assert dialog.text.get_text() == "VALIDATION_ERROR\n时长不在档位内\nplanning"
        assert str(dialog.text.cget("state")) == "disabled"
        dialog.copy()
        assert window.clipboard_get() == "VALIDATION_ERROR\n时长不在档位内\nplanning"
    finally:
        dialog.close()
        assert host.grab_current() is host
        host.destroy()


def test_main_window_copies_labeled_failure_text(window):
    from desktop_app.viewmodel import failure_clipboard_text

    window.code_value.set_text("VALIDATION_ERROR")
    window.message_value.set_text("时长不在档位内")
    window.stage_value.set_text("planning")
    window.question_text.set_text("缺少产品图片")
    window.execution_text.set_text("阶段：preparing_first_frame\n已完成镜头：\n阶段说明：等待首帧")
    window._set_copy_enabled(window._copy_failure_button, "VALIDATION_ERROR", "时长不在档位内", "planning")
    assert str(window._copy_failure_button.cget("state")) == "normal"
    window._copy_failure()
    assert window.clipboard_get() == failure_clipboard_text("VALIDATION_ERROR", "时长不在档位内", "planning")
    window._copy_question()
    assert window.clipboard_get() == "缺少产品图片"
    window._copy_execution()
    assert window.clipboard_get() == "阶段：preparing_first_frame\n已完成镜头：\n阶段说明：等待首帧"
    window._set_copy_enabled(window._copy_failure_button, "", "", "")
    assert str(window._copy_failure_button.cget("state")) == "disabled"
