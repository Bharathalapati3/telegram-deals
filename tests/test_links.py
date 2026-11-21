import asyncio

import aiohttp
import pytest

from links import (Expander, affiliate_params_in, canonicalize, detect_store, extract_asin,
                   strip_tracking)
from tests.conftest import FakeResponse, FakeSession, redirect

ASIN = "B0BZ8TRMK6"


@pytest.mark.parametrize("url", [
    f"https://www.amazon.in/dp/{ASIN}",
    f"https://www.amazon.in/dp/{ASIN}/",
    f"https://www.amazon.in/dp/{ASIN}?tag=someone-21&linkCode=ogi&th=1",
    f"https://www.amazon.in/Samsung-Galaxy-Smoky-Storage-Without/dp/{ASIN}/ref=sr_1_3?keywords=m14",
    f"https://www.amazon.in/gp/product/{ASIN}/ref=ox_sc_act_title_1?smid=A14CZOWI0VEHLG&psc=1",
    f"https://www.amazon.in/gp/aw/d/{ASIN}?psc=1",
    f"https://amazon.in/gp/aw/d/{ASIN}/ref=mp_s_a_1_1",
    f"https://m.amazon.in/dp/{ASIN}",
    f"https://www.amazon.in/gp/offer-listing/{ASIN}",
    f"https://www.amazon.in/exec/obidos/ASIN/{ASIN}/someone-21",
    f"https://www.amazon.in/product-reviews/{ASIN}",
    f"https://www.amazon.in/d/{ASIN}",
    f"https://www.amazon.in/dp/product/{ASIN}",
    f"https://www.amazon.in/some-title/dp/{ASIN.lower()}?_encoding=UTF8",
    f"https://www.amazon.in/gp/product/glance/{ASIN}",
    f"https://www.amazon.in/s?k=phone&asin={ASIN}",
])
def test_asin_shapes(url):
    assert extract_asin(url) == ASIN


def test_asin_isbn_and_none():
    assert extract_asin("https://www.amazon.in/dp/817525766X") == "817525766X"
    assert extract_asin("https://www.amazon.in/deals?ref=nav") is None
    assert extract_asin("https://www.amazon.in/dp/NOTANASIN1") is None


def test_detect_store(cfg):
    assert detect_store("https://www.amazon.in/dp/X", cfg.stores) == "amazon"
    assert detect_store("https://dl.flipkart.com/s/abc", cfg.stores) == "flipkart"
    assert detect_store("https://www.myntra.com/123", cfg.stores) == "myntra"
    assert detect_store("https://luxury.tatacliq.com/x", cfg.stores) == "tatacliq"
    assert detect_store("https://amazon.com/dp/X", cfg.stores) is None
    assert detect_store("https://notamazon.in/dp/X", cfg.stores) is None


def test_strip_tracking_generic():
    url = ("https://www.croma.com/p/123?utm_source=tg&utm_medium=x&affid=someone"
           "&affExtParam1=abc&affExtParam2=def&ref=tg&color=red&fbclid=zz")
    assert strip_tracking(url) == "https://www.croma.com/p/123?color=red"


def test_strip_tracking_keep_params_flipkart(cfg):
    url = ("https://www.flipkart.com/boat-airdopes/p/itm123?pid=ACCG7&lid=LST1&marketplace=FLIPKART"
           "&affid=bharat&affExtParam1=ENKR&cmpid=x&otracker=search")
    pid, canon = canonicalize(url, "flipkart", cfg.stores)
    assert pid is None
    assert canon == "https://www.flipkart.com/boat-airdopes/p/itm123?pid=ACCG7&lid=LST1&marketplace=FLIPKART"


def test_canonical_amazon(cfg):
    pid, canon = canonicalize(f"https://www.amazon.in/x/dp/{ASIN}?tag=other-21&ascsubtag=z", "amazon", cfg.stores)
    assert pid == ASIN and canon == f"https://www.amazon.in/dp/{ASIN}"


def test_affiliate_params_detected():
    assert set(affiliate_params_in("https://x.com/p?tag=a-21&affid=b&color=red&ascsubtag=c")) == {
        "tag", "affid", "ascsubtag"}
    assert affiliate_params_in("https://x.com/p?color=red") == []


# ------------------------------------------------------------------ expander (mocked network)

async def test_expand_redirect_chain_stops_at_store(cfg):
    session = FakeSession({
        "https://bit.ly/abc": redirect("https://amzn.to/xyz"),
        "https://amzn.to/xyz": redirect(f"https://www.amazon.in/dp/{ASIN}?tag=someone-21"),
    })
    ex = Expander(cfg.expansion, cfg.stores, session=session)
    r = await ex.resolve("https://bit.ly/abc")
    assert r.resolved and r.store == "amazon" and r.product_id == ASIN
    assert r.key == f"amazon:{ASIN}"
    # never requested the amazon product page itself
    assert not any("amazon.in" in c for c in session.calls)


async def test_expand_failure_marks_unresolved(cfg):
    session = FakeSession({"https://fkrt.it/dead": aiohttp.ClientConnectionError("boom")})
    ex = Expander(cfg.expansion, cfg.stores, session=session)
    r = await ex.resolve("https://fkrt.it/dead")
    assert not r.resolved and r.store is None


async def test_expand_timeout_never_raises(cfg):
    session = FakeSession({"https://cutt.ly/t": asyncio.TimeoutError()})
    ex = Expander(cfg.expansion, cfg.stores, session=session)
    r = await ex.resolve("https://cutt.ly/t")
    assert not r.resolved


async def test_max_redirects(cfg):
    cfg.expansion.max_redirects = 3
    routes = {f"https://tinyurl.com/{i}": redirect(f"https://tinyurl.com/{i + 1}") for i in range(10)}
    ex = Expander(cfg.expansion, cfg.stores, session=FakeSession(routes))
    r = await ex.resolve("https://tinyurl.com/0")
    assert not r.resolved
    assert len(ex._session.calls) == 4


async def test_meta_refresh_and_embedded_target(cfg):
    body = b'<html><meta http-equiv="refresh" content="0; url=https://linkredirect.in/visit?dl=https%3A%2F%2Fwww.amazon.in%2Fdp%2FB0BZ8TRMK6"></html>'
    session = FakeSession({"https://ekaro.in/enkr1": FakeResponse(200, {"Content-Type": "text/html"}, body)})
    ex = Expander(cfg.expansion, cfg.stores, session=session)
    r = await ex.resolve("https://ekaro.in/enkr1")
    assert r.store == "amazon" and r.product_id == ASIN


async def test_cache_used(cfg, db):
    session = FakeSession({"https://amzn.to/c": redirect(f"https://www.amazon.in/dp/{ASIN}")})
    ex = Expander(cfg.expansion, cfg.stores, cache=db, session=session)
    await ex.resolve("https://amzn.to/c")
    await ex.resolve("https://amzn.to/c")
    assert session.calls == ["https://amzn.to/c"]


async def test_direct_store_link_not_fetched(cfg):
    session = FakeSession({})
    ex = Expander(cfg.expansion, cfg.stores, session=session)
    r = await ex.resolve(f"https://www.amazon.in/dp/{ASIN}?tag=x-21")
    assert r.product_id == ASIN and session.calls == []
