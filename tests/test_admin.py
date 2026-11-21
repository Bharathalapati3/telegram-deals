"""Approval flow with fake Telegram objects."""
import time
import pytest

from admin import Admin
from converters import build_converters
from links import Expander
from models import Label
from pipeline import Pipeline
from poster import Poster
from tests.conftest import FakeSession
from tests.test_poster import FakeBot, deal


class Bot(FakeBot):
    def __init__(self):
        super().__init__()
        self.ctl_edits = []

    async def send_message(self, chat, text, **kw):
        m = await super().send_message(chat, text, **kw)
        return m

    async def edit_message(self, chat, msg_id, text, **kw):
        self.ctl_edits.append(text)


class Event:
    def __init__(self, sender, data=b""):
        self.sender_id = sender
        self.data = data
        self.answers = []

    async def answer(self, text="", alert=False):
        self.answers.append(text)

    async def edit(self, **kw):
        pass


@pytest.fixture
def admin(settings, db):
    bot = Bot()
    ex = Expander(settings.cfg.expansion, settings.cfg.stores, session=FakeSession({}))
    pipe = Pipeline(settings, db, ex, build_converters(settings.env, settings.cfg))
    poster = Poster(bot, settings, db)
    return Admin(bot, settings, db, pipe, poster)


async def test_post_button_queues_once(admin, db):
    d = deal("amazon:AP1")
    pid = await admin.request_approval(d)
    ev = Event(1, f"a:post:{pid}".encode())
    await admin._on_callback(ev)
    assert await db.count_queued() == 1
    ev2 = Event(1, f"a:post:{pid}".encode())
    await admin._on_callback(ev2)
    assert await db.count_queued() == 1 and "Already" in ev2.answers[0]


async def test_non_admin_rejected(admin, db):
    pid = await admin.request_approval(deal("amazon:AP2"))
    ev = Event(999, f"a:post:{pid}".encode())
    await admin._on_callback(ev)
    assert ev.answers == ["Not allowed."] and await db.count_queued() == 0


async def test_post_as_price_error_relabels(admin, db):
    pid = await admin.request_approval(deal("amazon:AP3"))
    await admin._on_callback(Event(1, f"a:pe:{pid}".encode()))
    q = await db.queued_items()
    assert q[0]["deal"].label == Label.PRICE_ERROR and "PRICE ERROR" in q[0]["deal"].html


async def test_edit_flow_rejects_foreign_links_and_accepts_own(admin, db):
    from models import ConvertedLink
    d = deal("amazon:AP4")
    my = "https://www.amazon.in/dp/B0BZ8TRMK6?tag=mytag-21"
    d.links = [ConvertedLink("amazon", my, True)]
    pid = await admin.request_approval(d)
    await admin._on_callback(Event(1, f"a:edit:{pid}".encode()))
    assert admin.awaiting_edit[1] == pid
    await admin._apply_edit(1, pid, "Buy here https://amzn.to/thief")
    assert "not yours" in admin.bot.sent[-1]
    await admin._apply_edit(1, pid, "<b>Superb earbuds</b>")
    row = await db.get_pending(pid)
    assert my in row["custom_text"] and "#ad" in row["custom_text"]
    assert 1 not in admin.awaiting_edit


async def test_skip_and_expiry(admin, db):
    pid = await admin.request_approval(deal("amazon:AP5"))
    await admin._on_callback(Event(1, f"a:skip:{pid}".encode()))
    assert (await db.get_pending(pid))["status"] == "skipped"
    pid2 = await admin.request_approval(deal("amazon:AP6"))
    await db._exec("UPDATE pending_approvals SET expires_at=? WHERE id=?", (time.time() - 1, pid2))
    import asyncio
    stop = asyncio.Event()
    task = asyncio.create_task(admin.expiry_loop(stop))
    await asyncio.sleep(0.05)
    stop.set()
    await task
    assert (await db.get_pending(pid2))["status"] == "expired"
    assert any("Expired" in t for t in admin.bot.ctl_edits)
