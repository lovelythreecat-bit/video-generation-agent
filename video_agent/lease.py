import threading
from contextlib import contextmanager
from datetime import timedelta

from video_agent.errors import VideoAgentError

MAX_AUTO_RETRIES = 2


class ThreadSleeper:
    def wait(self, seconds: float, stop: threading.Event) -> bool:
        return stop.wait(timeout=float(seconds))


class ManualSleeper:
    def __init__(self) -> None:
        self._gate = threading.Event()
        self.entered = threading.Event()
        self.renewed = threading.Event()
        self.seconds: list[float] = []

    def wait(self, seconds: float, stop: threading.Event) -> bool:
        self.seconds.append(float(seconds))
        self.entered.set()
        self._gate.wait()
        stopped = stop.is_set()
        if not stopped:
            self._gate.clear()
            self.entered.clear()
        return stopped

    def release(self) -> None:
        self._gate.set()


@contextmanager
def hold_lease(db, work_id: str, owner: str, clock, lease_seconds: int, *, sleeper=None):
    if int(lease_seconds) < 15:
        raise VideoAgentError("VALIDATION_ERROR", "租约不得低于 15 秒")
    if sleeper is None:
        sleeper = getattr(clock, "lease_sleeper", None)
    if sleeper is None:
        sleeper = ThreadSleeper()
    stop = threading.Event()
    started = threading.Event()

    def renew() -> None:
        until = (clock.now() + timedelta(seconds=int(lease_seconds))).isoformat()
        with db.transaction() as conn:
            conn.execute(
                """UPDATE work_items
                   SET lease_owner=?, lease_until=?
                   WHERE work_id=? AND (lease_owner IS NULL OR lease_owner=?)""",
                (owner, until, work_id, owner),
            )
            conn.execute(
                """UPDATE work_items
                   SET lease_until=?
                   WHERE done=1
                     AND task_id=(SELECT task_id FROM work_items WHERE work_id=?)""",
                (until, work_id),
            )
        renewed = getattr(sleeper, "renewed", None)
        if isinstance(renewed, threading.Event):
            renewed.set()

    def loop() -> None:
        renew()
        started.set()
        while not sleeper.wait(int(lease_seconds) / 2, stop):
            renew()

    thread = threading.Thread(target=loop, name="video-agent-lease", daemon=True)
    thread.start()
    if not started.wait(timeout=5):
        stop.set()
        release = getattr(sleeper, "release", None)
        if release is not None:
            release()
        thread.join(timeout=5)
        raise VideoAgentError("PROVIDER_TEMPORARY", "租约续期线程没有启动")
    try:
        yield
    finally:
        stop.set()
        release = getattr(sleeper, "release", None)
        if release is not None:
            release()
        thread.join(timeout=5)
