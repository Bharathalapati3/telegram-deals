import pytest

from converters import AmazonConverter, ConverterSet, HttpApiSpec
from converters.base import dig
from converters.earnkaro import EarnKaroConverter
from models import ResolvedLink
from tests.conftest import FakeResponse, FakeSession

ASIN = "B0BZ8TRMK6"


async def test_amazon_rebuilds_with_my_tag_only():
    c = AmazonConverter("mytag-21")
    r = await c.convert(f"https://www.amazon.in/Some-Title/dp/{ASIN}/ref=x?tag=thief-21&ascsubtag=abc&linkCode=ll1")
    assert r.ok and r.url == f"https://www.amazon.in/dp/{ASIN}?tag=mytag-21"
    assert "thief" not in r.url


async def test_amazon_non_product_page_gets_my_tag():
    c = AmazonConverter("mytag-21")
    r = await c.convert("https://www.amazon.in/deals?tag=thief-21&ref_=nav&discounts-widget=x")
    assert r.ok and "tag=mytag-21" in r.url and "thief" not in r.url


@pytest.mark.parametrize("url,mine", [
    (f"https://www.amazon.in/dp/{ASIN}?tag=mytag-21", True),
    (f"https://www.amazon.in/dp/{ASIN}?tag=thief-21", False),
    (f"https://www.amazon.in/dp/{ASIN}?tag=mytag-21&ascsubtag=x", False),
    (f"https://www.amazon.in/dp/{ASIN}", False),
    (f"https://www.amazon.in/dp/{ASIN}?tag=mytag-21&tag=thief-21", False),
    (f"https://evil.com/dp/{ASIN}?tag=mytag-21", False),
])
def test_amazon_is_my_link(url, mine):
    assert AmazonConverter("mytag-21").is_my_link(url) is mine


async def test_http_converter_not_configured(cfg):
    c = EarnKaroConverter(HttpApiSpec(), ["ekaro.in"], stores_map=cfg.stores)
    r = await c.convert("https://www.flipkart.com/p/x?pid=1")
    assert not r.ok and "not configured" in r.note and r.store == "flipkart"


async def test_http_converter_success_with_response_path(cfg):
    session = FakeSession({"https://api.example.test/convert": FakeResponse(
        200, json_data={"data": {"link": "https://ekaro.in/enkr123"}})})
    spec = HttpApiSpec(api_url="https://api.example.test/convert", api_key="KEY", request_field="deal",
                       response_path="data.link", extra_body={"mode": "x"})
    c = EarnKaroConverter(spec, ["ekaro.in"], stores_map=cfg.stores, session=session, retries=0)
    r = await c.convert("https://www.flipkart.com/p/x?pid=1")
    assert r.ok and r.url == "https://ekaro.in/enkr123"
    assert session.posts[0]["json"] == {"deal": "https://www.flipkart.com/p/x?pid=1", "mode": "x"}
    assert session.posts[0]["headers"]["Authorization"] == "Bearer KEY"


async def test_http_converter_bad_response(cfg):
    session = FakeSession({"https://api.example.test/c": FakeResponse(200, json_data={"error": "nope"})})
    spec = HttpApiSpec(api_url="https://api.example.test/c", api_key="K", response_path="data.link")
    c = EarnKaroConverter(spec, ["ekaro.in"], stores_map=cfg.stores, session=session, retries=0)
    r = await c.convert("https://www.flipkart.com/p/x")
    assert not r.ok


def test_dig():
    assert dig({"a": [{"b": "x"}]}, "a.0.b") == "x"
    assert dig({"a": []}, "a.0.b") is None


async def test_converter_set_routes(cfg):
    cs = ConverterSet(AmazonConverter("mytag-21"), None, cfg.stores)
    amz = ResolvedLink("u", f"https://www.amazon.in/dp/{ASIN}?tag=x-21", True, "amazon", ASIN,
                       f"https://www.amazon.in/dp/{ASIN}")
    fk = ResolvedLink("u", "https://www.flipkart.com/p/x?pid=1&affid=x", True, "flipkart", None,
                      "https://www.flipkart.com/p/x?pid=1")
    r1 = await cs.convert(amz)
    r2 = await cs.convert(fk)
    assert r1.ok and r1.url.endswith("tag=mytag-21")
    assert not r2.ok and r2.url == "https://www.flipkart.com/p/x?pid=1"   # clean, no foreign affid
