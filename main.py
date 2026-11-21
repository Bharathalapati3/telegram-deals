"""Entry point: wires everything together and runs until SIGINT/SIGTERM.

    python main.py                # run the bot
    python main.py --check        # validate config and exit
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from typing import Optional

from telethon import TelegramClient

from admin import Admin
from config import Settings, load_app_config, load_env, validate_env
from converters import build_converters
from db import Database
from health import Health
from links import Expander
from logging_setup import setup_logging
from models import Action, RawMessage
from pipeline import Pipeline
from poster import Poster
from reader import Reader
from utils import ErrorTracker, cleanup_media, supervise, sweep_media_dir

log = logging.getLogger("main")


class App:
    def __init__(self, config_path: str, env_path: str):
        self.env = load_env(env_path)
        self.cfg = load_app_config(config_path)
        self.stop = asyncio.Event()
        self.tasks: list[asyncio.Task[None]] = []

    async def setup(self) -> None:
        env, cfg = self.env, self.cfg
        env.data_dir.mkdir(parents=True, exist_ok=True)
        env.media_dir.mkdir(parents=True, exist_ok=True)

        self.db = Database(env.db_path, cfg.expansion.cache_ttl_hours, cfg.expansion.failed_cache_ttl_minutes)
        await self.db.connect()
        self.settings = Settings(cfg, env, self.db)
        await self.settings.load_overrides()

        self.user = TelegramClient(env.session_path(env.user_session), env.api_id, env.api_hash,
                                   flood_sleep_threshold=60, connection_retries=-1, retry_delay=5,
                                   auto_reconnect=True)
        self.bot = TelegramClient(env.session_path(env.bot_session), env.api_id, env.api_hash,
                                  flood_sleep_threshold=60, connection_retries=-1, retry_delay=5,
                                  auto_reconnect=True)
        self.bot.parse_mode = "html"
        await self.bot.start(bot_token=env.bot_token)

        self.errors = ErrorTracker(cfg.health.error_alert_threshold, cfg.health.error_alert_window_minutes)
        self.expander = Expander(cfg.expansion, cfg.stores, cache=self.db)
        self.converters = build_converters(env, cfg)
        self.pipeline = Pipeline(self.settings, self.db, self.expander, self.converters)
        self.poster = Poster(self.bot, self.settings, self.db, self.errors)
        self.reader = Reader(self.user, self.settings, self.db, self.handle_message, errors=self.errors)
        self.admin = Admin(self.bot, self.settings, self.db, self.pipeline, self.poster, self.reader, self.errors)
        self.poster.on_posted = self.admin.on_posted
        self.reader.notify = self.admin.notify
        self.errors.notify = self.admin.notify
        self.admin.register()
        self.health = Health(self.settings, self.db, self.reader, self.admin.notify, self.errors)

        try:
            await self.poster.channel()
        except Exception as exc:  # noqa: BLE001
            log.error("Cannot access TARGET_CHANNEL=%s: %r. Is the bot an admin there?",
                      env.target_channel, exc)
            await self.admin.notify(f"⚠️ I cannot access the target channel {env.target_channel}. "
                                    "Add me as an admin with 'Post messages' and 'Edit messages'.")
        sweep_media_dir(env.media_dir, await self.db.referenced_media())

    async def handle_message(self, raw: RawMessage) -> None:
        """Route a pipeline result: queue it, ask for approval, or drop it."""
        result = await self.pipeline.analyze(raw)
        deal = result.deal
        if result.action == Action.AUTO and deal:
            await self.poster.enqueue(deal)
        elif result.action == Action.APPROVAL and deal:
            await self.admin.request_approval(deal)
        elif deal:
            cleanup_media(deal.media_paths)

    async def run(self) -> None:
        await self.setup()
        s = self.stop
        self.tasks = [
            asyncio.create_task(supervise("reader", lambda: self.reader.run(s), s, self.errors)),
            asyncio.create_task(supervise("resolver", lambda: self.reader.resolve_loop(s), s, self.errors)),
            asyncio.create_task(supervise("poster", lambda: self.poster.run(s), s, self.errors)),
            asyncio.create_task(supervise("approvals", lambda: self.admin.expiry_loop(s), s, self.errors)),
            asyncio.create_task(supervise("health", lambda: self.health.run(s), s, self.errors)),
        ]
        log.info("Deal bot started · mode=%s · stores=%s · sources=%d",
                 self.settings.mode, ",".join(self.settings.enabled_stores), len(self.settings.sources))
        await self.admin.notify(f"🤖 Deal bot started · mode <b>{self.settings.mode}</b> · "
                                f"stores: {', '.join(self.settings.enabled_stores)}")
        await self.stop.wait()
        await self.shutdown()

    async def shutdown(self) -> None:
        log.info("Shutting down…")
        try:
            await self.user.disconnect()   # makes run_until_disconnected return
        except Exception:  # noqa: BLE001
            pass
        await self.reader.drain(15)
        for t in self.tasks:
            t.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await self.expander.close()
        await self.converters.close()
        try:
            await self.bot.disconnect()
        except Exception:  # noqa: BLE001
            pass
        await self.db.close()
        log.info("Bye.")

    def request_stop(self, *_: object) -> None:
        log.info("Stop requested")
        self.stop.set()


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Telegram deal watcher → affiliate → poster")
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--env", default=".env")
    p.add_argument("--check", action="store_true", help="validate configuration and exit")
    return p.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    env = load_env(args.env)
    setup_logging(env.log_dir, env.log_level, env.log_format)
    problems = validate_env(env)
    try:
        cfg = load_app_config(args.config)
    except Exception as exc:  # noqa: BLE001
        problems.append(f"config.yaml error: {exc}")
        cfg = None
    if problems:
        for p in problems:
            log.error("CONFIG: %s", p)
        return 2
    if args.check:
        assert cfg is not None
        log.info("Config OK · sources=%d · stores=%s · mode=%s · network=%s",
                 len(cfg.sources), cfg.enabled_stores, cfg.posting.mode, env.affiliate_network)
        return 0

    async def runner() -> None:
        app = App(args.config, args.env)
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, app.request_stop)
            except NotImplementedError:  # Windows
                signal.signal(sig, app.request_stop)
        await app.run()

    try:
        asyncio.run(runner())
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
