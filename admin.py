"""Admin bot: commands, approval previews with inline buttons, edit flow, expiry."""
from __future__ import annotations

import asyncio
import html
import logging
import os
import re
import time
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Optional

from telethon import Button, TelegramClient, events
from telethon.errors import FloodWaitError, MessageNotModifiedError

from config import Settings
from db import Database
from formatter import CAPTION_LIMIT, finalize_custom_text, visible_len
from models import Action, Label, ProcessedDeal, RawMessage
from utils import ErrorTracker, cleanup_media, start_of_day_ts

if TYPE_CHECKING:  # pragma: no cover
    from pipeline import Pipeline
    from poster import Poster
    from reader import Reader

log = logging.getLogger(__name__)

HELP = """<b>Deal bot — admin commands</b>

/status — uptime, posts today, queue, errors
/mode auto|approval|mixed — posting mode
/pause · /resume — stop/start channel posting
/sources · /addsource &lt;@ch|link|id&gt; · /removesource &lt;…&gt;
/stores · /enablestore &lt;store&gt; · /disablestore &lt;store&gt;
/setmin &lt;percent&gt; — minimum discount for normal deals
/block &lt;brand or keyword&gt; · /allow &lt;brand&gt;
/stats — per-source seen / passed / posted
/test &lt;message&gt; — dry-run the pipeline (or reply /test to a message)
/lastskipped — why recent deals were rejected
/scan [count] [hours] — read recent messages already in the sources now
/queue — deals waiting to be posted
/over &lt;post id&gt; — mark a channel post as ❌ DEAL OVER
/cancel — cancel an edit in progress"""


def _short(s: str, n: int = 60) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


