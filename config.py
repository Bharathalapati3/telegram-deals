"""Configuration loading.

* Secrets and per-install values come from ``.env`` (see ``.env.example``).
* Lists, thresholds and templates come from ``config.yaml`` (see ``config.example.yaml``).
* Some values can be changed at runtime with admin commands; those overrides are
  stored in the SQLite ``settings`` table and layered on top by :class:`Settings`.
"""
from __future__ import annotations

import copy
import dataclasses
import logging
import os
import typing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Protocol

import yaml
from dotenv import load_dotenv

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------- defaults

DEFAULT_SHORTENERS: list[str] = [
    "amzn.to", "amzn.in", "amzn.eu", "a.co",
    "fkrt.it", "fkrt.cc", "fkrt.co", "fktr.in", "dl.flipkart.com",
    "bit.ly", "bitli.in", "cutt.ly", "tinyurl.com", "t.ly", "rb.gy", "is.gd", "shorturl.at",
    "ow.ly", "goo.gl", "tiny.cc", "clnk.in",
    "ekaro.in", "ekaro.co", "linkredirect.in", "linksredirect.com",
    "myntr.it", "ajiio.in", "ajio.me", "msho.in", "meesho.page.link",
    "nykaa.me", "bitly.com", "shrsl.com", "inr.deals", "fas.st", "dealsmagnet.com",
    "wishlink.com", "extp.in", "earnkaro.com",
]

DEFAULT_STORES: dict[str, dict[str, Any]] = {
    "amazon": {"name": "Amazon", "domains": ["amazon.in"]},
    "flipkart": {"name": "Flipkart", "domains": ["flipkart.com"], "keep_params": ["pid", "lid", "marketplace"]},
    "shopsy": {"name": "Shopsy", "domains": ["shopsy.in"], "keep_params": ["pid", "lid"]},
    "myntra": {"name": "Myntra", "domains": ["myntra.com"], "keep_params": []},
    "ajio": {"name": "Ajio", "domains": ["ajio.com"], "keep_params": []},
    "meesho": {"name": "Meesho", "domains": ["meesho.com"], "keep_params": []},
    "nykaa": {"name": "Nykaa", "domains": ["nykaa.com", "nykaafashion.com", "nykaaman.com"],
              "keep_params": ["productId", "skuId"]},
    "croma": {"name": "Croma", "domains": ["croma.com"], "keep_params": []},
    "tatacliq": {"name": "Tata CLiQ", "domains": ["tatacliq.com", "luxury.tatacliq.com"], "keep_params": []},
    "jiomart": {"name": "JioMart", "domains": ["jiomart.com"], "keep_params": []},
    "reliancedigital": {"name": "Reliance Digital", "domains": ["reliancedigital.in"], "keep_params": []},
    "firstcry": {"name": "FirstCry", "domains": ["firstcry.com"], "keep_params": []},
    "bigbasket": {"name": "BigBasket", "domains": ["bigbasket.com"], "keep_params": []},
    "boat": {"name": "boAt", "domains": ["boat-lifestyle.com"], "keep_params": ["variant"]},
}

DEFAULT_TEMPLATE = """{header}
{price_drop}

🛍 <b>{title}</b>

💰 Deal Price: <b>₹{price}</b>
🏷 MRP: <s>₹{mrp}</s>
📉 <b>{discount}% OFF</b>
✨ Effective: ₹{effective_price}
🎟 Coupon: <code>{coupon}</code>
💳 {bank_offer}

{links}

{hashtags}
{disclosure}"""

UA_DEFAULT = (
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Mobile Safari/537.36"
)


# --------------------------------------------------------------------------- sections

@dataclass
class ReaderConfig:
    process_edits: bool = True
    auto_join: bool = True
    catch_up: bool = False
    max_concurrent: int = 5
    ignore_forwards: bool = True


