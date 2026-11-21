from config import AppConfig
from formatter import (CAPTION_LIMIT, TEXT_LIMIT, find_foreign_links, finalize_custom_text, fmt_inr,
                       format_post, render_deal_over, utf16_len, visible_len, visible_text)
from models import ConvertedLink, ExtractedInfo, Label

CFG = AppConfig()
MY = "https://www.amazon.in/dp/B0BZ8TRMK6?tag=mytag-21"


def post(label=Label.NORMAL, title="boAt Airdopes 141", has_media=False, **info):
    base = dict(price=799.0, mrp=4490.0, discount=82, discount_source="computed")
    base.update(info)
    return format_post(title=title, info=ExtractedInfo(**base), label=label,
                       links=[ConvertedLink("amazon", MY, True)], store="amazon",
                       t=CFG.templates, stores=CFG.stores, has_media=has_media)


def test_basic_post():
    r = post(coupon="SAVE10")
    txt = visible_text(r.html)
    assert r.html.startswith("✅ <b>Deal</b>")
    assert "<s>₹4,490</s>" in r.html
    assert "82% OFF" in txt and "SAVE10" in txt and "#ad" in txt
    assert f'href="{MY}"' in r.html
    assert "Bank" not in txt and "Effective" not in txt  # empty lines dropped


def test_headers():
    assert "PRICE ERROR" in post(Label.PRICE_ERROR).html
    assert "#PriceError" in post(Label.PRICE_ERROR).html
    assert post(Label.HOT_DEAL).html.startswith("🔥")


def test_no_mrp_line_when_unknown():
    r = post(mrp=None, discount=None, discount_source="")
    assert "MRP" not in r.html and "OFF" not in r.html


def test_title_is_escaped():
    r = post(title="Tom & Jerry <script>")
    assert "Tom &amp; Jerry &lt;script&gt;" in r.html


def test_caption_limit_trims_title():
    r = post(title="Super long product name " * 80, has_media=True, bank_offer="10% off on HDFC " * 5)
    assert visible_len(r.html) <= CAPTION_LIMIT and r.fits_caption
    assert MY in r.html and "#ad" in r.html


def test_text_limit():
    r = post(title="x" * 5000)
    assert visible_len(r.html) <= TEXT_LIMIT


def test_utf16_counts_emoji_as_two():
    assert utf16_len("🔥") == 2 and utf16_len("₹") == 1


def test_fmt_inr():
    assert fmt_inr(149999) == "1,49,999"
    assert fmt_inr(499.0) == "499"
    assert fmt_inr(1234567.5) == "12,34,567.50"


def test_deal_over():
    r = post()
    over = render_deal_over(r.body_html, CFG.templates)
    assert over.startswith("❌ <b>DEAL OVER</b>") and "Deal</b>" not in over.split("\n")[0]


def test_foreign_links_detection():
    allowed = {MY}
    assert find_foreign_links(post().html, allowed, own_channel="@mychannel") == []
    bad = f'<a href="{MY}">x</a> https://amzn.to/abc @otherdeals https://t.me/mychannel'
    found = find_foreign_links(bad, allowed, own_channel="@mychannel")
    assert found == ["https://amzn.to/abc", "@otherdeals"]


def test_finalize_custom_text_adds_links_and_disclosure():
    out = finalize_custom_text("<b>Great deal</b> on earbuds", [ConvertedLink("amazon", MY, True)],
                               CFG.templates, CFG.stores)
    assert MY in out and "#ad" in out
