"""One-time interactive login for the *reader* (user) account.

Run this once on the server. It asks for the phone number, tells you WHERE
Telegram sent the login code (Telegram app, SMS, phone call or email), lets you
request the code again by another method, and asks for your 2-step password if
you have one. It creates the session file in DATA_DIR so main.py can run unattended.

    python login.py
    docker compose run --rm dealbot python login.py
"""
from __future__ import annotations

import asyncio
import getpass
import re

from telethon import TelegramClient
from telethon.errors import (ApiIdInvalidError, FloodWaitError, PhoneCodeExpiredError,
                             PhoneCodeInvalidError, PhoneNumberBannedError, PhoneNumberInvalidError,
                             PasswordHashInvalidError, SendCodeUnavailableError,
                             SessionPasswordNeededError)
from telethon.tl import types
from telethon.tl.functions.auth import ResendCodeRequest

from config import load_env


def describe(code_type: object) -> str:
    """Explain in plain English where Telegram delivered the code."""
    if isinstance(code_type, types.auth.SentCodeTypeApp):
        return ("inside the TELEGRAM APP, on a device where this account is already logged in. "
                "Open the chat named 'Telegram' (blue tick).")
    if isinstance(code_type, (types.auth.SentCodeTypeSms, types.auth.SentCodeTypeFirebaseSms)):
        return "by SMS to the phone number."
    if isinstance(code_type, types.auth.SentCodeTypeFragmentSms):
        return "via Fragment (anonymous number) — check fragment.com."
    if isinstance(code_type, types.auth.SentCodeTypeCall):
        return "by a PHONE CALL — a voice will read the code."
    if isinstance(code_type, types.auth.SentCodeTypeFlashCall):
        return "by a missed call — the code is the LAST digits of the calling number."
    if isinstance(code_type, types.auth.SentCodeTypeMissedCall):
        prefix = getattr(code_type, "prefix", "")
        return f"by a missed call from a number starting {prefix} — the code is its last digits."
    if isinstance(code_type, types.auth.SentCodeTypeEmailCode):
        pattern = getattr(code_type, "email_pattern", "your login email")
        return f"to your LOGIN EMAIL ({pattern}). Check inbox and spam."
    if isinstance(code_type, types.auth.SentCodeTypeSetUpEmailRequired):
        return ("NOWHERE yet: Telegram requires a login email for this account first. Log in once "
                "with the official Telegram app on a phone, set a login email when asked, then retry.")
    return f"by an unknown method ({type(code_type).__name__})."


def describe_next(next_type: object) -> str:
    if next_type is None:
        return ""
    if isinstance(next_type, types.auth.CodeTypeSms):
        return "SMS"
    if isinstance(next_type, types.auth.CodeTypeCall):
        return "a phone call"
    if isinstance(next_type, (types.auth.CodeTypeFlashCall, types.auth.CodeTypeMissedCall)):
        return "a missed call"
    if isinstance(next_type, types.auth.CodeTypeFragmentSms):
        return "Fragment SMS"
    return type(next_type).__name__


def show(sent: object) -> None:
    print("\n📨 Telegram sent the login code " + describe(sent.type))
    nxt = describe_next(getattr(sent, "next_type", None))
    timeout = getattr(sent, "timeout", None)
    if nxt:
        wait = f" (available in about {timeout}s)" if timeout else ""
        print(f"   Didn't get it? Type  r  to resend it by {nxt}{wait}.")
    else:
        print("   (Telegram offers no other delivery method for this account right now.)")


async def run() -> None:
    env = load_env()
    if not env.api_id or not env.api_hash:
        raise SystemExit("Set API_ID and API_HASH in .env first (https://my.telegram.org).")
    env.data_dir.mkdir(parents=True, exist_ok=True)
    client = TelegramClient(env.session_path(env.user_session), env.api_id, env.api_hash)
    await client.connect()
    try:
        if await client.is_user_authorized():
            me = await client.get_me()
            print(f"Already logged in as {me.first_name} (id {me.id}). Nothing to do.")
            return

        phone = env.phone or input("Phone number of the READER account (e.g. +919876543210): ")
        phone = re.sub(r"[\s\-()]", "", phone)
        if not phone.startswith("+"):
            print("⚠️  Number has no country code — assuming India (+91).")
            phone = "+91" + phone.lstrip("0")
        print(f"Requesting code for {phone} …")

        try:
            sent = await client.send_code_request(phone)
        except ApiIdInvalidError:
            raise SystemExit("❌ API_ID / API_HASH are wrong. Copy them again from my.telegram.org.")
        except PhoneNumberInvalidError:
            raise SystemExit("❌ Telegram says this phone number is invalid. Check the country code.")
        except PhoneNumberBannedError:
            raise SystemExit("❌ This phone number is banned by Telegram.")
        except FloodWaitError as e:
            raise SystemExit(f"❌ Too many attempts. Telegram asks you to wait {e.seconds // 60} min "
                             f"({e.seconds}s) before trying again. Don't retry before that.")
        show(sent)
        if isinstance(sent.type, types.auth.SentCodeTypeSetUpEmailRequired):
            return

        while True:
            code = input("\nEnter the code (or r to resend): ").strip().replace(" ", "")
            if code.lower() == "r":
                try:
                    sent = await client(ResendCodeRequest(phone, sent.phone_code_hash))
                    show(sent)
                except SendCodeUnavailableError:
                    print("Telegram has no other delivery method left. Use the last code you received, "
                          "or wait a few hours and run login.py again.")
                except FloodWaitError as e:
                    print(f"Too many resends. Wait {e.seconds // 60} min, then run login.py again.")
                    return
                except Exception as e:  # noqa: BLE001
                    print(f"Resend not possible yet: {e}. Wait a minute and type r again.")
                continue
            if not code.isdigit():
                print("The code is digits only.")
                continue
            try:
                await client.sign_in(phone, code, phone_code_hash=sent.phone_code_hash)
                break
            except PhoneCodeInvalidError:
                print("❌ Wrong code, try again.")
            except PhoneCodeExpiredError:
                print("❌ Code expired. Type r to get a new one.")
            except SessionPasswordNeededError:
                for _ in range(3):
                    pw = getpass.getpass("🔒 2-step verification password: ")
                    try:
                        await client.sign_in(password=pw)
                        break
                    except PasswordHashInvalidError:
                        print("❌ Wrong password.")
                else:
                    raise SystemExit("Too many wrong passwords.")
                break

        me = await client.get_me()
        print(f"\n✅ Logged in as {me.first_name} (id {me.id}). Session saved in {env.data_dir}/.")
        print("Keep the .session file secret — it is as powerful as your password.")
    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(run())
