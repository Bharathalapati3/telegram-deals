"""Text extraction: URLs, prices, MRP, discount, coupon codes, bank offers, title.

All functions are pure (no I/O) so they are easy to unit test with real-style
Indian deal messages (English, Hinglish, emojis, fancy unicode fonts).
"""
from __future__ import annotations

import html
import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable, Optional

from models import ExtractedInfo

# --------------------------------------------------------------------------- text helpers

ZERO_WIDTH_RE = re.compile(r"[​‌‎‏⁠﻿]")
EMOJI_RE = re.compile(
    "["
    "\U0001F000-\U0001FAFF"   # pictographs, emoticons, transport, flags...
    "☀-➿"           # misc symbols & dingbats
    "⬀-⯿"           # arrows/stars
    "←-⇿⌀-⏿■-◿"
    "〰〽㊗㊙️⃣‍"
    "\U000E0000-\U000E007F"
    "]+"
)
URL_RE = re.compile(r"https?://[^\s<>\"'`]+", re.IGNORECASE)
BARE_URL_RE = re.compile(
    r"(?<![@\w/.])(?:www\.)?(?:[a-z0-9-]+\.)+(?:com|in|to|it|cc|co|me|ly|gl|link|page\.link|deals)"
    r"/[^\s<>\"'`]+",
    re.IGNORECASE,
)
MENTION_RE = re.compile(r"(?<!\w)@[A-Za-z0-9_]{4,}")
HASHTAG_RE = re.compile(r"(?<!\w)#\w+")
TME_RE = re.compile(r"(?:https?://)?(?:t\.me|telegram\.me|telegram\.dog)/\S+", re.IGNORECASE)
TRAILING_PUNCT = ".,!?;:)]}>»'\"*_~|"


def normalize_text(text: str) -> str:
    """NFKC-normalise (turns 𝗟𝗢𝗢𝗧 into LOOT), drop zero-width chars, unify newlines."""
    text = unicodedata.normalize("NFKC", text or "")
    text = ZERO_WIDTH_RE.sub("", text)
    return text.replace("\r\n", "\n").replace("\r", "\n")


def strip_emojis(text: str) -> str:
    return EMOJI_RE.sub("", text)


def _clean_url(url: str) -> str:
    url = url.strip()
    while url and url[-1] in TRAILING_PUNCT:
        # keep a closing paren if the URL contains a matching opening one
        if url[-1] == ")" and url.count("(") >= url.count(")"):
            break
        url = url[:-1]
    return url


def extract_urls(text: str, extra: Iterable[str] = ()) -> list[str]:
    """Return unique URLs from text (with or without scheme) plus ``extra`` (entities/buttons)."""
    text = normalize_text(text)
    found: list[str] = []
    for m in URL_RE.finditer(text):
        found.append(_clean_url(m.group(0)))
    without = URL_RE.sub(" ", text)
    for m in BARE_URL_RE.finditer(without):
        found.append("https://" + _clean_url(m.group(0)))
    for u in extra:
        if u:
            u = u.strip()
            if not re.match(r"^https?://", u, re.I):
                u = "https://" + u
            found.append(_clean_url(u))
    seen: dict[str, None] = {}
    for u in found:
        if u and not TME_RE.match(u):
            seen.setdefault(u, None)
    return list(seen)


# --------------------------------------------------------------------------- prices

CURRENCY = r"(?:₹|(?<![a-z])rs\.?|(?<![a-z])inr|(?<![a-z])rupees?)"
NUM = r"(\d{1,3}(?:,\d{2,3})+|\d+)(?:\.(\d{1,2}))?"
PRICE_PATTERNS = [
    re.compile(CURRENCY + r"\s*[:\-]?\s*" + NUM, re.IGNORECASE),
    re.compile(NUM + r"\s*(?:/-|₹|(?:rs\.?|rupees?|inr)(?![a-z]))", re.IGNORECASE),
]
AT_PRICE_RE = re.compile(r"@\s*" + NUM + r"(?!\s*%)")