@dataclass
class ExpansionConfig:
    shortener_domains: list[str] = field(default_factory=lambda: list(DEFAULT_SHORTENERS))
    extra_shortener_domains: list[str] = field(default_factory=list)
    timeout_seconds: float = 10.0
    max_redirects: int = 8
    concurrency: int = 5
    retries: int = 2
    user_agent: str = UA_DEFAULT
    cache_ttl_hours: int = 168
    failed_cache_ttl_minutes: int = 60
    expand_unknown_domains: bool = True

    @property
    def all_shorteners(self) -> list[str]:
        return list(dict.fromkeys([*self.shortener_domains, *self.extra_shortener_domains]))


@dataclass
class StoreDef:
    name: str = ""
    domains: list[str] = field(default_factory=list)
    keep_params: Optional[list[str]] = None   # None = strip known tracking params only


@dataclass
class FilterConfig:
    min_discount: float = 50.0
    hot_deal_min_discount: float = 70.0
    hot_deal_min_score: float = 85.0
    categories: dict[str, list[str]] = field(default_factory=lambda: {
        "electronics": ["laptop", "smartphone", "mobile", "phone", "tv", "television", "monitor",
                        "tablet", "smartwatch", "earbuds", "headphone", "speaker", "camera", "ssd",
                        "processor", "router", "printer", "refrigerator", "washing machine", "ac "],
        "fashion": ["t-shirt", "tshirt", "shirt", "jeans", "kurta", "saree", "sneakers", "shoes",
                    "sandals", "dress", "jacket", "trousers", "watch for men", "handbag"],
        "grocery": ["atta", "rice", "dal", "oil", "ghee", "tea", "coffee", "biscuit", "snacks"],
        "beauty": ["serum", "lipstick", "face wash", "shampoo", "sunscreen", "moisturizer", "perfume"],
    })
    category_min_discount: dict[str, float] = field(default_factory=lambda: {
        "electronics": 35, "fashion": 65, "grocery": 40, "beauty": 50,
    })
    brand_allowlist: list[str] = field(default_factory=list)
    brand_blocklist: list[str] = field(default_factory=list)
    keyword_blocklist: list[str] = field(default_factory=lambda: [
        "refurbished", "renewed", "used", "pre-owned", "preowned", "open box", "second hand",
        "condom", "lingerie", "adult", "betting", "casino", "crypto", "rummy", "loan app",
    ])
    ad_keywords: list[str] = field(default_factory=lambda: [
        "paid promotion", "sponsored", "advertisement", "promoted post", "#promo", "join our vip",
        "telegram premium", "investment", "earn daily", "trading signals",
    ])
    unknown_discount: str = "allow"   # allow | skip
    max_products_per_post: int = 3


@dataclass
class PriceErrorConfig:
    strong_keywords: list[str] = field(default_factory=lambda: [
        "price error", "pricing error", "price glitch", "glitch", "price bug", "bug deal",
        "pricing mistake", "price mistake",
    ])
    weak_keywords: list[str] = field(default_factory=lambda: [
        "loot", "steal", "big loot", "super loot", "maha loot", "crazy deal",
    ])
    min_discount: float = 90.0
    max_absurd_price: float = 9.0
    absurd_min_mrp: float = 200.0
    max_price_ratio: float = 0.10


@dataclass
class DedupeConfig:
    window_hours: float = 24.0
    price_drop_percent: float = 10.0
    title_similarity: float = 0.85


@dataclass
class ConversionConfig:
    on_fail: str = "approval"    # skip | approval | clean_link
    timeout_seconds: float = 15.0
    concurrency: int = 3


@dataclass
class PostingConfig:
    mode: str = "mixed"                  # auto | approval | mixed
    max_posts_per_hour: int = 8
    min_gap_seconds: int = 300
    price_error_bypass_rate_limit: bool = True
    queue_max_age_minutes: int = 240
    price_error_max_age_minutes: int = 45
    quiet_hours_enabled: bool = True
    quiet_hours_start: str = "01:00"
    quiet_hours_end: str = "07:00"
    auto_min_score: float = 75.0
    require_brand_allowlist: bool = True
    price_errors_need_approval: bool = False
    link_preview: bool = False
    max_album_photos: int = 4
    notify_admin_on_post: bool = True


@dataclass
class ApprovalConfig:
    expiry_minutes: int = 120
    price_error_expiry_minutes: int = 20


