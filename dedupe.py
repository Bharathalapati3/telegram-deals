"""Duplicate detection: same product key or near-identical title within a time window."""
from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any, Optional, Protocol

from config import DedupeConfig

STOPWORDS = {
    "the", "a", "an", "for", "with", "and", "of", "in", "on", "to", "by", "at", "off", "deal",
    "loot", "price", "offer", "buy", "only", "now", "new", "best", "rs", "inr", "mrp", "sale",
}


def normalize_title(title: str) -> str:
    """Lowercase, keep alphanumerics, drop filler words; used for fuzzy matching."""
    words = re.findall(r"[a-z0-9]+", (title or "").lower())
    return " ".join(w for w in words if w not in STOPWORDS)


def title_similarity(a: str, b: str) -> float:
    """Similarity in [0, 1] combining sequence ratio and token overlap."""
    if not a or not b:
        return 0.0
    seq = SequenceMatcher(None, a, b).ratio()
    ta, tb = set(a.split()), set(b.split())
    jac = len(ta & tb) / len(ta | tb) if ta | tb else 0.0
    return max(seq, jac)


class RecentStore(Protocol):
    async def recent_by_key(self, deal_key: str, since_ts: float) -> Optional[dict[str, Any]]: ...
    async def recent_titles(self, since_ts: float) -> list[dict[str, Any]]: ...


@dataclass
class DedupeResult:
    duplicate: bool
    price_dropped: bool = False
    reason: str = ""


def _price_check(old_price: Optional[float], new_price: Optional[float],
                 cfg: DedupeConfig, what: str) -> DedupeResult:
    if old_price and new_price and new_price <= old_price * (1 - cfg.price_drop_percent / 100):
        return DedupeResult(False, True, f"{what}_but_price_dropped:₹{old_price:g}->₹{new_price:g}")
    return DedupeResult(True, False, what)


async def check_duplicate(store: RecentStore, deal_key: str, title_norm: str,
                          price: Optional[float], now: float, cfg: DedupeConfig) -> DedupeResult:
    """Check posted + pending + queued deals within ``cfg.window_hours``."""
    since = now - cfg.window_hours * 3600
    hit = await store.recent_by_key(deal_key, since)
    if hit:
        return _price_check(hit.get("price"), price, cfg, f"duplicate_key:{deal_key}")
    if title_norm and len(title_norm.split()) >= 3:
        for row in await store.recent_titles(since):
            sim = title_similarity(title_norm, row.get("title_norm") or "")
            if sim >= cfg.title_similarity:
                return _price_check(row.get("price"), price, cfg, f"duplicate_title:{sim:.2f}")
    return DedupeResult(False)
