"""One-time interactive login for the *reader* (user) account.

Run this once on the server; it asks for your phone number, the code Telegram
sends you, and your 2FA password if you have one. It creates the session file in
DATA_DIR so that main.py can run unattended.

    python login.py
    docker compose run --rm dealbot python login.py
"""
from __future__ import annotations

import asyncio

from telethon import TelegramClient

from config import load_env


async def run() -> None:
    env = load_env()
    if not env.api_id or not env.api_hash:
        raise SystemExit("Set API_ID and API_HASH in .env first (https://my.telegram.org).")
    env.data_dir.mkdir(parents=True, exist_ok=True)
    client = TelegramClient(env.session_path(env.user_session), env.api_id, env.api_hash)
    await client.start(phone=lambda: env.phone or input("Phone number (with +91): "))
    me = await client.get_me()
    print(f"\n✅ Logged in as {me.first_name} (id {me.id}). Session saved in {env.data_dir}/.")
    print("Keep the .session file secret — it is as powerful as your password.")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(run())