@dataclass
class HealthConfig:
    daily_summary_time: str = "22:00"
    no_messages_alert_hours: float = 3.0
    error_alert_threshold: int = 5
    error_alert_window_minutes: int = 10


@dataclass
class TemplateConfig:
    header_price_error: str = "⚠️ <b>PRICE ERROR — may get cancelled, order fast!</b>"
    header_hot_deal: str = "🔥 <b>HOT DEAL</b>"
    header_normal: str = "✅ <b>Deal</b>"
    price_drop_line: str = "📉 <i>Price dropped further!</i>"
    deal_over_line: str = "❌ <b>DEAL OVER</b> — expired / out of stock"
    link_line: str = '👉 <a href="{url}">Buy on {store}</a>'
    disclosure: str = "#ad — we may earn a commission"
    hashtags: str = "#{store} #deals #India"
    post: str = DEFAULT_TEMPLATE


@dataclass
class AppConfig:
    sources: list[str] = field(default_factory=list)
    #: Stores whose deals we post. Start with Amazon only; add e.g. "flipkart" once an
    #: affiliate network converter (AFFILIATE_NETWORK) is configured for it.
    enabled_stores: list[str] = field(default_factory=lambda: ["amazon"])
    reader: ReaderConfig = field(default_factory=ReaderConfig)
    expansion: ExpansionConfig = field(default_factory=ExpansionConfig)
    stores: dict[str, StoreDef] = field(default_factory=lambda: {
        k: StoreDef(**v) for k, v in DEFAULT_STORES.items()})
    filters: FilterConfig = field(default_factory=FilterConfig)
    price_error: PriceErrorConfig = field(default_factory=PriceErrorConfig)
    dedupe: DedupeConfig = field(default_factory=DedupeConfig)
    conversion: ConversionConfig = field(default_factory=ConversionConfig)
    posting: PostingConfig = field(default_factory=PostingConfig)
    approvals: ApprovalConfig = field(default_factory=ApprovalConfig)
    health: HealthConfig = field(default_factory=HealthConfig)
    templates: TemplateConfig = field(default_factory=TemplateConfig)


@dataclass
class EnvConfig:
    """Values from ``.env``."""

    api_id: int = 0
    api_hash: str = ""
    phone: str = ""
    bot_token: str = ""
    admin_ids: list[int] = field(default_factory=list)
    target_channel: str = ""
    amazon_tag: str = ""
    affiliate_network: str = "none"
    earnkaro_api_url: str = ""
    earnkaro_api_key: str = ""
    cuelinks_api_url: str = ""
    cuelinks_api_key: str = ""
    data_dir: Path = Path("data")
    user_session: str = "user"
    bot_session: str = "bot"
    log_level: str = "INFO"
    log_format: str = "text"
    timezone: str = "Asia/Kolkata"

    @property
    def media_dir(self) -> Path:
        return self.data_dir / "media"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "deals.db"

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"

    def session_path(self, name: str) -> str:
        return str(self.data_dir / name)


# --------------------------------------------------------------------------- loading

def _build(cls: type, data: Any, path: str = "") -> Any:
    """Recursively build dataclass ``cls`` from a dict, warning about unknown keys."""
    if data is None:
        return cls()
    if not isinstance(data, dict):
        raise ValueError(f"config section '{path or cls.__name__}' must be a mapping")
    hints = typing.get_type_hints(cls)
    kwargs: dict[str, Any] = {}
    names = {f.name for f in dataclasses.fields(cls)}
    for key, value in data.items():
        if key not in names:
            log.warning("Unknown config key ignored: %s%s", f"{path}." if path else "", key)
            continue
        hint = hints[key]
        kwargs[key] = _coerce(hint, value, f"{path}.{key}" if path else key)
    return cls(**kwargs)


def _coerce(hint: Any, value: Any, path: str) -> Any:
    origin = typing.get_origin(hint)
    args = typing.get_args(hint)
    if dataclasses.is_dataclass(hint):
        return _build(hint, value, path)
    if origin is dict and args and dataclasses.is_dataclass(args[1]):
        return {k: _build(args[1], v, f"{path}.{k}") for k, v in (value or {}).items()}
    if origin is typing.Union and type(None) in args:   # Optional[X]
        return None if value is None else _coerce(args[0], value, path)
    if origin is list:
        return list(value or [])
    if origin is dict:
        return dict(value or {})
    if hint is bool and isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    if hint in (int, float, str) and value is not None:
        return hint(value)
    return value


