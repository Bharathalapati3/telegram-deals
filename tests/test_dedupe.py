import time

from dedupe import check_duplicate, normalize_title, title_similarity
from models import Action, ConvertedLink, ExtractedInfo, Label, ProcessedDeal


def make_deal(key="amazon:B0BZ8TRMK6", title="boAt Airdopes 141 Bluetooth TWS Earbuds", price=799.0):
    return ProcessedDeal(source_id=1, source_name="s", message_id=1, deal_key=key, store="amazon",
                         product_id=None, title=title, title_norm=normalize_title(title),
                         info=ExtractedInfo(title=title, price=price), label=Label.HOT_DEAL, score=80,
                         reasons=[], links=[ConvertedLink("amazon", "https://x", True)], all_converted=True,
                         brand_allowed=True, action=Action.AUTO)


async def test_same_key_is_duplicate(db, cfg):
    d = make_deal()
    await db.add_posted(d, [10], "", "", False)
    r = await check_duplicate(db, d.deal_key, d.title_norm, 799, time.time(), cfg.dedupe)
    assert r.duplicate


async def test_price_drop_allows_repost(db, cfg):
    d = make_deal()
    await db.add_posted(d, [10], "", "", False)
    r = await check_duplicate(db, d.deal_key, d.title_norm, 699, time.time(), cfg.dedupe)  # 12.5% lower
    assert not r.duplicate and r.price_dropped
    r2 = await check_duplicate(db, d.deal_key, d.title_norm, 760, time.time(), cfg.dedupe)  # only 5% lower
    assert r2.duplicate


async def test_outside_window_not_duplicate(db, cfg):
    d = make_deal()
    await db.add_posted(d, [10], "", "", False)
    later = time.time() + 25 * 3600
    r = await check_duplicate(db, d.deal_key, d.title_norm, 799, later, cfg.dedupe)
    assert not r.duplicate


async def test_fuzzy_title_duplicate_different_link(db, cfg):
    await db.add_posted(make_deal(), [10], "", "", False)
    other_title = normalize_title("LOOT: boAt Airdopes 141 Bluetooth TWS Earbuds (Black)")
    r = await check_duplicate(db, "amazon:B0OTHER0000", other_title, 799, time.time(), cfg.dedupe)
    assert r.duplicate and r.reason.startswith("duplicate_title")


async def test_pending_and_queue_count_as_seen(db, cfg):
    d = make_deal(key="amazon:B0QUEUED000", title="Philips Air Fryer HD9200 4.1L")
    await db.enqueue(d, 0, time.time() + 600)
    r = await check_duplicate(db, d.deal_key, "", 799, time.time(), cfg.dedupe)
    assert r.duplicate
    d2 = make_deal(key="amazon:B0PENDING00", title="Bajaj Mixer Grinder 750W")
    await db.add_pending(d2, time.time() + 600)
    r2 = await check_duplicate(db, d2.deal_key, "", 799, time.time(), cfg.dedupe)
    assert r2.duplicate


def test_title_similarity():
    a = normalize_title("Samsung Galaxy M14 5G 6GB 128GB")
    b = normalize_title("Deal: Samsung Galaxy M14 5G 6GB 128GB!!")
    c = normalize_title("Prestige Induction Cooktop PIC 20")
    assert title_similarity(a, b) > 0.9
    assert title_similarity(a, c) < 0.5
