import threading

from video_agent import VideoAgent

from desktop_app.app import run
from desktop_app.viewmodel import resolve_home
from desktop_app.worker import start_worker


def main() -> None:
    home = resolve_home()
    agent = VideoAgent(home=home)
    stop = threading.Event()
    thread = start_worker(agent, stop)
    try:
        run(agent, home, stop)
    finally:
        stop.set()
        thread.join(timeout=2)


if __name__ == "__main__":
    main()
