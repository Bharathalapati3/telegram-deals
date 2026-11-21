"""End-to-end pipeline tests with mocked HTTP (no network, no Telegram)."""
import pytest

from converters import build_converters
from formatter import urls_in_post
from links import Expander
from models import Action, Label, RawMessage
from pipeline import Pipeline
from tests.conftest import FakeSession, redirect

ASIN = "B0BZ8TRMK6"
ROUTES = {
    "https://amzn.to/thief": redirect(f"https://www.amazon.in/boAt-Airdopes/dp/{ASIN}?tag=thief-21&ascsubtag=x"),
    "https://fkrt.it/fk1": redirect("https://www.flipkart.com/x/p/itm1?pid=ABC&affid=thief"),
    "https://bit.ly/dead": redirect("https://bit.ly/dead2"),
}


@pytest.fixture
def pipeline(settings, db):
    ex = Expander(settings.cfg.expansion, settings.cfg.stores, cache=db, session=FakeSession(ROUTES))
    return Pipeline(settings, db, ex, build_converters(settings.env, settings.cfg))


def msg(text, mid=1, **kw):
    return RawMessage(source_id=-1001, source_name="src", message_id=mid, text=text, **kw)


DEAL = ("🔥 LOOT 🔥\nboAt Airdopes 141 TWS Earbuds @ ₹799\nMRP ₹4,490\nUse code BOAT10\n"
        "https://amzn.to/thief\nJoin @thiefdeals for more")


async def test_amazon_deal_gets_my_tag_and_no_source_branding(pipeline):
    res = await pipeline.analyze(msg(DEAL))
    assert res.deal and res.label == Label.HOT_DEAL
    html = res.deal.html
    assert f"https://www.amazon.in/dp/{ASIN}?tag=mytag-21" in html
    assert "thief" not in html.lower()
    assert urls_in_post(html) == [f"https://www.amazon.in/dp/{ASIN}?tag=mytag-21"]
    assert "BOAT10" in html


async def test_mixed_mode_needs_allowlist(pipeline, settings):
    res = await pipeline.analyze(msg(DEAL))
    assert res.action == Action.APPROVAL   # brand allowlist empty → approval
    settings.cfg.filters.brand_allowlist = ["boat"]
    res2 = await pipeline.analyze(msg(DEAL.replace("@ ₹799", "@ ₹749"), mid=2))
    assert res2.action == Action.AUTO


async def test_auto_and_approval_modes(pipeline, settings):
    await settings.set("mode", "auto")
    assert (await pipeline.analyze(msg(DEAL), dry_run=True)).action == Action.AUTO
    await settings.set("mode", "approval")
    assert (await pipeline.analyze(msg(DEAL), dry_run=True)).action == Action.APPROVAL


async def test_flipkart_skipped_while_only_amazon_enabled(pipeline):
    res = await pipeline.analyze(msg("Puma shoes ₹999 MRP ₹4,999\nhttps://fkrt.it/fk1"))
    assert res.action == Action.SKIP and res.reasons[0] == "store_not_enabled:flipkart"


async def test_flipkart_enabled_without_network_uses_policy(pipeline, settings):
    await settings.enable_store("flipkart")
    res = await pipeline.analyze(msg("Puma shoes ₹999 MRP ₹4,999\nhttps://fkrt.it/fk1"))
    assert res.action == Action.APPROVAL   # on_fail=approval
    assert "affid" not in res.deal.html and "thief" not in res.deal.html
    settings.cfg.conversion.on_fail = "skip"
    res2 = await pipeline.analyze(msg("Puma shoes ₹899 MRP ₹4,999\nhttps://fkrt.it/fk1", mid=9), dry_run=True)
    assert res2.action == Action.SKIP


async def test_mixed_message_drops_non_enabled_store_links(pipeline):
    res = await pipeline.analyze(msg(DEAL + "\nAlso on https://fkrt.it/fk1"))
    assert res.deal and "flipkart" not in res.deal.html.lower()


async def test_duplicate_skipped_after_queue(pipeline, db):
    res = await pipeline.analyze(msg(DEAL))
    await db.enqueue(res.deal, 0, 9e12)
    res2 = await pipeline.analyze(msg(DEAL, mid=2))
    assert res2.action == Action.SKIP and res2.reasons[0].startswith("duplicate")


async def test_no_links_and_forward_and_ad(pipeline):
    assert (await pipeline.analyze(msg("Good morning!"))).reasons == ["no_links"]
    assert (await pipeline.analyze(msg(DEAL, is_forward=True))).reasons == ["forwarded_message"]
    assert (await pipeline.analyze(msg("Paid promotion https://amzn.to/thief"))).reasons[0].startswith("ad_or_promo")


async def test_unresolved_link_only(pipeline):
    res = await pipeline.analyze(msg("Kettle ₹99 MRP ₹999 https://bit.ly/dead"))
    assert res.action == Action.SKIP and res.reasons == ["no_supported_store_link"]


async def test_dry_run_records_nothing(pipeline, db):
    await pipeline.analyze(msg(DEAL), dry_run=True)
    assert await db.last_skipped() == [] and await db.count_seen_since(0) == 0


async def test_skips_are_recorded_for_lastskipped(pipeline, db):
    await pipeline.analyze(msg("Kettle ₹900 MRP ₹1,000 https://amzn.to/thief"))
    rows = await db.last_skipped()
    assert rows and rows[0]["reasons"][0].startswith("discount_")


async def test_media_loader_called_only_for_passing_deals(pipeline):
    calls = []

    async def loader():
        calls.append(1)
        return []
    await pipeline.analyze(msg("Kettle ₹900 MRP ₹1,000 https://amzn.to/thief", media_loader=loader))
    assert calls == []
    await pipeline.analyze(msg(DEAL, mid=5, media_loader=loader))
    assert calls == [1]