MRP_CTX_RE = re.compile(
    r"(mrp|m\.r\.p|original(?:ly)?|was|actual|regular|list\s*price|retail|market\s*price|"
    r"asli|pehle|instead\s*of|worth|striked?)\W*$",
    re.IGNORECASE,
)
MRP_AFTER_RE = re.compile(r"^\W{0,3}(tha\b|\(\s*mrp\s*\))", re.IGNORECASE)
EFFECTIVE_CTX_RE = re.compile(
    r"(effective|after\s*(?:bank|coupon|offer|cashback|discount)|net\s*price|final\s*price|"
    r"with\s*(?:bank|coupon))[^\n₹]{0,25}$",
    re.IGNORECASE,
)
DEAL_CTX_RE = re.compile(
    r"(deal\s*price|offer\s*price|price|@|\bat\b|only|sirf|now|just|loot|steal|buy|mein|"
    r"keemat|kimat|daam)\W*$",
    re.IGNORECASE,
)
NON_PRICE_CTX_RE = re.compile(
    r"(cashback|discount\s*(?:of|upto|up\s*to)?|off\s*(?:on|upto)|save|saving|shipping|delivery|"
    r"min(?:imum)?\s*(?:order|purchase|cart)|above|orders?\s*(?:of|above)|upto|up\s*to|extra|"
    r"coupon\s*(?:of|worth)?|flat)\W*$",
    re.IGNORECASE,
)


@dataclass
class PriceMention:
    value: float
    start: int
    end: int
    kind: str  # "mrp" | "effective" | "deal" | "plain" | "aux" | "at"


def _to_float(int_part: str, dec: Optional[str]) -> float:
    v = float(int_part.replace(",", ""))
    if dec:
        v += float("0." + dec)
    return v


def find_prices(text: str, strike_texts: Iterable[str] = ()) -> list[PriceMention]:
    """Find all price mentions and classify them by surrounding context."""
    struck_values: set[float] = set()
    for st in strike_texts:
        for m in re.finditer(NUM, normalize_text(st)):
            try:
                struck_values.add(_to_float(m.group(1), m.group(2)))
            except ValueError:
                pass

    mentions: list[PriceMention] = []
    taken: list[tuple[int, int]] = []

    def overlaps(a: int, b: int) -> bool:
        return any(a < y and b > x for x, y in taken)

    for pat in PRICE_PATTERNS:
        for m in pat.finditer(text):
            if overlaps(m.start(), m.end()):
                continue
            value = _to_float(m.group(1), m.group(2))
            if value <= 0 or value > 10_000_000:
                continue
            # skip percentages like "₹ 50%"? (rare) and things like "Rs 500 off"
            after = text[m.end(): m.end() + 12]
            if re.match(r"\s*%", after):
                continue
            taken.append((m.start(), m.end()))
            mentions.append(PriceMention(value, m.start(), m.end(), "plain"))

    if not mentions:
        for m in AT_PRICE_RE.finditer(text):
            value = _to_float(m.group(1), m.group(2))
            if value > 0:
                mentions.append(PriceMention(value, m.start(), m.end(), "at"))

    for pm in mentions:
        line_start = text.rfind("\n", 0, pm.start) + 1
        before = text[max(line_start, pm.start - 30): pm.start]
        after = text[pm.end: pm.end + 15]
        after_line = after.split("\n", 1)[0]
        if pm.value in struck_values or MRP_CTX_RE.search(before) or MRP_AFTER_RE.search(after_line):
            pm.kind = "mrp"
        elif EFFECTIVE_CTX_RE.search(before):
            pm.kind = "effective"
        elif NON_PRICE_CTX_RE.search(before) or re.match(r"\s*(off|cashback|discount)", after_line, re.I):
            pm.kind = "aux"   # "Rs 200 off", "cashback Rs 100", "min order Rs 499"
        elif pm.kind == "at" or DEAL_CTX_RE.search(before):
            pm.kind = "deal"
    mentions.sort(key=lambda p: p.start)
    return mentions


def pick_prices(text: str, mentions: list[PriceMention]) -> tuple[Optional[float], Optional[float],
                                                                    Optional[float], bool]:
    """Choose (deal_price, mrp, effective_price, mrp_inferred) from classified mentions."""
    mrps = [p for p in mentions if p.kind == "mrp"]
    effs = [p for p in mentions if p.kind == "effective"]
    deals = [p for p in mentions if p.kind == "deal"]
    plains = [p for p in mentions if p.kind == "plain"]

    price_m = deals[0] if deals else (plains[0] if plains else None)
    mrp: Optional[float] = mrps[0].value if mrps else None
    inferred = False
    price = price_m.value if price_m else None

    if mrp is None and price_m is not None:
        # Infer MRP when two prices sit close together on one line ("₹299 ₹999", "₹999 → ₹299").
        candidates = [p for p in deals + plains if p is not price_m]
        for other in candidates:
            gap = other.start - price_m.end if other.start > price_m.end else price_m.start - other.end
            same_line = "\n" not in text[min(other.start, price_m.start): max(other.end, price_m.end)]
            if same_line and 0 <= gap <= 25:
                hi, lo = max(other.value, price_m.value), min(other.value, price_m.value)
                if hi >= lo * 1.1:
                    mrp, price, inferred = hi, lo, True
                    break

    if price is None and effs:
        price = effs[0].value
    if mrp is not None and price is not None and mrp <= price:
        # MRP must be above deal price; if not, swap when clearly reversed, else drop MRP
        if price > mrp * 1.1 and not deals:
            mrp, price = price, mrp
        else:
            mrp = None
    effective = effs[0].value if effs else None
    if effective is not None and price is not None and effective >= price:
        effective = None
    return price, mrp, effective, inferred


