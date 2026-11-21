"""Queue/rate-limit/quiet-hours behaviour with a fake Telegram bot."""
import time
from types import SimpleNamespace

import pytest

from poster import Poster, parse_peer
from tests.test_dedupe import make_deal
from models import Label


class FakeBot:
    def __init__(self):
        self.sent = []
        self.edits = []
        self._id = 100

    async def get_entity(self, peer):
        return SimpleNamespace(id=1, username="mychannel")

    async def send_message(self, chat, text, **kw):
        self._id += 1
        self.sent.append(text)
        return SimpleNamespace(id=self._id)

    async def send_file(self, chat, files, caption=None, **kw):
        self._id += 1
        self.sent.append(caption)
        return SimpleNamespace(id=self._id)

    async def edit_message(self, chat, msg_id, text, **kw):
        self.edits.append((msg_id, text))


@pytest.fixture
def poster(settings, db):
    settings.cfg.posting.quiet_hours_enabled = False
    return Poster(FakeBot(), settings, db)


def deal(key, label=Label.NORMAL):
    d = make_deal(key=key, title=f"Product {key}")
    d.label = label
    d.html = f"post {key}"
    d.body_html = f"body {key}"
    return d


async def test_posts_and_respects_min_gap(poster, db):
    await poster.enqueue(deal("amazon:A1"))
    await poster.enqueue(deal("amazon:A2"))
    await poster._tick()
    await poster._tick()
    assert poster.bot.sent == ["post amazon:A1"]  # second blocked by min gap
    assert await db.count_queued() == 1


async def test_price_error_jumps_queue_and_bypasses_gap(poster):
    await poster.enqueue(deal("amazon:N1"))
    await poster._tick()
    await poster.enqueue(deal("amazon:N2"))
    await poster.enqueue(deal("amazon:PE", Label.PRICE_ERROR))
    await poster._tick()
    assert poster.bot.sent == ["post amazon:N1", "post amazon:PE"]


async def test_hourly_cap(poster, settings):
    settings.cfg.posting.min_gap_seconds = 0
    settings.cfg.posting.max_posts_per_hour = 2
    for i in range(4):
        await poster.enqueue(deal(f"amazon:H{i}"))
    for _ in range(4):
        await poster._tick()
    assert len(poster.bot.sent) == 2


async def test_quiet_hours_hold_normal_but_allow_price_errors(poster, settings):
    settings.cfg.posting.quiet_hours_enabled = True
    settings.cfg.posting.quiet_hours_start = "00:00"
    settings.cfg.posting.quiet_hours_end = "23:59"
    await poster.enqueue(deal("amazon:Q1"))
    await poster._tick()
    assert poster.bot.sent == []
    await poster.enqueue(deal("amazon:QPE", Label.PRICE_ERROR))
    await poster._tick()
    assert poster.bot.sent == ["post amazon:QPE"]


async def test_paused_posts_nothing(poster, settings):
    await settings.set("paused", True)
    await poster.enqueue(deal("amazon:P1"))
    await poster._tick()
    assert poster.bot.sent == []


async def test_expired_items_dropped(poster, db):
    poster.clock = lambda: time.time()
    await poster.enqueue(deal("amazon:E1"))
    poster.clock = lambda: time.time() + 10 * 3600
    await poster._tick()
    assert poster.bot.sent == [] and await db.count_queued() == 0


async def test_queue_survives_restart(settings, db):
    p1 = Poster(FakeBot(), settings, db)
    settings.cfg.posting.quiet_hours_enabled = False
    await p1.enqueue(deal("amazon:R1"))
    p2 = Poster(FakeBot(), settings, db)   # "restart": new poster, same DB
    await p2._tick()
    assert p2.bot.sent == ["post amazon:R1"]


async def test_deal_over_edit(poster, db):
    await poster.enqueue(deal("amazon:D1"))
    await poster._tick()
    post_id = (await db.recent_posts(1))[0]["id"]
    ok, _ = await poster.mark_deal_over(post_id)
    assert ok and poster.bot.edits[0][1].startswith("❌ <b>DEAL OVER</b>")
    assert "body amazon:D1" in poster.bot.edits[0][1]


def test_parse_peer():
    assert parse_peer("@mychannel") == "@mychannel"
    assert parse_peer("mychannel") == "@mychannel"
    assert parse_peer("https://t.me/mychannel") == "@mychannel"
    assert parse_peer("-1001234567890") == -1001234567890
