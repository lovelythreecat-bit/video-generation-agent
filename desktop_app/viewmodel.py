from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from pathlib import Path

from video_agent.models import TaskView

POLL_INTERVAL_MS = 1000
IDLE_SLEEP_SECONDS = 1


@dataclass(frozen=True)
class ButtonStates:
    create: bool
    submit_inputs: bool
    revise_plan: bool
    confirm: bool
    discard_revision: bool
    regenerate_full: bool
    regenerate_shots: bool
    retry_planning: bool
    retry_generation: bool
    open_artifact: bool


def button_states(
    *,
    status: str | None,
    failure_scope: str | None,
    has_head: bool,
    head_confirmed: bool,
    has_confirmed_plan: bool,
    has_successful_artifact: bool,
    ffmpeg_available: bool,
) -> ButtonStates:
    can_regenerate = has_confirmed_plan and ffmpeg_available
    shots = can_regenerate and has_successful_artifact
    submit_inputs = False
    revise_plan = False
    confirm = False
    discard_revision = False
    regenerate_full = False
    regenerate_shots = False
    retry_planning = False
    retry_generation = False

    if status == "awaiting_input":
        submit_inputs = True
        discard_revision = has_confirmed_plan
    elif status == "awaiting_confirmation":
        revise_plan = True
        confirm = ffmpeg_available
        discard_revision = has_confirmed_plan
        regenerate_full = can_regenerate
        regenerate_shots = shots
    elif status == "succeeded":
        revise_plan = True
        regenerate_full = can_regenerate
        regenerate_shots = shots
    elif status == "failed" and failure_scope == "planning":
        retry_planning = True
        submit_inputs = not has_head
        revise_plan = has_head
        discard_revision = has_confirmed_plan
    elif status == "failed" and failure_scope == "generation":
        retry_generation = ffmpeg_available
        revise_plan = True
        regenerate_full = can_regenerate
        regenerate_shots = shots
        head_open = has_head and not head_confirmed
        confirm = head_open and ffmpeg_available
        discard_revision = head_open and has_confirmed_plan

    return ButtonStates(
        create=ffmpeg_available,
        submit_inputs=submit_inputs,
        revise_plan=revise_plan,
        confirm=confirm,
        discard_revision=discard_revision,
        regenerate_full=regenerate_full,
        regenerate_shots=regenerate_shots,
        retry_planning=retry_planning,
        retry_generation=retry_generation,
        open_artifact=has_successful_artifact,
    )


def button_states_for(
    task: TaskView | None,
    *,
    has_successful_artifact: bool,
    ffmpeg_available: bool,
) -> ButtonStates:
    if task is None:
        return button_states(
            status=None,
            failure_scope=None,
            has_head=False,
            head_confirmed=False,
            has_confirmed_plan=False,
            has_successful_artifact=has_successful_artifact,
            ffmpeg_available=ffmpeg_available,
        )
    has_head = task.head_plan_version is not None
    has_confirmed_plan = task.confirmed_plan_version is not None
    head_confirmed = has_head and task.head_plan_version == task.confirmed_plan_version
    return button_states(
        status=task.status,
        failure_scope=task.failure_scope,
        has_head=has_head,
        head_confirmed=head_confirmed,
        has_confirmed_plan=has_confirmed_plan,
        has_successful_artifact=has_successful_artifact,
        ffmpeg_available=ffmpeg_available,
    )


def failure_text(task: TaskView | None) -> tuple[str, str, str]:
    if task is None or task.status != "failed" or task.failure is None:
        return ("", "", "")
    return (task.failure.code, task.failure.message, task.failure.stage or "")


def question_text(task: TaskView | None) -> str:
    if task is None or task.status != "awaiting_input":
        return ""
    return "\n".join(task.questions)


def error_dialog_text(code: str, message: str, stage: str | None) -> str:
    return f"{code}\n{message}\n{stage or ''}"


def should_poll(status: str | None) -> bool:
    return status in {"pending", "planning", "generating"}


def new_idempotency_key() -> str:
    return uuid.uuid4().hex


def command_enabled(allowed: bool, busy: bool) -> bool:
    return allowed and not busy


def resolve_home() -> Path:
    raw = os.environ.get("VIDEO_AGENT_HOME")
    if raw:
        return Path(raw)
    return Path.cwd() / ".video-agent"
