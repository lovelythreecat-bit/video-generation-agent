import threading

from video_agent import VideoAgent
from video_agent.db import Database
from video_agent.runtime import RuntimeSettings
from video_agent.settings_service import SettingsService

from desktop_app.app import run
from desktop_app.worker import start_worker


def main() -> None:
    settings = RuntimeSettings.from_env(validate_tokens=False)
    home = settings.home
    database = Database.from_settings(settings)
    stop = threading.Event()
    thread = None
    try:
        service = SettingsService(database, settings.secret_key)
        agent = VideoAgent(home=home, database=database, settings_service=service)
        thread = start_worker(agent, stop)
        run(agent, home, stop)
    finally:
        stop.set()
        if thread is not None:
            thread.join(timeout=10)
        database.close()


if __name__ == "__main__":
    main()
