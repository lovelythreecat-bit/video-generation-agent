from __future__ import annotations

import threading
from collections.abc import Callable

from desktop_app.viewmodel import IDLE_SLEEP_SECONDS


def tick_loop(agent, stop_event: threading.Event, sleep: Callable[[float], None] | None = None) -> None:
    while not stop_event.is_set():
        result = agent.tick()
        if result.did_work:
            continue
        if sleep is None:
            stop_event.wait(IDLE_SLEEP_SECONDS)
        else:
            sleep(IDLE_SLEEP_SECONDS)


def start_worker(agent, stop_event: threading.Event, sleep: Callable[[float], None] | None = None) -> threading.Thread:
    if getattr(agent, "settings_service", None) is not None:
        from video_agent.worker_main import run_worker

        target, args = run_worker, (agent, stop_event)
    else:
        # Explicit legacy/test agents have no database singleton protocol.
        agent.recover()
        target, args = tick_loop, (agent, stop_event, sleep)
    thread = threading.Thread(
        target=target,
        args=args,
        name="video-agent-worker",
        daemon=True,
    )
    thread.start()
    return thread
