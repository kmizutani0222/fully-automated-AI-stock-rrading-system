from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone

from app.core.config import get_settings
from app.db.session import SessionLocal
from app.runtime.cycle import run_cycle

logger = logging.getLogger(__name__)


class PaperScheduler:
    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="paper-scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def run_once(self, now: datetime | None = None) -> None:
        current = now or datetime.now(timezone.utc)
        with SessionLocal() as session:
            run_cycle(session, current, delay_seconds=get_settings().runtime_bar_delay_seconds)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception:  # noqa: BLE001 - the next bar must still be attempted
                logger.exception("paper scheduler tick failed")
            self._stop.wait(self._wait_seconds())

    @staticmethod
    def _wait_seconds() -> float:
        now = datetime.now(timezone.utc)
        delay = get_settings().runtime_bar_delay_seconds
        step = 300
        nxt = (int(now.timestamp()) // step + 1) * step + delay
        return max(5.0, nxt - now.timestamp())


_SCHEDULER = PaperScheduler()


def start_scheduler() -> PaperScheduler:
    _SCHEDULER.start()
    return _SCHEDULER


def stop_scheduler() -> None:
    _SCHEDULER.stop()
