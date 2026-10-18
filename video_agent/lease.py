import threading
from contextlib import contextmanager
from datetime import timedelta

from video_agent.errors import VideoAgentError

MAX_AUTO_RETRIES = 2


class LeaseLostError(VideoAgentError):
    def __init__(self):
        super().__init__('STATE_CONFLICT', '工作租约已失效，结果未写回')


def redact_error(agent, error):
    """Remove active snapshot credentials before a provider error reaches SQL."""
    from video_agent.config import _is_secret
    secrets = set()
    def collect(value, sensitive=False):
        if isinstance(value, dict):
            for key, item in value.items():
                collect(item, sensitive or _is_secret(str(key)))
        elif isinstance(value, list):
            for item in value:
                collect(item, sensitive)
        elif sensitive and isinstance(value, str) and value:
            secrets.add(value)
    collect(agent.config.model_dump())
    def clean(value):
        if isinstance(value, str):
            for secret in sorted(secrets, key=len, reverse=True):
                value = value.replace(secret, '[redacted]')
            return value
        if isinstance(value, dict):
            return {key: clean(item) for key, item in value.items()}
        if isinstance(value, list):
            return [clean(item) for item in value]
        return value
    return VideoAgentError(error.code, clean(error.message), stage=error.stage,
        retryable=error.retryable, task_id=error.task_id, generation_id=error.generation_id,
        plan_version=error.plan_version, fields=error.fields, shot_ids=error.shot_ids,
        details=clean(error.details))


def assert_lease(conn, work_id: str, owner: str, clock):
    row = conn.execute(conn.for_update('SELECT * FROM work_items WHERE work_id=:id'), {'id': work_id}).fetchone()
    if row is None or row['done'] or row['lease_owner'] != owner or not row['lease_until'] or row['lease_until'] <= clock.now().isoformat():
        raise LeaseLostError()
    return row


@contextmanager
def owned_transaction(db, work_id, owner, clock):
    with db.transaction() as conn:
        locked = assert_lease(conn, work_id, owner, clock)
        yield conn
        # A task lock or local rename may have outlived the deadline while this
        # row was locked. Roll back every outcome, including lease release.
        if locked['lease_until'] <= clock.now().isoformat():
            raise LeaseLostError()


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
    lost = threading.Event()

    def renew() -> None:
        now = clock.now()
        until = (now + timedelta(seconds=int(lease_seconds))).isoformat()
        with db.transaction() as conn:
            result = conn.execute(
                """UPDATE work_items
                   SET lease_until=:until
                   WHERE work_id=:id AND lease_owner=:owner AND done=0 AND lease_until>:now""",
                {'until': until, 'id': work_id, 'owner': owner, 'now': now.isoformat()},
            )
            if result.rowcount != 1:
                # MySQL may report an unchanged value; verify under its row lock.
                assert_lease(conn, work_id, owner, clock)
        renewed = getattr(sleeper, "renewed", None)
        if isinstance(renewed, threading.Event):
            renewed.set()

    def loop() -> None:
        while not sleeper.wait(int(lease_seconds) / 2, stop):
            try:
                renew()
            except Exception:
                lost.set()
                return

    # Initial renewal is synchronous: a failed startup never runs the provider.
    renew()
    thread = threading.Thread(target=loop, name="video-agent-lease", daemon=True)
    thread.start()
    try:
        yield
        if lost.is_set():
            raise LeaseLostError()
    finally:
        stop.set()
        release = getattr(sleeper, "release", None)
        if release is not None:
            release()
        thread.join(timeout=5)
