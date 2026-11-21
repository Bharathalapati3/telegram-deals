import pytest

from extract import extract_info
from filters import detect_price_error, evaluate, is_ad
from models import Label


def run(text, cfg, **kw):
    info = extract_info(text)
    kw.setdefault("has_store_link", True)
    return evaluate(text, info, cfg.filters, cfg.price_error, **kw)


@pytest.mark.parametrize("text", [
    "PRICE ERROR!! Bosch Drill ₹499 MRP ₹6,999",
    "Pricing error on Amazon — order fast. Mixer ₹1,299 MRP ₹2,000",
    "Glitch: Sony speaker",
    "Fastrack watch ₹9 MRP ₹1,995",                 # absurd price
    "Lenovo tablet ₹1,999 MRP ₹24,999",              # 92% off
    "Prestige cooker ₹149 (MRP ₹1,999)",             # ratio < 0.10 → 93%
])
def test_price_errors(text, cfg):
    assert run(text, cfg).label == Label.PRICE_ERROR


def test_loot_keyword_alone_is_not_price_error(cfg):
    r = run("Loot deal: boAt earbuds ₹899 MRP ₹2,990", cfg)
    assert r.label == Label.HOT_DEAL   # 70% → hot, but not a price error


def test_upto_90_is_not_price_error(cfg):
    info = extract_info("Upto 90% off on shoes")
    is_pe, _, _ = detect_price_error("Upto 90% off on shoes", info, cfg.price_error)
    assert not is_pe


def test_min_discount(cfg):
    r = run("Pigeon Kettle ₹800 MRP ₹1,000", cfg)  # 20%
    assert r.label == Label.SKIP and any("min_" in x for x in r.reasons)


def test_category_threshold(cfg):
    # electronics threshold 35% → 40% passes; fashion 65% → 60% fails
    assert run("Redmi smartphone ₹6,000 MRP ₹10,000", cfg).label == Label.NORMAL
    assert run("Allen Solly shirt ₹400 MRP ₹1,000", cfg).label == Label.SKIP


def test_hot_deal(cfg):
    assert run("Cello bottle set ₹299 MRP ₹1,199", cfg).label == Label.HOT_DEAL


def test_no_store_link(cfg):
    r = run("Kettle ₹100 MRP ₹1000", cfg, has_store_link=False)
    assert r.label == Label.SKIP and r.reasons == ["no_supported_store_link"]


@pytest.mark.parametrize("text", [
    "Renewed iPhone 12 ₹20,000 MRP ₹60,000",
    "Refurbished laptop ₹9,999 MRP ₹50,000",
    "Used camera 80% off",
])
def test_keyword_blocklist(text, cfg):
    assert run(text, cfg).label == Label.SKIP


def test_brand_block_and_allow(cfg):
    cfg.filters.brand_blocklist = ["generic"]
    cfg.filters.brand_allowlist = ["boat", "philips"]
    assert run("Generic earbuds ₹99 MRP ₹999", cfg).label == Label.SKIP
    r = run("Philips trimmer ₹699 MRP ₹1,995", cfg)
    assert r.brand_allowed and r.brand == "philips"


def test_unknown_discount_policy(cfg):
    assert run("Nice bag @ 499", cfg).label == Label.NORMAL
    cfg.filters.unknown_discount = "skip"
    assert run("Nice bag @ 499", cfg).label == Label.SKIP


def test_too_many_products(cfg):
    assert run("x ₹1 MRP ₹100", cfg, product_count=5).label == Label.SKIP


def test_is_ad(cfg):
    assert is_ad("Paid Promotion: join this trading group", cfg.filters)
    assert is_ad("boAt earbuds ₹899", cfg.filters) is None


async def test_runtime_overrides_apply(settings):
    await settings.block("milton")
    await settings.allow("boat")
    await settings.set("min_discount", 80)
    fc = settings.filter_config()
    assert "milton" in fc.keyword_blocklist and "boat" in fc.brand_allowlist and fc.min_discount == 80
