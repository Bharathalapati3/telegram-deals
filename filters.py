"""Filtering and scoring: decide PRICE_ERROR / HOT_DEAL / NORMAL / SKIP with reasons."""
from __future__ import annotations

import re
from typing import Iterable, Optional

from config import FilterConfig, PriceErrorConfig
from extract import normalize_text
from models import ExtractedInfo, FilterResult, Label


def _word_re(word: str) -> re.Pattern[str]:
    w = re.escape(word.strip().lower()).replace(r"\ ", r"\s+")
    return re.compile(rf"(?<![a-z0-9]){w}(?![a-z0-9])", re.IGNORECASE)


def find_words(text: str, words: Iterable[str]) -> list[str]:
    """Return the words/phrases from ``words`` that appear as whole words in ``text``."""
    return [w for w in words if w.strip() and _word_re(w).search(text)]


def is_ad(text: str, fc: FilterConfig) -> Optional[str]:
    """Return the matched ad keyword if the message looks like a promo/ad, else None."""
    hits = find_words(normalize_text(text).lower(), fc.ad_keywords)
    return hits[0] if hits else None


def detect_category(text: str, fc: FilterConfig) -> Optional[str]:
    low = text.lower()
    best, best_hits = None, 0
    for cat, words in fc.categories.items():
        hits = len(find_words(low, words))
        if hits > best_hits:
            best, best_hits = cat, hits
    return best


def detect_price_error(text: str, info: ExtractedInfo, pe: PriceErrorConfig) -> tuple[bool, list[str], bool]:
    """Return (is_price_error, reasons, weak_keyword_hit)."""
    reasons: list[str] = []
    low = text.lower()
    strong = find_words(low, pe.strong_keywords)
    weak = find_words(low, pe.weak_keywords)
    if strong:
        reasons.append(f"price_error_keyword:{strong[0]}")
    reliable_discount = info.discount if info.discount_source not in ("upto", "") else None
    if reliable_discount is not None and reliable_discount >= pe.min_discount:
        reasons.append(f"discount_{reliable_discount:g}%>=price_error_min_{pe.min_discount:g}%")
    if info.price is not None and info.mrp is not None:
        if info.price <= pe.max_absurd_price and info.mrp >= pe.absurd_min_mrp:
            reasons.append(f"absurd_price:₹{info.price:g}_vs_mrp_₹{info.mrp:g}")
        elif info.mrp > 0 and info.price / info.mrp <= pe.max_price_ratio:
            reasons.append(f"price_ratio:{info.price / info.mrp:.2f}")
    return bool(reasons), reasons, bool(weak)


def evaluate(text: str, info: ExtractedInfo, fc: FilterConfig, pe: PriceErrorConfig, *,
             has_store_link: bool, product_count: int = 1, unresolved_links: int = 0) -> FilterResult:
    """Score a deal and give it a label. ``fc`` should already include runtime overrides."""
    text = normalize_text(text)
    low = text.lower()
    reasons: list[str] = []
    title_low = (info.title or "").lower()

    if not has_store_link:
        return FilterResult(Label.SKIP, 0, ["no_supported_store_link"])
    if product_count > fc.max_products_per_post:
        return FilterResult(Label.SKIP, 0, [f"too_many_products:{product_count}>{fc.max_products_per_post}"])

    blocked_kw = find_words(low, fc.keyword_blocklist)
    if blocked_kw:
        return FilterResult(Label.SKIP, 0, [f"blocked_keyword:{blocked_kw[0]}"])
    blocked_brand = find_words(low, fc.brand_blocklist)
    if blocked_brand:
        return FilterResult(Label.SKIP, 0, [f"blocked_brand:{blocked_brand[0]}"], brand=blocked_brand[0])

    allowed = find_words(title_low or low, fc.brand_allowlist) or find_words(low, fc.brand_allowlist)
    brand = allowed[0] if allowed else None
    category = detect_category(title_low or low, fc)

    is_pe, pe_reasons, weak_kw = detect_price_error(text, info, pe)
    discount = info.discount

    # ---- score
    score = 0.0
    if discount is not None:
        score += discount * (0.6 if info.discount_source == "upto" else 1.0)
    else:
        score += 20
        reasons.append("discount_unknown")
    if is_pe:
        score += 25
    if brand:
        score += 10
        reasons.append(f"brand_allowed:{brand}")
    if info.coupon:
        score += 3
    if info.bank_offer:
        score += 2
    if weak_kw:
        score += 5
    if info.price is None:
        score -= 10
        reasons.append("price_unknown")
    if not info.title:
        score -= 10
        reasons.append("title_unknown")
    if unresolved_links:
        score -= 10 * unresolved_links
        reasons.append(f"unresolved_links:{unresolved_links}")
    score = round(max(0.0, min(150.0, score)), 1)

    result = FilterResult(Label.NORMAL, score, reasons, brand=brand, brand_allowed=bool(brand),
                          category=category)

    if is_pe:
        result.label = Label.PRICE_ERROR
        result.reasons = pe_reasons + reasons
        return result

    # ---- thresholds
    min_disc = fc.min_discount
    if category and category in fc.category_min_discount:
        min_disc = float(fc.category_min_discount[category])
        reasons.append(f"category:{category}(min {min_disc:g}%)")
    if discount is None:
        if fc.unknown_discount == "skip":
            result.label = Label.SKIP
            result.reasons = ["discount_unknown_and_unknown_discount=skip"]
        return result
    if info.discount_source == "upto":
        reasons.append("discount_is_upto")
    if discount < min_disc:
        result.label = Label.SKIP
        result.reasons = [f"discount_{discount:g}%<min_{min_disc:g}%"] + reasons
        return result
    reliable = info.discount_source != "upto"
    if reliable and (discount >= fc.hot_deal_min_discount or score >= fc.hot_deal_min_score):
        result.label = Label.HOT_DEAL
    return result
