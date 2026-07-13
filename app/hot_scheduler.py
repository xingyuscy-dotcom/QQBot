import subprocess
import sys
import threading
import time
from datetime import date, datetime, timedelta

from .db import log_event
from .paths import PROJECT_ROOT
from .settings import get_settings, set_setting


_STOP_EVENT = threading.Event()
_THREAD: threading.Thread | None = None


def start_hot_scheduler() -> None:
    global _THREAD
    if _THREAD and _THREAD.is_alive():
        return
    _STOP_EVENT.clear()
    _THREAD = threading.Thread(target=_run_scheduler, daemon=True)
    _THREAD.start()


def stop_hot_scheduler() -> None:
    _STOP_EVENT.set()


def _run_scheduler() -> None:
    while not _STOP_EVENT.is_set():
        settings = get_settings()
        if settings.get("hot.daily_archive_enabled", "1") != "1":
            _sleep_seconds(300)
            continue

        now = datetime.now()
        target = now.date() - timedelta(days=1)
        last_done = settings.get("hot.last_daily_archive_date", "")
        if now.hour == 0 and last_done != target.isoformat():
            _update_one_day_with_retries(target)

        _sleep_seconds(seconds_until_next_check())


def _update_one_day_with_retries(target: date) -> None:
    for attempt in range(1, 4):
        if _STOP_EVENT.is_set():
            return
        ok, output = run_hot_history_update(target.isoformat(), target.isoformat())
        if ok:
            set_setting("hot.last_daily_archive_date", target.isoformat())
            log_event("info", "hot", "daily hot history update finished", output[-800:])
            if get_settings().get("rag.enabled", "1") == "1":
                from .rag_store import start_rag_index

                start_rag_index(False)
            return

        log_event("warning", "hot", "daily hot history update failed", f"attempt={attempt}\n{output[-800:]}")
        if attempt < 3:
            _sleep_seconds(1800)


def run_hot_history_update(date_from: str, date_to: str) -> tuple[bool, str]:
    script = PROJECT_ROOT / "scripts" / "update_hot_history.py"
    command = [sys.executable, str(script), "--from", date_from, "--to", date_to]
    process = subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=1800,
    )
    return process.returncode == 0, process.stdout or ""


def seconds_until_next_check() -> int:
    now = datetime.now()
    if now.hour == 0:
        return 60
    tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(60, min(300, int((tomorrow - now).total_seconds())))


def _sleep_seconds(seconds: int) -> None:
    _STOP_EVENT.wait(max(1, seconds))