def load_app_config(path: str | Path = "config.yaml") -> AppConfig:
    """Load ``config.yaml``; missing file → all defaults. User stores merge with defaults."""
    p = Path(path)
    data: dict[str, Any] = {}
    if p.exists():
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    else:
        log.warning("%s not found — using built-in defaults", p)
    stores_raw = data.pop("stores", None) or {}
    cfg: AppConfig = _build(AppConfig, data)
    for key, val in stores_raw.items():
        base = dataclasses.asdict(cfg.stores[key]) if key in cfg.stores else {}
        base.update(val or {})
        cfg.stores[key] = _build(StoreDef, base, f"stores.{key}")
    unknown = [s for s in cfg.enabled_stores if s not in cfg.stores]
    if unknown:
        raise ValueError(f"enabled_stores has unknown store keys: {unknown}")
    if cfg.posting.mode not in {"auto", "approval", "mixed"}:
        raise ValueError("posting.mode must be auto, approval or mixed")
    if cfg.conversion.on_fail not in {"skip", "approval", "clean_link"}:
        raise ValueError("conversion.on_fail must be skip, approval or clean_link")
    return cfg


def load_env(env_file: str | Path = ".env") -> EnvConfig:
    """Read ``.env`` (if present) plus real environment variables."""
    load_dotenv(env_file, override=False)
    g = os.environ.get
    admin_ids = [int(x) for x in g("ADMIN_IDS", "").replace(" ", "").split(",") if x]
    return EnvConfig(
        api_id=int(g("API_ID", "0") or 0),
        api_hash=g("API_HASH", ""),
        phone=g("PHONE", ""),
        bot_token=g("BOT_TOKEN", ""),
        admin_ids=admin_ids,
        target_channel=g("TARGET_CHANNEL", ""),
        amazon_tag=g("AMAZON_TAG", ""),
        affiliate_network=(g("AFFILIATE_NETWORK", "none") or "none").lower(),
        earnkaro_api_url=g("EARNKARO_API_URL", ""),
        earnkaro_api_key=g("EARNKARO_API_KEY", ""),
        cuelinks_api_url=g("CUELINKS_API_URL", ""),
        cuelinks_api_key=g("CUELINKS_API_KEY", ""),
        data_dir=Path(g("DATA_DIR", "data")),
        user_session=g("USER_SESSION", "user"),
        bot_session=g("BOT_SESSION", "bot"),
        log_level=g("LOG_LEVEL", "INFO"),
        log_format=g("LOG_FORMAT", "text"),
        timezone=g("TZ_NAME", "Asia/Kolkata"),
    )


def validate_env(env: EnvConfig) -> list[str]:
    """Return a list of human-readable problems (empty = OK)."""
    problems = []
    if not env.api_id or not env.api_hash:
        problems.append("API_ID / API_HASH missing (get them from https://my.telegram.org)")
    if not env.bot_token:
        problems.append("BOT_TOKEN missing (create a bot with @BotFather)")
    if not env.admin_ids:
        problems.append("ADMIN_IDS missing (your numeric Telegram user id, e.g. from @userinfobot)")
    if not env.target_channel:
        problems.append("TARGET_CHANNEL missing (e.g. @mychannel or -100123...)")
    if not env.amazon_tag:
        problems.append("AMAZON_TAG missing (your Amazon Associates tag, e.g. yourtag-21)")
    return problems


# --------------------------------------------------------------------------- runtime overlay

class SettingsStore(Protocol):
    async def get_all_settings(self) -> dict[str, Any]: ...
    async def set_setting(self, key: str, value: Any) -> None: ...


