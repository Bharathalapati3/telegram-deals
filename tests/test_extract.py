import pytest

from extract import (extract_bank_offer, extract_coupon, extract_info, extract_title, extract_urls,
                     find_explicit_discounts)

# (message, price, mrp, discount)
PRICE_CASES = [
    ("boAt Airdopes 141 @ ₹799\nMRP: ₹4,490\nhttps://amzn.to/x", 799, 4490, 82),
    ("Philips Trimmer sirf Rs.99 mein! MRP Rs 1995 tha", 99, 1995, 95),
    ("Deal Price - 11,999/-\nM.R.P. ₹ 17,990", 11999, 17990, 33),
    ("Levis Jeans ₹699 ₹2,999", 699, 2999, 77),
    ("Price: INR 1,49,999 (MRP INR 2,09,900)", 149999, 209900, 29),
    ("🔥 Prestige Kettle Rs 549 only (M.R.P Rs 1,595) 66% off", 549, 1595, 66),
    ("Lowest ever! Fire-Boltt watch at ₹1,099. Original price ₹9,999", 1099, 9999, 89),
    ("Bag @ 499\nhttps://amzn.to/y", 499, None, None),
    ("Mivi Earbuds ₹ 699.00 (MRP ₹2,999.00)", 699, 2999, 77),
    ("जबरदस्त deal: Bajaj Mixer Rs. 1,899 me, MRP Rs. 4,500", 1899, 4500, 58),
    ("Noise Smartwatch ~₹5,999~ ₹1,299 only", 1299, 5999, 78),
]


@pytest.mark.parametrize("text,price,mrp,discount", PRICE_CASES)
def test_price_mrp_discount(text, price, mrp, discount):
    info = extract_info(text)
    assert info.price == price
    assert info.mrp == mrp
    assert info.discount == discount


def test_strikethrough_entity_marks_mrp():
    info = extract_info("Shoes ₹4,999 ₹899", strike_texts=["₹4,999"])
    assert info.mrp == 4999 and info.price == 899


def test_explicit_discount_only():
    info = extract_info("Flat 70% off on Puma shoes ₹1,199")
    assert info.discount == 70 and info.discount_source == "explicit"


def test_upto_discount_flagged():
    info = extract_info("Upto 80% off on Men's T-shirts")
    assert info.discount == 80 and info.discount_source == "upto" and info.discount_is_upto


def test_mismatched_discount_is_conservative():
    # text claims 90% but prices say 50% → take the lower
    info = extract_info("Mixer ₹1,000 MRP ₹2,000 90% off")
    assert info.discount == 50


def test_bank_and_extra_discounts_ignored():
    found = find_explicit_discounts("10% instant discount with HDFC cards\nExtra 5% off with coupon")
    assert found == []


def test_effective_price():
    info = extract_info("Phone ₹15,999\nMRP ₹24,999\nEffective price ₹13,999 with bank offer")
    assert info.price == 15999 and info.effective_price == 13999


def test_cashback_amount_is_not_price():
    info = extract_info("Cashback Rs 200 on Paytm\nSofa cover ₹399 MRP ₹1,299")
    assert info.price == 399


@pytest.mark.parametrize("text,code", [
    ("Use code SAVE50 at checkout", "SAVE50"),
    ("Apply Coupon: BOAT10", "BOAT10"),
    ("coupon code - flat100", "FLAT100"),
    ("Promo code “MEGA20”", "MEGA20"),
    ("Coupon applied automatically", None),
    ("Apply coupon on product page", None),
])
def test_coupon(text, code):
    assert extract_coupon(text) == code


def test_bank_offer():
    assert extract_bank_offer("🔥 Deal\n💳 10% off with SBI Credit Card EMI") == "10% off with SBI Credit Card EMI"
    assert extract_bank_offer("No bank offer here") is None


@pytest.mark.parametrize("text,title", [
    ("🔥🔥 LOOT DEAL 🔥🔥\nboAt Rockerz 450 Headphones @ ₹999\nhttps://amzn.to/a", "boAt Rockerz 450 Headphones"),
    ("𝗣𝗥𝗜𝗖𝗘 𝗘𝗥𝗥𝗢𝗥\nPhilips Air Fryer HD9200 sirf Rs.99", "Philips Air Fryer HD9200"),
    ("Join @lootchannel\nSamsung 980 SSD 1TB - ₹5,499", "Samsung 980 SSD 1TB"),
    ("MRP ₹999\nhttps://fkrt.it/x", ""),
])
def test_title(text, title):
    assert extract_title(text).startswith(title) if title else extract_title(text) == ""


def test_urls_with_and_without_scheme_and_entities():
    text = "Buy: amzn.to/3abc and https://fkrt.it/xyz). Join t.me/deals"
    urls = extract_urls(text, extra=["https://bit.ly/hidden"])
    assert urls == ["https://fkrt.it/xyz", "https://amzn.to/3abc", "https://bit.ly/hidden"]


def test_emojis_and_fancy_fonts_are_handled():
    info = extract_info("😱😱 𝐒𝐭𝐞𝐚𝐥 𝐃𝐞𝐚𝐥 😱😱\nWildcraft Backpack 45L at ₹₹ 649 🏃\n(MRP ₹2,499) 74% OFF")
    assert info.price == 649 and info.mrp == 2499 and info.discount == 74
    assert info.title.startswith("Wildcraft Backpack 45L")
