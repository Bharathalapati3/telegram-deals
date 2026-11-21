"""Reader: the *user* Telethon client that listens to source channels."""
from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from telethon import TelegramClient, events, utils
from telethon.errors import (AuthKeyUnregisteredError, FloodWaitError, InviteHashExpiredError,
                             InviteHashInvalidError, SessionRevokedError, UserAlreadyParticipantError,
                             UserDeactivatedError)
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.messages import CheckChatInviteRequest, ImportChatInviteRequest
from telethon.tl.types import (ChatInvite, ChatInviteAlready, ChatInvitePeek, MessageEntityStrike,
                               MessageEntityTextUrl, MessageEntityUrl)

from config import Settings
from db import Database
from models import RawMessage
from utils import ErrorTracker

log = logging.getLogger(__name__)

INVITE_RE = re.compile(r"(?:t\.me|telegram\.me)/(?:\+|joinchat/)([\w-]+)", re.I)


class NotLoggedIn(Exception):
    """The user session file is missing or not authorised."""


AUTH_ERRORS = (AuthKeyUnregisteredError, SessionRevokedError, UserDeactivatedError, NotLoggedIn)

OnMessage = Callable[[RawMessage], Awaitable[None]]
Notify = Callable[[str], Awaitable[None]]


# --------------------------------------------------------------------------- message helpers

def urls_from_message(msg: Any) -> list[str]:
    """URLs hidden in entities (text links) and inline URL buttons."""
    urls: list[str] = []
    if msg is None:
        return urls
    try:
        for ent, txt in msg.get_entities_text():
            if isinstance(ent, MessageEntityTextUrl):
                urls.append(ent.url)
            elif isinstance(ent, MessageEntityUrl):
                urls.append(txt)
    except Exception:  # noqa: BLE001
        pass
    for row in getattr(msg, "buttons", None) or []:
        for btn in row:
            url = getattr(btn, "url", None)
            if url:
                urls.append(url)
    return urls


def strike_texts_from_message(msg: Any) -> list[str]:
    try:
        return [txt for ent, txt in msg.get_entities_text() if isinstance(ent, MessageEntityStrike)]
    except Exception:  # noqa: BLE001
        return []


def _is_image(msg: Any) -> bool:
    if getattr(msg, "photo", None):
        return True
    doc = getattr(msg, "document", None)
    return bool(doc and (doc.mime_type or "").startswith("image/") and not getattr(msg, "sticker", None))


