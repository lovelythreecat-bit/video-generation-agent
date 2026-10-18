"""Independent, database-backed worker: python -m video_agent.worker_main."""
import logging
import signal
import threading
import uuid
from datetime import timedelta

from video_agent.lease import LeaseLostError

LEASE_SECONDS = 30
HEARTBEAT_SECONDS = 5
log = logging.getLogger(__name__)


def acquire_worker(database, owner, clock):
    now = clock.now()
    with database.transaction() as conn:
        sql = 'INSERT INTO worker_heartbeats (singleton_id, worker_owner, lease_until, last_seen_at) VALUES (1, NULL, NULL, NULL)'
        sql += ' ON DUPLICATE KEY UPDATE singleton_id=singleton_id' if conn.dialect_name == 'mysql' else ' ON CONFLICT(singleton_id) DO NOTHING'
        conn.execute(sql)
        row = conn.execute(conn.for_update('SELECT * FROM worker_heartbeats WHERE singleton_id=1')).fetchone()
        if row['worker_owner'] and row['lease_until'] and row['lease_until'] > now.isoformat():
            return False
        conn.execute('UPDATE worker_heartbeats SET worker_owner=:owner, lease_until=:until, last_seen_at=:now, stopping=0 WHERE singleton_id=1',
            {'owner': owner, 'until': (now + timedelta(seconds=LEASE_SECONDS)).isoformat(), 'now': now.isoformat()})
    return True


def renew_worker(database, owner, clock):
    now = clock.now()
    with database.transaction() as conn:
        result = conn.execute('UPDATE worker_heartbeats SET lease_until=:until, last_seen_at=:now WHERE singleton_id=1 AND worker_owner=:owner AND lease_until>:now',
            {'until': (now + timedelta(seconds=LEASE_SECONDS)).isoformat(), 'now': now.isoformat(), 'owner': owner})
        return result.rowcount == 1


def release_worker(database, owner):
    with database.transaction() as conn:
        conn.execute('UPDATE worker_heartbeats SET worker_owner=NULL, lease_until=NULL, stopping=1 WHERE singleton_id=1 AND worker_owner=:owner', {'owner': owner})


def run_worker(agent, stop_event: threading.Event) -> None:
    if stop_event.is_set():
        return
    owner = str(uuid.uuid4())
    if not acquire_worker(agent.db, owner, agent.clock):
        return
    heartbeat_stop = threading.Event()
    lost = threading.Event()

    def heartbeat():
        while not heartbeat_stop.wait(HEARTBEAT_SECONDS):
            try:
                if not renew_worker(agent.db, owner, agent.clock):
                    lost.set()
                    return
            except Exception:
                log.error('Worker heartbeat failed; new work is suspended')
                lost.set()
                return

    thread = threading.Thread(target=heartbeat, name='video-agent-heartbeat', daemon=True)
    thread.start()
    try:
        agent.recover()
        while not stop_event.is_set() and not lost.is_set():
            try:
                # Renewal supports liveness. tick validates this runtime again
                # under the singleton lock in its work-claim transaction.
                if not renew_worker(agent.db, owner, agent.clock):
                    raise LeaseLostError()
                result = agent.tick(runtime_owner=owner)
            except LeaseLostError:
                raise RuntimeError('Worker lease lost') from None
            except Exception:
                log.error('Worker stopped after a database or execution failure')
                raise RuntimeError('Worker execution failed') from None
            if not result.did_work:
                stop_event.wait(0.25)
        if lost.is_set() and not stop_event.is_set():
            raise RuntimeError('Worker heartbeat failed')
    finally:
        heartbeat_stop.set()
        thread.join(timeout=HEARTBEAT_SECONDS + 1)
        try:
            release_worker(agent.db, owner)
        except Exception:
            log.error('Worker lease release failed; it will expire automatically')


def main() -> int:
    from video_agent.db import Database
    from video_agent.runtime import RuntimeSettings
    from video_agent.service import VideoAgent
    from video_agent.settings_service import SettingsService
    database = None
    try:
        settings = RuntimeSettings.from_env(validate_tokens=False)
        from cryptography.fernet import Fernet
        if not settings.secret_key:
            raise ValueError('Missing configuration encryption key')
        Fernet(settings.secret_key.encode('ascii'))
        database = Database.from_settings(settings)
        service = SettingsService(database, settings.secret_key)
        agent = VideoAgent(settings.home, database=database, settings_service=service)
        stop = threading.Event()
        for name in ('SIGINT', 'SIGTERM'):
            signal.signal(getattr(signal, name), lambda *_: stop.set())
        run_worker(agent, stop)
        return 0
    except Exception:
        log.error('Worker startup or execution failed; check database and runtime settings')
        return 1
    finally:
        if database is not None:
            database.close()


if __name__ == '__main__':
    raise SystemExit(main())
