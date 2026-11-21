"""Channel posting: persistent queue, rate limits, quiet hours, priority for price errors."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any, Awaitable, Callable, Optional

from telethon import TelegramClient
from telethon.errors import FloodWaitError, MessageNotModifiedError, RPCError

from config import Settings
from db import Database
from formatter import render_deal_over
from models import Label, ProcessedDeal
from utils import ErrorTracker, cleanup_media, in_time_window, now_local

log = logging.getLogger(__name__)

PostedCallback = Callable[[int, ProcessedDeal], Awaitable[None]]


def parse_peer(target: str) -> Any:
    """'@name' / 'name' / 't.me/name' → str; '-100123' → int."""
    t = target.strip()
    t = t.removeprefix("https://").removeprefix("http://").removeprefix("t.me/")
    try:
        return int(t)
    except ValueError:
        return t if t.startswith("@") else "@" + t


class Poster:
    def __init__(self, bot: TelegramClient, settings: Settings, db: Database,
                 errors: Optional[ErrorTracker] = None, on_posted: Optional[PostedCallback] = None,
                 clock: Callable[[], float] = time.time):
        self.bot = bot
        self.settings = settings
        self.db = db
        self.errors = errors
        self.on_posted = on_posted
        self.clock = clock
        self._wake = asyncio.Event()
        self._channel: Any = None
        self._failures: dict[int, int] = {}

    # ------------------------------------------------------------------ public
    async def channel(self) -> Any:
        if self._channel is None:
            self._channel = await self.bot.get_entity(parse_peer(self.settings.env.target_channel))
        return self._channel

    async def enqueue(self, deal: ProcessedDeal) -> int:
        pc = self.settings.cfg.posting
        is_pe = deal.label == Label.PRICE_ERROR
        max_age = pc.price_error_max_age_minutes if is_pe else pc.queue_max_age_minutes
        priority = {Label.PRICE_ERROR: 100, Label.HOT_DEAL: 50}.get(deal.label, 0)
        qid = await self.db.enqueue(deal, priority, self.clock() + max_age * 60)
        log.info("queued deal id=%s key=%s label=%s", qid, deal.deal_key, deal.label.value)
        self._wake.set()
        return qid

    def in_quiet_hours(self) -> bool:
        pc = self.settings.cfg.posting
        if not pc.quiet_hours_enabled:
            return False
        t = now_local(self.settings.env.timezone).time()
        return in_time_window(t, pc.quiet_hours_start, pc.quiet_hours_end)

    # ------------------------------------------------------------------ worker
    async def run(self, stop: asyncio.Event) -> None:
        """Queue worker. Wakes on new items or every 15s."""
        while not stop.is_set():
            try:
                await self._tick()
            except Exception as exc:  # noqa: BLE001
                log.exception("poster tick failed")
                if self.errors:
                    await self.errors.report("poster", repr(exc))
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=15)
            except asyncio.TimeoutError:
                pass

    async def _tick(self) -> None:
        now = self.clock()
        pc = self.settings.cfg.posting
        items = await self.db.queued_items()
        if not items:
            return
        quiet = self.in_quiet_hours()
        posts_last_hour = await self.db.count_posts_since(now - 3600)
        last_post = await self.db.last_post_time()
        for item in items:
            deal: ProcessedDeal = item["deal"]
            if item["expires_at"] < now:
                log.info("queue item %s expired (stale deal)", item["id"])
                await self.db.set_queue_status(item["id"], "expired")
                cleanup_media(deal.media_paths)
                continue
            if self.settings.paused:
                return
            is_pe = deal.label == Label.PRICE_ERROR
            if quiet and not is_pe:
                continue  # hold normal deals until quiet hours end (or they expire)
            bypass = is_pe and pc.price_error_bypass_rate_limit
            if not bypass:
                if posts_last_hour >= pc.max_posts_per_hour:
                    continue
                if now - last_post < pc.min_gap_seconds:
                    continue
            await self._post_item(item)
            return  # one post per tick; limits are re-evaluated next tick

    async def _post_item(self, item: dict[str, Any]) -> None:
        deal: ProcessedDeal = item["deal"]
        try:
            msg_ids = await self.publish(deal)
        except Exception as exc:  # noqa: BLE001
            n = self._failures.get(item["id"], 0) + 1
            self._failures[item["id"]] = n
            log.exception("posting failed (attempt %s) queue_id=%s", n, item["id"])
            if self.errors:
                await self.errors.report("poster", f"post failed: {exc!r}")
            if n >= 3:
                await self.db.set_queue_status(item["id"], "failed")
                cleanup_media(deal.media_paths)
            return
        await self.db.set_queue_status(item["id"], "posted")
        has_media = bool(deal.media_paths) and deal.fits_caption
        post_id = await self.db.add_posted(deal, msg_ids, deal.header_html, deal.body_html, has_media)
        await self.db.bump_stat(deal.source_id, deal.source_name, "posted")
        cleanup_media(deal.media_paths)
        log.info("posted deal post_id=%s key=%s msgs=%s", post_id, deal.deal_key, msg_ids)
        if self.on_posted:
            try:
                await self.on_posted(post_id, deal)
            except Exception:  # noqa: BLE001
                log.exception("on_posted callback failed")

    async def publish(self, deal: ProcessedDeal) -> list[int]:
        """Send the deal to the channel (re-uploading photos — never forwarding)."""
        channel = await self.channel()
        files = [p for p in deal.media_paths if os.path.exists(p)]
        files = files[: self.settings.cfg.posting.max_album_photos]
        for attempt in range(4):
            try:
                if files and deal.fits_caption:
                    sent = await self.bot.send_file(channel, files if len(files) > 1 else files[0],
                                                    caption=deal.html, parse_mode="html")
                else:
                    sent = await self.bot.send_message(channel, deal.html, parse_mode="html",
                                                       link_preview=self.settings.cfg.posting.link_preview)
                msgs = sent if isinstance(sent, list) else [sent]
                return [m.id for m in msgs]
            except FloodWaitError as e:
                log.warning("FloodWait %ss while posting", e.seconds)
                await asyncio.sleep(e.seconds + 1)
        raise RuntimeError("posting failed after repeated FloodWaits")

    async def mark_deal_over(self, post_id: int) -> tuple[bool, str]:
        """Edit a channel post to show the DEAL OVER banner."""
        row = await self.db.get_posted(post_id)
        if not row:
            return False, "post not found"
        if row["deal_over"]:
            return True, "already marked"
        msg_ids = json.loads(row["channel_msg_ids"])
        new_text = render_deal_over(row["body_html"] or "", self.settings.cfg.templates)
        try:
            await self.bot.edit_message(await self.channel(), msg_ids[0], new_text, parse_mode="html")
        except MessageNotModifiedError:
            pass
        except FloodWaitError as e:
            await asyncio.sleep(e.seconds + 1)
            return False, f"flood wait {e.seconds}s, try again"
        except RPCError as e:
            return False, f"Telegram error: {e}"
        await self.db.mark_deal_over(post_id)
        return True, "marked as DEAL OVER"