class Reader:
    def __init__(self, client: TelegramClient, settings: Settings, db: Database, on_message: OnMessage,
                 notify: Optional[Notify] = None, errors: Optional[ErrorTracker] = None):
        self.client = client
        self.settings = settings
        self.db = db
        self.on_message = on_message
        self.notify = notify
        self.errors = errors
        self.state = "starting"
        self.last_message_at = 0.0
        self._chat_ids: dict[int, str] = {}
        self._status: dict[str, tuple[bool, str]] = {}
        self._sem = asyncio.Semaphore(settings.cfg.reader.max_concurrent)
        self._tasks: set[asyncio.Task[None]] = set()
        self._handlers_added = False

    # ------------------------------------------------------------------ sources
    def sources_status(self) -> dict[str, tuple[bool, str]]:
        return dict(self._status)

    async def _resolve(self, src: str) -> Any:
        auto_join = self.settings.cfg.reader.auto_join
        m = INVITE_RE.search(src)
        if m:
            h = m.group(1)
            invite = await self.client(CheckChatInviteRequest(h))
            if isinstance(invite, (ChatInviteAlready, ChatInvitePeek)):
                return invite.chat
            if isinstance(invite, ChatInvite):
                if not auto_join:
                    raise ValueError("not a member and reader.auto_join is false")
                try:
                    upd = await self.client(ImportChatInviteRequest(h))
                    return upd.chats[0]
                except UserAlreadyParticipantError:
                    pass
            raise ValueError("could not use invite link")
        s = src.strip()
        try:
            return await self.client.get_entity(int(s))
        except ValueError:
            pass
        name = re.sub(r"^(?:https?://)?(?:t\.me|telegram\.me)/", "", s).lstrip("@").split("/")[0]
        entity = await self.client.get_entity(name)
        if auto_join and getattr(entity, "left", False):
            await self.client(JoinChannelRequest(entity))
            log.info("joined source channel %s", name)
        return entity

    async def refresh_sources(self) -> None:
        """(Re-)resolve all configured sources to chat ids."""
        ids: dict[int, str] = {}
        status: dict[str, tuple[bool, str]] = {}
        sources = self.settings.sources
        if any(re.fullmatch(r"-?\d+", s.strip()) for s in sources):
            try:
                await self.client.get_dialogs(limit=None)   # fills the entity cache for numeric ids
            except Exception as exc:  # noqa: BLE001
                log.warning("get_dialogs failed: %r", exc)
        for src in sources:
            try:
                entity = await self._resolve(src)
                pid = utils.get_peer_id(entity)
                ids[pid] = getattr(entity, "title", None) or src
                status[src] = (True, f"{ids[pid]} ({pid})")
            except FloodWaitError as e:
                log.warning("FloodWait %ss resolving %s", e.seconds, src)
                await asyncio.sleep(e.seconds + 1)
                status[src] = (False, f"flood wait {e.seconds}s — will retry")
            except (InviteHashExpiredError, InviteHashInvalidError):
                status[src] = (False, "invite link expired/invalid")
            except Exception as exc:  # noqa: BLE001
                status[src] = (False, f"{type(exc).__name__}: {exc}")
                log.warning("cannot resolve source %s: %r", src, exc)
        self._chat_ids, self._status = ids, status
        log.info("watching %d/%d sources", len(ids), len(sources))

    async def add_source(self, src: str) -> tuple[bool, str]:
        await self.refresh_sources()
        return self._status.get(src, (False, "unknown"))

    # ------------------------------------------------------------------ handlers
    def _register(self) -> None:
        if self._handlers_added:
            return
        is_source: Callable[[Any], bool] = lambda e: e.chat_id in self._chat_ids  # noqa: E731
        self.client.add_event_handler(self._on_new, events.NewMessage(func=is_source))
        self.client.add_event_handler(self._on_album, events.Album(func=is_source))
        if self.settings.cfg.reader.process_edits:
            self.client.add_event_handler(self._on_edit, events.MessageEdited(func=is_source))
        self._handlers_added = True

    def _media_loader(self, msgs: list[Any]) -> Optional[Callable[[], Awaitable[list[str]]]]:
        imgs = [m for m in msgs if _is_image(m)][: self.settings.cfg.posting.max_album_photos]
        if not imgs:
            return None
        media_dir: Path = self.settings.env.media_dir

        async def load() -> list[str]:
            media_dir.mkdir(parents=True, exist_ok=True)
            paths: list[str] = []
            for m in imgs:
                for _ in range(3):
                    try:
                        p = await self.client.download_media(m, file=str(media_dir / uuid.uuid4().hex))
                        if p:
                            paths.append(str(p))
                        break
                    except FloodWaitError as e:
                        await asyncio.sleep(e.seconds + 1)
            return paths

        return load

    def _raw(self, msgs: list[Any], is_edit: bool = False) -> RawMessage:
        main = next((m for m in msgs if m.raw_text), msgs[0])
        urls: list[str] = []
        for m in msgs:
            urls += urls_from_message(m)
        chat_id = main.chat_id
        return RawMessage(
            source_id=chat_id, source_name=self._chat_ids.get(chat_id, str(chat_id)),
            message_id=main.id, text=main.raw_text or "", urls=urls,
            strike_texts=strike_texts_from_message(main), is_forward=bool(main.fwd_from),
            is_edit=is_edit, has_media=any(_is_image(m) for m in msgs),
            media_loader=self._media_loader(msgs))

    def _spawn(self, raw: RawMessage) -> None:
        async def run() -> None:
            async with self._sem:
                try:
                    await self.db.bump_stat(raw.source_id, raw.source_name, "seen")
                    await self.on_message(raw)
                except Exception as exc:  # noqa: BLE001
                    log.exception("handling message failed")
                    if self.errors:
                        await self.errors.report("reader", repr(exc))
        task = asyncio.create_task(run())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _on_new(self, event: events.NewMessage.Event) -> None:
        self.last_message_at = time.time()
        if event.message.grouped_id:
            return  # handled once by the Album event
        if not (event.message.raw_text or event.message.buttons):
            return
        self._spawn(self._raw([event.message]))

    async def _on_album(self, event: events.Album.Event) -> None:
        self.last_message_at = time.time()
        self._spawn(self._raw(list(event.messages)))

    async def _on_edit(self, event: events.MessageEdited.Event) -> None:
        msg = event.message
        if not msg.raw_text:
            return
        prev = await self.db.was_handled(msg.chat_id, msg.id)
        if prev in ("auto", "approval"):
            return  # already posted/queued — ignore edits
        self._spawn(self._raw([msg], is_edit=True))

    # ------------------------------------------------------------------ run loop
    async def run(self, stop: asyncio.Event) -> None:
        """Connect, watch sources and keep reconnecting. Returns only when ``stop`` is set."""
        self._register()
        backoff = 5.0
        while not stop.is_set():
            try:
                if not self.client.is_connected():
                    await self.client.connect()
                if not await self.client.is_user_authorized():
                    raise NotLoggedIn()
                await self.refresh_sources()
                if self.settings.cfg.reader.catch_up:
                    await self.client.catch_up()
                self.state = "running"
                backoff = 5.0
                await self.client.run_until_disconnected()
                if stop.is_set():
                    break
                self.state = "disconnected — reconnecting"
                log.warning("user client disconnected; reconnecting")
            except AUTH_ERRORS as exc:
                self.state = "NOT LOGGED IN — run login.py"
                log.error("user session invalid: %r", exc)
                if self.notify:
                    await self.notify("🔑 The reader account session is invalid or expired. "
                                      "Run <code>python login.py</code> on the server, then restart.")
                await self._sleep(stop, 1800)
                continue
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.state = f"error: {type(exc).__name__}"
                log.exception("reader error")
                if self.errors:
                    await self.errors.report("reader", repr(exc))
            await self._sleep(stop, backoff)
            backoff = min(backoff * 2, 300)

    @staticmethod
    async def _sleep(stop: asyncio.Event, seconds: float) -> None:
        try:
            await asyncio.wait_for(stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    async def resolve_loop(self, stop: asyncio.Event, every_minutes: float = 30) -> None:
        """Periodically retry sources that failed to resolve."""
        while not stop.is_set():
            await self._sleep(stop, every_minutes * 60)
            if self.state == "running" and any(not ok for ok, _ in self._status.values()):
                await self.refresh_sources()

    async def drain(self, timeout: float = 20) -> None:
        if self._tasks:
            await asyncio.wait(self._tasks, timeout=timeout)