# --------------------------------------------------------------------------- discount

BANK_NAMES = (
    r"hdfc|icici|sbi|axis|kotak|idfc|bob|bank\s*of\s*baroda|onecard|one\s*card|au\s*bank|rbl|"
    r"yes\s*bank|federal|hsbc|citi|amex|american\s*express|indusind|paytm|flipkart\s*axis|"
    r"amazon\s*pay\s*icici|canara|pnb|union\s*bank|dbs|standard\s*chartered|bajaj|supercard"
)
BANK_RE = re.compile(rf"\b(?:{BANK_NAMES})\b", re.IGNORECASE)
BANK_OFFER_RE = re.compile(
    r"(card|credit|debit|emi|cashback|instant|discount|off|offer|bank|upi)", re.IGNORECASE)

DISCOUNT_RE = re.compile(
    r"(?P<pre>(?:flat|upto|up\s*to|min(?:imum)?|extra|get|at|@)?\s*)"
    r"(?P<pct>\d{1,3}(?:\.\d+)?)\s*%\s*"
    r"(?P<post>off|discount|disc|flat|chhoot|chhut|chut|kam|sasta|ki\s*chhoot|savings?)?",
    re.IGNORECASE,
)


def find_explicit_discounts(text: str) -> list[tuple[float, bool]]:
    """Return list of (percent, is_upto) for "% off" mentions, ignoring bank/extra offers."""
    out: list[tuple[float, bool]] = []
    for line in text.split("\n"):
        if BANK_RE.search(line):
            continue
        for m in DISCOUNT_RE.finditer(line):
            pre = (m.group("pre") or "").strip().lower()
            post = (m.group("post") or "").strip().lower()
            if not post and pre not in {"flat", "upto", "up to", "min", "minimum", "at", "@"}:
                continue
            if pre == "extra" or re.search(r"cashback", line[m.end(): m.end() + 15], re.I):
                continue
            pct = float(m.group("pct"))
            if 0 < pct <= 100:
                out.append((pct, pre.replace(" ", "") in {"upto", "min", "minimum"}))
    return out


def compute_discount(price: Optional[float], mrp: Optional[float]) -> Optional[float]:
    if price is None or mrp is None or mrp <= 0 or price >= mrp:
        return None
    return (mrp - price) / mrp * 100


# --------------------------------------------------------------------------- coupons & bank offers

COUPON_RE = re.compile(
    r"(?:coupon(?:\s*code)?|promo\s*code|use\s*code|apply\s*code|code)\s*[:\-–=]?\s*"
    r"[\"'“”‘’`]?(?P<code>[A-Za-z0-9]{4,20})",
    re.IGNORECASE,
)
COUPON_STOPWORDS = {
    "here", "below", "applied", "apply", "link", "now", "available", "page", "only", "auto",
    "automatically", "checkout", "cart", "worth", "will", "from", "with", "this", "that",
    "code", "coupon", "need", "required", "not", "none", "free", "offer", "on", "at", "pe",
}


def extract_coupon(text: str) -> Optional[str]:
    for m in COUPON_RE.finditer(text):
        code = m.group("code")
        if code.lower() in COUPON_STOPWORDS:
            continue
        has_letter = any(c.isalpha() for c in code)
        has_digit = any(c.isdigit() for c in code)
        if has_letter and (has_digit or code.isupper()):
            return code.upper()
    return None


def extract_bank_offer(text: str) -> Optional[str]:
    for line in text.split("\n"):
        if BANK_RE.search(line) and BANK_OFFER_RE.search(line):
            cleaned = clean_line(line)
            if len(cleaned) >= 6:
                return cleaned[:120].rstrip() + ("…" if len(cleaned) > 120 else "")
    return None


# --------------------------------------------------------------------------- title

