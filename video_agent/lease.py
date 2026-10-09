import threading
from contextlib import contextmanager
from datetime import timedelta

from video_agent.errors import VideoAgentError

MAX_AUTO_RETRIES = 2


@contextmanager
def hold_lease(db, work_id: str, owner: str, clock, lease_seconds: int):
    stop = threading.Event()
    started = threading.Event()

    def renew() -> None:
        until = (clock.now() + timedelta(seconds=lease_seconds)).isoformat()
        with db.transaction() as conn:
            conn.execute(
                "UPDATE work_items SET lease_owner=?, lease_until=? WHERE work_id=?",
                (owner, until, work_id),
            )

    def loop() -> None:
        renew()
        started.set()
        while not stop.wait(lease_seconds / 2):
            renew()

    thread = threading.Thread(target=loop, name="video-agent-lease", daemon=True)
    thread.start()
    if not started.wait(timeout=5):
        stop.set()
        raise VideoAgentError("PROVIDER_TEMPORARY", "租约续期线程没有启动")
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=5)