class Admin:
    def __init__(self, bot: TelegramClient, settings: Settings, db: Database, pipeline: "Pipeline",
                 poster: "Poster", reader: Optional["Reader"] = None,
                 errors: Optional[ErrorTracker] = None):
        self.bot = bot
        self.settings = settings
        self.db = db
        self.pipeline = pipeline
        self.poster = poster
        self.reader = reader
        self.errors = errors
        self.started_at = time.time()
        self.awaiting_edit: dict[int, int] = {}   # admin_id -> pending id

    @property
    def admin_ids(self) -> list[int]:
        return self.settings.env.admin_ids

    def is_admin(self, user_id: Optional[int]) -> bool:
        return user_id is not None and user_id in self.admin_ids

    # ------------------------------------------------------------------ setup
    def register(self) -> None:
        self.bot.add_event_handler(self._on_message, events.NewMessage(incoming=True,
                                                                       func=lambda e: e.is_private))
        self.bot.add_event_handler(self._on_callback, events.CallbackQuery())

    async def notify(self, text: str) -> None:
        """Send a message to every admin (best effort)."""
        for aid in self.admin_ids:
            try:
                await self.bot.send_message(aid, text, parse_mode="html", link_preview=False)
            except FloodWaitError as e:
                await asyncio.sleep(e.seconds + 1)
            except Exception as exc:  # noqa: BLE001
                log.warning("cannot message admin %s (did they /start the bot?): %r", aid, exc)

    # ------------------------------------------------------------------ approvals
    def _buttons(self, pid: int) -> list[list[Button]]:
        return [
            [Button.inline("✅ Post", f"a:post:{pid}".encode()),
             Button.inline("⚠️ Post as Price Error", f"a:pe:{pid}".encode())],
            [Button.inline("✏️ Edit text", f"a:edit:{pid}".encode()),
             Button.inline("❌ Skip", f"a:skip:{pid}".encode())],
        ]

    def _control_text(self, pid: int, deal: ProcessedDeal, expires_at: float) -> str:
        mins = max(0, int((expires_at - time.time()) / 60))
        reasons = "\n".join(f"• {html.escape(r)}" for r in deal.reasons[:8]) or "—"
        return (f"🕵️ <b>Approval #{pid}</b> · {deal.label.value} · score {deal.score:g}\n"
                f"Source: {html.escape(deal.source_name)} · expires in {mins} min\n"
                f"Links converted: {'yes' if deal.all_converted else '<b>NO</b>'}\n{reasons}")

    async def _send_preview(self, chat_id: int, deal: ProcessedDeal) -> Optional[int]:
        files = [p for p in deal.media_paths if os.path.exists(p)][: self.settings.cfg.posting.max_album_photos]
        try:
            if files and deal.fits_caption:
                sent = await self.bot.send_file(chat_id, files if len(files) > 1 else files[0],
                                                caption=deal.html, parse_mode="html")
            else:
                sent = await self.bot.send_message(chat_id, deal.html, parse_mode="html", link_preview=False)
        except Exception as exc:  # noqa: BLE001
            log.warning("preview to %s failed: %r", chat_id, exc)
            return None
        msgs = sent if isinstance(sent, list) else [sent]
        return msgs[0].id

    async def request_approval(self, deal: ProcessedDeal) -> int:
        ac = self.settings.cfg.approvals
        minutes = ac.price_error_expiry_minutes if deal.label == Label.PRICE_ERROR else ac.expiry_minutes
        expires_at = time.time() + minutes * 60
        pid = await self.db.add_pending(deal, expires_at)
        admin_msgs: list[list[int]] = []
        for aid in self.admin_ids:
            preview_id = await self._send_preview(aid, deal)
            try:
                ctl = await self.bot.send_message(aid, self._control_text(pid, deal, expires_at),
                                                  parse_mode="html", buttons=self._buttons(pid),
                                                  reply_to=preview_id)
                admin_msgs.append([aid, ctl.id])
            except Exception as exc:  # noqa: BLE001
                log.warning("approval message to %s failed: %r", aid, exc)
        await self.db.update_pending(pid, admin_msgs=admin_msgs)
        log.info("approval requested id=%s key=%s", pid, deal.deal_key)
        return pid

    async def _set_control(self, admin_msgs: list[list[int]], text: str) -> None:
        for chat_id, msg_id in admin_msgs:
            try:
                await self.bot.edit_message(chat_id, msg_id, text, parse_mode="html", buttons=None)
            except MessageNotModifiedError:
                pass
            except Exception as exc:  # noqa: BLE001
                log.debug("edit control msg failed: %r", exc)

    async def expiry_loop(self, stop: asyncio.Event) -> None:
        """Expire stale approvals (price errors die fast) and update their buttons."""
        while not stop.is_set():
            try:
                for row in await self.db.expired_pending(time.time()):
                    if await self.db.claim_pending(row["id"], "expired"):
                        await self._set_control(row["admin_msgs"],
                                                f"⌛ <b>Expired</b> — approval #{row['id']} was not posted.\n"
                                                f"{html.escape(_short(row['deal'].title, 80))}")
                        cleanup_media(row["deal"].media_paths)
                        self.awaiting_edit = {a: p for a, p in self.awaiting_edit.items() if p != row["id"]}
            except Exception as exc:  # noqa: BLE001
                log.exception("expiry loop error")
                if self.errors:
                    await self.errors.report("approvals", repr(exc))
            try:
                await asyncio.wait_for(stop.wait(), timeout=30)
            except asyncio.TimeoutError:
                pass

    async def _on_callback(self, event: events.CallbackQuery.Event) -> None:
        if not self.is_admin(event.sender_id):
            await event.answer("Not allowed.", alert=True)
            return
        data = event.data.decode(errors="ignore")
        try:
            if data.startswith("a:"):
                _, action, pid_s = data.split(":")
                await self._handle_approval(event, action, int(pid_s))
            elif data.startswith("o:"):
                ok, msg = await self.poster.mark_deal_over(int(data[2:]))
                await event.answer(msg, alert=not ok)
                if ok:
                    try:
                        await event.edit(buttons=None)
                    except Exception:  # noqa: BLE001
                        pass
        except Exception as exc:  # noqa: BLE001
            log.exception("callback failed")
            await event.answer(f"Error: {exc}", alert=True)

    async def _handle_approval(self, event: Any, action: str, pid: int) -> None:
        row = await self.db.get_pending(pid)
        if not row:
            await event.answer("Not found.", alert=True)
            return
        if row["status"] != "pending":
            await event.answer(f"Already {row['status']}.", alert=True)
            return
        deal: ProcessedDeal = row["deal"]
        who = event.sender_id
        if action == "edit":
            self.awaiting_edit[who] = pid
            await event.answer()
            await self.bot.send_message(
                who, f"✏️ Send the new text for approval #{pid}.\nHTML allowed: &lt;b&gt; &lt;i&gt; "
                     f"&lt;s&gt; &lt;code&gt; &lt;a href&gt;. If you include no link, your affiliate "
                     f"link(s) are added automatically. /cancel to abort.", parse_mode="html")
            return
        if action == "skip":
            if await self.db.claim_pending(pid, "skipped"):
                cleanup_media(deal.media_paths)
                await self._set_control(row["admin_msgs"], f"❌ <b>Skipped</b> — approval #{pid}")
            await event.answer("Skipped")
            return
        if action in ("post", "pe"):
            if not await self.db.claim_pending(pid, "approved"):
                await event.answer("Already handled.", alert=True)
                return
            if action == "pe" and deal.label != Label.PRICE_ERROR:
                deal.label = Label.PRICE_ERROR
                if row.get("custom_text"):
                    header = self.settings.cfg.templates.header_price_error
                    deal.html = f"{header}\n{deal.html}"
                    deal.header_html = header
                    deal.body_html = row["custom_text"]
                else:
                    self.pipeline.render(deal)
            deal.action = Action.AUTO
            await self.poster.enqueue(deal)
            label = "as PRICE ERROR " if action == "pe" else ""
            await self._set_control(row["admin_msgs"], f"✅ <b>Approved</b> {label}— approval #{pid} queued "
                                                       f"for posting.")
            await event.answer("Queued for posting ✅")
            return
        await event.answer("Unknown action", alert=True)

    async def _apply_edit(self, admin_id: int, pid: int, text: str) -> None:
        row = await self.db.get_pending(pid)
        if not row or row["status"] != "pending":
            await self.bot.send_message(admin_id, "That approval is no longer pending.")
            return
        deal: ProcessedDeal = row["deal"]
        cfg = self.settings.cfg
        new_html = finalize_custom_text(text, deal.links, cfg.templates, cfg.stores)
        foreign = self.pipeline.foreign_links(new_html, deal)
        if foreign:
            await self.bot.send_message(
                admin_id, "⛔ Your text contains links/mentions that are not yours:\n"
                          + "\n".join(html.escape(f) for f in foreign[:5])
                          + "\nRemove them (or leave links out — I'll add yours) and send again.",
                parse_mode="html")
            return
        if visible_len(new_html) > 4096:
            await self.bot.send_message(admin_id, "Too long (max 4096 characters). Send a shorter text.")
            return
        deal.html = new_html
        deal.header_html = ""
        deal.body_html = new_html
        deal.fits_caption = visible_len(new_html) <= CAPTION_LIMIT
        await self.db.update_pending(pid, custom_text=new_html, deal=deal)
        self.awaiting_edit.pop(admin_id, None)
        preview_id = await self._send_preview(admin_id, deal)
        ctl = await self.bot.send_message(admin_id, self._control_text(pid, deal, row["expires_at"])
                                          + "\n<i>(edited)</i>", parse_mode="html",
                                          buttons=self._buttons(pid), reply_to=preview_id)
        # retire old buttons, keep the new control message
        await self._set_control(row["admin_msgs"], f"✏️ Edited — see the new preview for #{pid}.")
        await self.db.update_pending(pid, admin_msgs=[[admin_id, ctl.id]])

    async def on_posted(self, post_id: int, deal: ProcessedDeal) -> None:
        if not self.settings.cfg.posting.notify_admin_on_post:
            return
        link = ""
        try:
            ch = await self.poster.channel()
            row = await self.db.get_posted(post_id)
            if getattr(ch, "username", None) and row:
                import json
                mid = json.loads(row["channel_msg_ids"])[0]
                link = f' · <a href="https://t.me/{ch.username}/{mid}">open</a>'
        except Exception:  # noqa: BLE001
            pass
        text = (f"📢 Posted #{post_id} [{deal.label.value}]{link}\n"
                f"{html.escape(_short(deal.title, 90))}")
        for aid in self.admin_ids:
            try:
                await self.bot.send_message(aid, text, parse_mode="html", link_preview=False,
                                            buttons=[[Button.inline("❌ Mark DEAL OVER", f"o:{post_id}".encode())]])
            except Exception as exc:  # noqa: BLE001
                log.debug("post notify failed: %r", exc)

    # ------------------------------------------------------------------ commands
    async def _on_message(self, event: events.NewMessage.Event) -> None:
        if not self.is_admin(event.sender_id):
            if (event.raw_text or "").startswith("/start"):
                await event.reply("This is a private bot.")
            return
        text = event.raw_text or ""
        if not text.startswith("/"):
            pid = self.awaiting_edit.get(event.sender_id)
            if pid:
                # message.text is HTML because bot.parse_mode is set to html
                await self._apply_edit(event.sender_id, pid, event.message.text or "")
            return
        parts = text.split(maxsplit=1)
        cmd = parts[0][1:].split("@")[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""
        handler: Optional[Callable[[Any, str], Awaitable[None]]] = getattr(self, f"cmd_{cmd}", None)
        if handler is None:
            await event.reply("Unknown command. /help")
            return
        try:
            await handler(event, arg)
        except Exception as exc:  # noqa: BLE001
            log.exception("command /%s failed", cmd)
            await event.reply(f"Error: {html.escape(repr(exc))}", parse_mode="html")

    async def cmd_start(self, event: Any, arg: str) -> None:
        await event.reply(HELP, parse_mode="html")

    cmd_help = cmd_start

    async def cmd_cancel(self, event: Any, arg: str) -> None:
        self.awaiting_edit.pop(event.sender_id, None)
        await event.reply("Cancelled.")

    async def cmd_status(self, event: Any, arg: str) -> None:
        up = int(time.time() - self.started_at)
        tz = self.settings.env.timezone
        today = await self.db.count_posts_since(start_of_day_ts(tz))
        seen_today = await self.db.count_seen_since(start_of_day_ts(tz))
        last_read = self.reader.last_message_at if self.reader else 0
        ago = f"{int((time.time() - last_read) / 60)} min ago" if last_read else "never"
        errs = len(self.errors.recent()) if self.errors else 0
        reader_state = self.reader.state if self.reader else "n/a"
        s = self.settings
        await event.reply(
            f"<b>Status</b>\nUptime: {up // 3600}h {(up % 3600) // 60}m\n"
            f"Mode: <b>{s.mode}</b>{' · ⏸ PAUSED' if s.paused else ''}\n"
            f"Reader: {reader_state} · last message {ago}\n"
            f"Messages today: {seen_today} · Posts today: {today}\n"
            f"Queue: {await self.db.count_queued()} · Pending approvals: {await self.db.count_pending()}\n"
            f"Quiet hours now: {'yes' if self.poster.in_quiet_hours() else 'no'}\n"
            f"Errors (24h): {errs}\n"
            f"Stores: {', '.join(s.enabled_stores)} · Min discount: {s.min_discount:g}%\n"
            f"Sources: {len(s.sources)}", parse_mode="html")

    async def cmd_mode(self, event: Any, arg: str) -> None:
        if arg not in {"auto", "approval", "mixed"}:
            await event.reply(f"Current mode: {self.settings.mode}\nUsage: /mode auto|approval|mixed")
            return
        await self.settings.set("mode", arg)
        await event.reply(f"Mode set to <b>{arg}</b>", parse_mode="html")

    async def cmd_pause(self, event: Any, arg: str) -> None:
        await self.settings.set("paused", True)
        await event.reply("⏸ Paused. Deals are still collected and queued but nothing is posted. /resume")

    async def cmd_resume(self, event: Any, arg: str) -> None:
        await self.settings.set("paused", False)
        await event.reply("▶️ Resumed.")

    async def cmd_sources(self, event: Any, arg: str) -> None:
        status = self.reader.sources_status() if self.reader else {}
        lines = []
        for src in self.settings.sources:
            st = status.get(src)
            mark = "✅" if st and st[0] else ("❌" if st else "…")
            info = html.escape(st[1]) if st else "not resolved yet"
            lines.append(f"{mark} {html.escape(src)} — {info}")
        await event.reply("<b>Sources</b>\n" + ("\n".join(lines) or "none — /addsource @channel"),
                          parse_mode="html")

    async def cmd_addsource(self, event: Any, arg: str) -> None:
        if not arg:
            await event.reply("Usage: /addsource @channel | https://t.me/+invite | -100123456")
            return
        await self.settings.add_source(arg)
        msg = "added"
        if self.reader:
            ok, info = await self.reader.add_source(arg)
            msg = f"added and resolved: {info}" if ok else f"saved, but could not resolve yet: {info}"
        await event.reply(f"Source {html.escape(arg)} {html.escape(msg)}", parse_mode="html")

    async def cmd_removesource(self, event: Any, arg: str) -> None:
        if not arg:
            await event.reply("Usage: /removesource @channel")
            return
        await self.settings.remove_source(arg)
        if self.reader:
            await self.reader.refresh_sources()
        await event.reply(f"Removed {html.escape(arg)}", parse_mode="html")

    async def cmd_stores(self, event: Any, arg: str) -> None:
        enabled = set(self.settings.enabled_stores)
        net = self.settings.env.affiliate_network
        lines = [f"{'✅' if k in enabled else '▫️'} <code>{k}</code> — {html.escape(v.name)}"
                 for k, v in self.settings.cfg.stores.items()]
        await event.reply("<b>Stores</b> (✅ = enabled)\n" + "\n".join(lines)
                          + f"\n\nAffiliate network for non-Amazon: <b>{html.escape(net)}</b>\n"
                            "Use /enablestore &lt;key&gt; or /disablestore &lt;key&gt;", parse_mode="html")

    async def cmd_enablestore(self, event: Any, arg: str) -> None:
        key = arg.strip().lower()
        try:
            await self.settings.enable_store(key)
        except ValueError as exc:
            await event.reply(str(exc))
            return
        warn = ""
        if key != "amazon" and self.settings.env.affiliate_network == "none":
            warn = ("\n⚠️ No AFFILIATE_NETWORK configured: links for this store cannot be converted, "
                    f"so conversion.on_fail='{self.settings.cfg.conversion.on_fail}' applies.")
        await event.reply(f"Enabled {key}. Now: {', '.join(self.settings.enabled_stores)}{warn}")

    async def cmd_disablestore(self, event: Any, arg: str) -> None:
        await self.settings.disable_store(arg.strip().lower())
        await event.reply(f"Now enabled: {', '.join(self.settings.enabled_stores) or 'none'}")

    async def cmd_setmin(self, event: Any, arg: str) -> None:
        try:
            v = float(arg.rstrip("%"))
            assert 0 <= v <= 100
        except (ValueError, AssertionError):
            await event.reply(f"Current: {self.settings.min_discount:g}%. Usage: /setmin 60")
            return
        await self.settings.set("min_discount", v)
        await event.reply(f"Minimum discount set to {v:g}% (category thresholds in config still apply).")

    async def cmd_block(self, event: Any, arg: str) -> None:
        if not arg:
            await event.reply("Usage: /block <brand or keyword>")
            return
        await self.settings.block(arg)
        await event.reply(f"Blocked: {html.escape(arg.lower())}", parse_mode="html")

    async def cmd_allow(self, event: Any, arg: str) -> None:
        if not arg:
            fc = self.settings.filter_config()
            await event.reply("Allowed brands: " + (", ".join(fc.brand_allowlist) or "none"))
            return
        await self.settings.allow(arg)
        await event.reply(f"Allowed brand: {html.escape(arg.lower())}", parse_mode="html")

    async def cmd_stats(self, event: Any, arg: str) -> None:
        rows = await self.db.get_stats()
        if not rows:
            await event.reply("No stats yet.")
            return
        lines = ["<b>Source · seen / passed / posted</b>"]
        for r in rows:
            seen = r["seen"] or 0
            pr = f"{100 * (r['posted'] or 0) / seen:.0f}%" if seen else "-"
            lines.append(f"{html.escape(_short(r['source_name'] or str(r['source_id']), 30))}: "
                         f"{seen} / {r['passed']} / {r['posted']} ({pr} posted)")
        await event.reply("\n".join(lines), parse_mode="html")

    async def cmd_lastskipped(self, event: Any, arg: str) -> None:
        rows = await self.db.last_skipped(10)
        if not rows:
            await event.reply("Nothing skipped yet.")
            return
        lines = ["<b>Recently skipped</b>"]
        for r in rows:
            title = _short(r["title"] or f"msg {r['message_id']}", 40)
            lines.append(f"• {html.escape(r['source_name'] or '')}: {html.escape(title)}\n"
                         f"  ↳ {html.escape('; '.join(r['reasons'])[:200])}")
        await event.reply("\n".join(lines), parse_mode="html", link_preview=False)

    async def cmd_scan(self, event: Any, arg: str) -> None:
        """/scan [messages per source, default 20] [max age in hours, default 12]"""
        if not self.reader or self.reader.state != "running":
            state = self.reader.state if self.reader else "n/a"
            await event.reply(f"Reader is not running ({state}). Check /status.")
            return
        nums = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", arg)]
        per_source = int(min(max(nums[0], 1), 100)) if nums else 20
        hours = min(max(nums[1], 0.5), 72) if len(nums) > 1 else 12
        await event.reply(f"🔎 Scanning the last {per_source} messages (up to {hours:g}h old) in "
                          f"{len(self.settings.sources)} source(s)… this can take a minute.")
        started = time.time()
        counts = await self.reader.backfill(per_source, hours)
        actions = await self.db.count_actions_since(started)
        checked = sum(counts.values())
        per = "\n".join(f"• {html.escape(_short(n, 35))}: {c}" for n, c in counts.items()) or "• no sources resolved"
        notes = []
        if self.settings.paused:
            notes.append("⏸ Posting is paused — /resume")
        if self.poster.in_quiet_hours():
            notes.append("🌙 Quiet hours: normal deals wait in the queue until they end")
        pc = self.settings.cfg.posting
        await event.reply(
            f"✅ <b>Scan done</b> — {checked} new message(s) checked\n{per}\n\n"
            f"Queued to post: <b>{actions.get('auto', 0)}</b>\n"
            f"Sent to you for approval: <b>{actions.get('approval', 0)}</b>\n"
            f"Skipped: {actions.get('skip', 0)} (see /lastskipped)\n\n"
            f"Posts go out one at a time, at least {pc.min_gap_seconds // 60} min apart, max "
            f"{pc.max_posts_per_hour}/hour (see /queue)."
            + ("\n" + "\n".join(notes) if notes else ""), parse_mode="html")

    async def cmd_queue(self, event: Any, arg: str) -> None:
        items = await self.db.queued_items()
        if not items:
            await event.reply("Queue is empty.")
            return
        lines = [f"• [{i['deal'].label.value}] {html.escape(_short(i['deal'].title, 50))}" for i in items[:15]]
        await event.reply("<b>Queued</b>\n" + "\n".join(lines), parse_mode="html")

    async def cmd_over(self, event: Any, arg: str) -> None:
        m = re.search(r"(\d+)\s*$", arg)
        if not m:
            rows = await self.db.recent_posts(5)
            listing = "\n".join(f"#{r['id']} {html.escape(_short(r['title'] or '', 50))}" for r in rows)
            await event.reply(f"Usage: /over &lt;post id or channel message link&gt;\nRecent:\n{listing}",
                              parse_mode="html")
            return
        num = int(m.group(1))
        post_id = num
        if "t.me/" in arg:
            row = await self.db.get_posted_by_channel_msg(num)
            post_id = row["id"] if row else -1
        ok, msg = await self.poster.mark_deal_over(post_id)
        await event.reply(("✅ " if ok else "❌ ") + msg)

    async def cmd_test(self, event: Any, arg: str) -> None:
        from reader import urls_from_message, strike_texts_from_message
        msg = event.message
        if event.is_reply and not arg:
            msg = await event.get_reply_message()
            text = msg.raw_text or ""
        else:
            text = arg
        if not text.strip():
            await event.reply("Usage: /test <paste a deal message> (or reply /test to a message)")
            return
        raw = RawMessage(source_id=0, source_name="test", message_id=msg.id, text=text,
                         urls=urls_from_message(msg), strike_texts=strike_texts_from_message(msg))
        res = await self.pipeline.analyze(raw, dry_run=True)
        links = "\n".join(f"• {html.escape(_short(r.original, 50))} → "
                          f"{html.escape(r.store or 'unknown')} {html.escape(r.product_id or '')}"
                          f"{'' if r.resolved else ' (unresolved)'}" for r in res.resolved_links) or "none"
        d = res.deal
        facts = ""
        if d:
            i = d.info
            facts = (f"\nTitle: {html.escape(d.title)}\nPrice: {i.price} · MRP: {i.mrp} · "
                     f"Discount: {i.discount} ({i.discount_source}) · Coupon: {i.coupon}")
        await event.reply(
            f"🧪 <b>Dry run</b> → action <b>{res.action.value}</b> · {res.label.value} · score {res.score:g}"
            f"{facts}\nLinks:\n{links}\nReasons:\n"
            + "\n".join(f"• {html.escape(r)}" for r in res.reasons), parse_mode="html", link_preview=False)
        if d and d.html:
            await event.reply(d.html, parse_mode="html", link_preview=False)