PROMO_LINE_RE = re.compile(
    r"(join|subscribe|channel|telegram|whatsapp|follow\s*us|share\s*with|instagram|youtube|"
    r"click\s*here|tap\s*here|buy\s*now|grab\s*(?:it|now|fast)|hurry|limited\s*time|deal\s*link|"
    r"buy\s*link|link\s*:|order\s*now|more\s*deals|disclaimer|#ad\b|affiliate|commission)",
    re.IGNORECASE,
)
HEADER_ONLY_RE = re.compile(
    r"^(?:(?:super|mega|maha|big|crazy|price|pricing|loot|deals?|hot|steal|offers?|sale|alert|new|"
    r"best|today'?s?|lowest|all|time|low|atl|error|glitch|bug|of|the|day|fast|hurry|limited|"
    r"flash|lightning|grab|fastest|exclusive)\W*)+$",
    re.IGNORECASE,
)
INFO_LINE_RE = re.compile(
    r"^\W*(mrp|m\.r\.p|price|deal\s*price|offer\s*price|coupon|code|use\s*code|apply|bank|"
    r"effective|discount|off\b|cashback|link|buy|shop|order|\d+\s*%)", re.IGNORECASE)


def clean_line(line: str) -> str:
    """Remove URLs, t.me links, @mentions, hashtags, emojis and stray markdown from a line."""
    line = TME_RE.sub(" ", line)
    line = URL_RE.sub(" ", line)
    line = BARE_URL_RE.sub(" ", line)
    line = MENTION_RE.sub(" ", line)
    line = HASHTAG_RE.sub(" ", line)
    line = strip_emojis(line)
    line = re.sub(r"[*_~`|>]+", " ", line)
    line = re.sub(r"\s{2,}", " ", line)
    return line.strip(" -–—:•·|,.\t")


PRICE_TAIL_RE = re.compile(
    r"\s*(?:[@\-–—:|,(]|\bat\b|\bfor\b|\bonly\b|\bsirf\b|\bjust\b)*\s*" + CURRENCY + r"\s*[\d,]+.*$",
    re.IGNORECASE,
)


def extract_title(text: str) -> str:
    """Pick the most product-like line and clean it. Returns '' if nothing usable."""
    text = normalize_text(text)
    for raw in text.split("\n"):
        line = clean_line(raw)
        if len(line) < 8:
            continue
        if HEADER_ONLY_RE.match(line) or PROMO_LINE_RE.search(line) or INFO_LINE_RE.match(line):
            continue
        if BANK_RE.search(line) and BANK_OFFER_RE.search(line):
            continue
        line = PRICE_TAIL_RE.sub("", line).strip(" -–—:@|,(")
        line = re.sub(r"^(?:loot|deal|steal|price\s*error|glitch)\s*[:\-–—!]+\s*", "", line, flags=re.I)
        letters = sum(c.isalpha() for c in line)
        if letters < 6 or len(line.split()) < 2:
            continue
        return line[:150].rstrip() + ("…" if len(line) > 150 else "")
    return ""


# --------------------------------------------------------------------------- main entry

def extract_info(text: str, strike_texts: Iterable[str] = ()) -> ExtractedInfo:
    """Parse all deal facts from message text."""
    text = normalize_text(html.unescape(text or ""))
    mentions = find_prices(text, strike_texts)
    price, mrp, effective, mrp_inferred = pick_prices(text, mentions)
    explicit = find_explicit_discounts(text)
    hard = [p for p, upto in explicit if not upto]
    upto = [p for p, is_upto in explicit if is_upto]

    info = ExtractedInfo(
        title=extract_title(text),
        price=price,
        mrp=mrp,
        effective_price=effective,
        coupon=extract_coupon(text),
        bank_offer=extract_bank_offer(text),
    )
    info.explicit_discount = max(hard) if hard else (max(upto) if upto else None)
    info.discount_is_upto = bool(not hard and upto)

    computed = compute_discount(price, mrp)
    if computed is not None and hard:
        best_hard = min(hard, key=lambda p: abs(p - computed))
        if abs(best_hard - computed) <= 5:
            info.discount, info.discount_source = computed, "computed"
        elif mrp_inferred:
            info.discount, info.discount_source = best_hard, "explicit"
        else:
            # disagreement → be conservative
            info.discount, info.discount_source = min(best_hard, computed), "conservative"
    elif computed is not None:
        info.discount, info.discount_source = computed, "computed"
    elif hard:
        info.discount, info.discount_source = max(hard), "explicit"
    elif upto:
        info.discount, info.discount_source = max(upto), "upto"
    if info.discount is not None:
        info.discount = float(int(info.discount + 0.5))  # round half up
    return info