class Settings:
    """Config + env + runtime overrides (set by admin commands, persisted in SQLite).

    Override keys: ``mode``, ``paused``, ``min_discount``, ``enabled_stores``, ``extra_sources``,
    ``removed_sources``, ``extra_blocked``, ``extra_allowed``.
    """

    def __init__(self, cfg: AppConfig, env: EnvConfig, store: Optional[SettingsStore] = None):
        self.cfg = cfg
        self.env = env
        self.store = store
        self.overrides: dict[str, Any] = {}

    async def load_overrides(self) -> None:
        if self.store:
            self.overrides = await self.store.get_all_settings()

    async def set(self, key: str, value: Any) -> None:
        self.overrides[key] = value
        if self.store:
            await self.store.set_setting(key, value)

    # -- simple values
    @property
    def mode(self) -> str:
        return self.overrides.get("mode", self.cfg.posting.mode)

    @property
    def paused(self) -> bool:
        return bool(self.overrides.get("paused", False))

    @property
    def min_discount(self) -> float:
        return float(self.overrides.get("min_discount", self.cfg.filters.min_discount))

    # -- lists
    @property
    def enabled_stores(self) -> list[str]:
        """Stores we post deals for (config ``enabled_stores`` or /enablestore override)."""
        return list(self.overrides.get("enabled_stores", self.cfg.enabled_stores))

    async def enable_store(self, store: str) -> None:
        if store not in self.cfg.stores:
            raise ValueError(f"unknown store '{store}'. Known: {', '.join(self.cfg.stores)}")
        await self.set("enabled_stores", list(dict.fromkeys([*self.enabled_stores, store])))

    async def disable_store(self, store: str) -> None:
        await self.set("enabled_stores", [s for s in self.enabled_stores if s != store])

    @property
    def sources(self) -> list[str]:
        removed = {s.lower() for s in self.overrides.get("removed_sources", [])}
        merged = [*self.cfg.sources, *self.overrides.get("extra_sources", [])]
        return [s for s in dict.fromkeys(merged) if s.lower() not in removed]

    async def add_source(self, src: str) -> None:
        extra = list(self.overrides.get("extra_sources", []))
        removed = [s for s in self.overrides.get("removed_sources", []) if s.lower() != src.lower()]
        if src not in extra and src not in self.cfg.sources:
            extra.append(src)
        await self.set("extra_sources", extra)
        await self.set("removed_sources", removed)

    async def remove_source(self, src: str) -> None:
        extra = [s for s in self.overrides.get("extra_sources", []) if s.lower() != src.lower()]
        removed = list(self.overrides.get("removed_sources", []))
        if src.lower() not in {s.lower() for s in removed}:
            removed.append(src)
        await self.set("extra_sources", extra)
        await self.set("removed_sources", removed)

    async def block(self, word: str) -> None:
        word = word.strip().lower()
        blocked = list(dict.fromkeys([*self.overrides.get("extra_blocked", []), word]))
        allowed = [a for a in self.overrides.get("extra_allowed", []) if a != word]
        await self.set("extra_blocked", blocked)
        await self.set("extra_allowed", allowed)

    async def allow(self, brand: str) -> None:
        brand = brand.strip().lower()
        allowed = list(dict.fromkeys([*self.overrides.get("extra_allowed", []), brand]))
        blocked = [b for b in self.overrides.get("extra_blocked", []) if b != brand]
        await self.set("extra_allowed", allowed)
        await self.set("extra_blocked", blocked)

    def filter_config(self) -> FilterConfig:
        """A copy of the filter config with runtime overrides applied."""
        fc = copy.deepcopy(self.cfg.filters)
        fc.min_discount = self.min_discount
        extra_allowed = self.overrides.get("extra_allowed", [])
        extra_blocked = self.overrides.get("extra_blocked", [])
        fc.brand_allowlist = list(dict.fromkeys(
            [b.lower() for b in fc.brand_allowlist if b.lower() not in extra_blocked] + extra_allowed))
        fc.brand_blocklist = [b.lower() for b in fc.brand_blocklist if b.lower() not in extra_allowed]
        fc.keyword_blocklist = list(dict.fromkeys(
            [k.lower() for k in fc.keyword_blocklist if k.lower() not in extra_allowed] + extra_blocked))
        return fc
