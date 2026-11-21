"""Small shared helpers: time zones, media cleanup, error tracking, task supervision."""
from __future__ import annotations

import asyncio
import collections
import datetime as dt
import logging
import os
import random
import time
from pathlib import Path
from typing import Awaitable, Callable, Iterable, Optional
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)


def parse_hhmm(value: str) -> dt.time:
    h, m = value.strip().split(":")
    return dt.time(int(h), int(m))


def now_local(tz: str) -> dt.datetime:
    return dt.datetime.now(ZoneInfo(tz))


def in_time_window(t: dt.time, start: str, end: str) -> bool:
    """True if ``t`` is within [start, end). Handles windows that cross midnight."""
    s, e = parse_hhmm(start), parse_hhmm(end)
    if s == e:
        return False
    if s < e:
        return s <= t < e
    return t >= s or t < e


def start_of_day_ts(tz: str) -> float:
    n = now_local(tz)
    return n.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def cleanup_media(paths: Iterable[str], keep: Optional[set[str]] = None) -> None:
    """Delete downloaded media files, ignoring errors. ``keep`` = paths still needed."""
    for p in paths:
        if keep and p in keep:
            continue
        try:
            os.remove(p)
        except FileNotFoundError:
            pass
        except OSError as exc:
            log.warning("could not delete media %s: %r", p, exc)


def sweep_media_dir(media_dir: Path, keep: set[str], max_age_hours: float = 24) -> int:
    """Remove old media files not referenced by pending/queued deals."""
    if not media_dir.exists():
        return 0
    cutoff = time.time() - max_age_hours * 3600
    removed = 0
    for f in media_dir.iterdir():
        if f.is_file() and str(f) not in keep and f.stat().st_mtime < cutoff:
            try:
                f.unlink()
                removed += 1
            except OSError:
                pass
    return removed


Notifier = Callable[[str], Awaitable[None]]


class ErrorTracker:
    """Counts errors; alerts the admin when too many happen in a short window."""

    def __init__(self, threshold: int, window_minutes: float, notify: Optional[Notifier] = None,
                 cooldown_minutes: float = 30):
        self.threshold = threshold
        self.window = window_minutes * 60
        self.cooldown = cooldown_minutes * 60
        self.notify = notify
        self.events: collections.deque[tuple[float, str, str]] = collections.deque(maxlen=500)
        self.total = 0
        self._last_alert = 0.0

    def recent(self, seconds: float = 86400) -> list[tuple[float, str, str]]:
        cutoff = time.time() - seconds
        return [e for e in self.events if e[0] >= cutoff]

    async def report(self, component: str, error: str) -> None:
        now = time.time()
        self.total += 1
        self.events.append((now, component, error[:300]))
        in_window = [e for e in self.events if e[0] >= now - self.window]
        if self.notify and len(in_window) >= self.threshold and now - self._last_alert > self.cooldown:
            self._last_alert = now
            summary = "\n".join(f"• {c}: {e[:120]}" for _, c, e in in_window[-5:])
            try:
                await self.notify(f"⚠️ {len(in_window)} errors in the last "
                                  f"{int(self.window / 60)} min:\n{summary}")
            except Exception:  # noqa: BLE001
                log.exception("failed to send error alert")


async def supervise(name: str, factory: Callable[[], Awaitable[None]], stop: asyncio.Event,
                    errors: Optional[ErrorTracker] = None, max_backoff: float = 300) -> None:
    """Run ``factory()`` forever, restarting it with exponential backoff if it crashes."""
    attempt = 0
    while not stop.is_set():
        started = time.monotonic()
        try:
            await factory()
            if stop.is_set():
                return
            log.warning("task %s exited; restarting", name)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception("task %s crashed", name)
            if errors:
                await errors.report(name, repr(exc))
        if time.monotonic() - started > 600:
            attempt = 0  # it ran fine for a while — reset backoff
        attempt += 1
        delay = min(max_backoff, 2 ** min(attempt, 10)) + random.uniform(0, 1)
        try:
            await asyncio.wait_for(stop.wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass
