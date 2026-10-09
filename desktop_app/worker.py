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
    agent.recover()
    thread = threading.Thread(
        target=tick_loop,
        args=(agent, stop_event, sleep),
        name="video-agent-worker",
        daemon=True,
    )
    thread.start()
    return thread
