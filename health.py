"""Health: daily summary and 'no messages read for X hours' alert."""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time
from typing import TYPE_CHECKING, Awaitable, Callable

from config import Settings
from db import Database
from utils import ErrorTracker, now_local, parse_hhmm, start_of_day_ts, sweep_media_dir

if TYPE_CHECKING:  # pragma: no cover
    from reader import Reader

log = logging.getLogger(__name__)


class Health:
    def __init__(self, settings: Settings, db: Database, reader: "Reader",
                 notify: Callable[[str], Awaitable[None]], errors: ErrorTracker):
        self.settings = settings
        self.db = db
        self.reader = reader
        self.notify = notify
        self.errors = errors
        self.started_at = time.time()
        self._silence_alerted = False
        self._last_summary_date: dt.date | None = None

    async def summary_text(self) -> str:
        tz = self.settings.env.timezone
        since = start_of_day_ts(tz)
        posts = await self.db.count_posts_since(since)
        pe = await self.db.count_posts_since(since, ["PRICE_ERROR"])
        seen = await self.db.count_seen_since(since)
        stats = await self.db.get_stats()
        worst = [s for s in stats if (s["seen"] or 0) >= 50 and not s["posted"]]
        lines = [
            "📊 <b>Daily summary</b>",
            f"Messages read: {seen}",
            f"Posted: {posts} (price errors: {pe})",
            f"Queue: {await self.db.count_queued()} · Pending: {await self.db.count_pending()}",
            f"Errors (24h): {len(self.errors.recent())}",
            f"Reader: {self.reader.state}",
        ]
        if worst:
            lines.append("Sources with 50+ messages and 0 posts: "
                         + ", ".join(s["source_name"] or str(s["source_id"]) for s in worst[:5]))
        return "\n".join(lines)

    async def run(self, stop: asyncio.Event) -> None:
        hc = self.settings.cfg.health
        local = now_local(self.settings.env.timezone)
        if local.time() >= parse_hhmm(hc.daily_summary_time):
            self._last_summary_date = local.date()   # don't send a summary on every restart
        while not stop.is_set():
            try:
                await self._check(hc.no_messages_alert_hours, hc.daily_summary_time)
            except Exception:  # noqa: BLE001
                log.exception("health check failed")
            try:
                await asyncio.wait_for(stop.wait(), timeout=60)
            except asyncio.TimeoutError:
                pass

    async def _check(self, silence_hours: float, summary_at: str) -> None:
        now = time.time()
        last = self.reader.last_message_at or self.started_at
        if now - last > silence_hours * 3600:
            if not self._silence_alerted:
                self._silence_alerted = True
                await self.notify(f"🔕 No messages read from sources for {silence_hours:g}h.\n"
                                  f"Reader state: {self.reader.state}. Check /sources and the logs.")
        else:
            self._silence_alerted = False

        local = now_local(self.settings.env.timezone)
        target = parse_hhmm(summary_at)
        if local.time() >= target and self._last_summary_date != local.date():
            self._last_summary_date = local.date()
            await self.notify(await self.summary_text())
            await self.db.prune()
            removed = sweep_media_dir(self.settings.env.media_dir, await self.db.referenced_media())
            if removed:
                log.info("swept %d stale media files", removed)
