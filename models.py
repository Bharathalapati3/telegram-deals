"""Plain data objects passed between pipeline stages.

Everything here is a dataclass with JSON-friendly fields so that deals can be
persisted in SQLite (pending approvals, post queue) and restored after a restart.
"""
from __future__ import annotations

import dataclasses
import enum
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional


class Label(str, enum.Enum):
    """Final classification of a deal."""

    PRICE_ERROR = "PRICE_ERROR"
    HOT_DEAL = "HOT_DEAL"
    NORMAL = "NORMAL"
    SKIP = "SKIP"


class Action(str, enum.Enum):
    """What the pipeline decided to do with a deal."""

    SKIP = "skip"
    AUTO = "auto"          # straight to the post queue
    APPROVAL = "approval"  # send to admin for a decision


MediaLoader = Callable[[], Awaitable[list[str]]]


@dataclass
class RawMessage:
    """A source message, independent of Telethon (easy to build in tests / /test)."""

    source_id: int
    source_name: str
    message_id: int
    text: str
    urls: list[str] = field(default_factory=list)     # from entities + buttons
    strike_texts: list[str] = field(default_factory=list)  # substrings shown as ~~strikethrough~~
    is_forward: bool = False
    is_edit: bool = False
    has_media: bool = False
    media_loader: Optional[MediaLoader] = None           # downloads photos lazily


@dataclass
class ResolvedLink:
    """A URL after redirect expansion and store identification."""

    original: str
    final: str
    resolved: bool
    store: Optional[str] = None
    product_id: Optional[str] = None   # ASIN for Amazon, else None
    canonical: Optional[str] = None    # clean product URL without tracking params

    @property
    def key(self) -> Optional[str]:
        """Dedupe key: store + ASIN / canonical URL."""
        if not self.store:
            return None
        ident = self.product_id or self.canonical or self.final
        return f"{self.store}:{ident}"


@dataclass
class ConvertedLink:
    """A store link after affiliate conversion."""

    store: str
    url: str           # the URL we will publish
    ok: bool           # True if converted to *my* affiliate link
    note: str = ""


@dataclass
class ExtractedInfo:
    """Facts parsed out of the message text."""

    title: str = ""
    price: Optional[float] = None
    mrp: Optional[float] = None
    effective_price: Optional[float] = None
    explicit_discount: Optional[float] = None
    discount_is_upto: bool = False
    discount: Optional[float] = None      # most reliable value (computed or explicit)
    discount_source: str = ""             # "computed" | "explicit" | "upto" | ""
    coupon: Optional[str] = None
    bank_offer: Optional[str] = None


@dataclass
class FilterResult:
    """Outcome of filtering/scoring."""

    label: Label
    score: float
    reasons: list[str] = field(default_factory=list)
    brand: Optional[str] = None
    brand_allowed: bool = False
    category: Optional[str] = None


@dataclass
class ProcessedDeal:
    """Everything needed to post (or re-render) a deal. JSON-serialisable."""

    source_id: int
    source_name: str
    message_id: int
    deal_key: str
    store: str
    product_id: Optional[str]
    title: str
    title_norm: str
    info: ExtractedInfo
    label: Label
    score: float
    reasons: list[str]
    links: list[ConvertedLink]
    all_converted: bool
    brand_allowed: bool
    price_dropped: bool = False
    action: Action = Action.SKIP
    html: str = ""                 # rendered post (HTML parse mode)
    header_html: str = ""
    body_html: str = ""
    fits_caption: bool = True
    media_paths: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Serialise to plain JSON-compatible dict."""
        d = dataclasses.asdict(self)
        d["label"] = self.label.value
        d["action"] = self.action.value
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ProcessedDeal":
        """Inverse of :meth:`to_dict`."""
        d = dict(d)
        d["info"] = ExtractedInfo(**d["info"])
        d["links"] = [ConvertedLink(**x) for x in d["links"]]
        d["label"] = Label(d["label"])
        d["action"] = Action(d["action"])
        return cls(**d)


@dataclass
class PipelineResult:
    """Result of running a message through the pipeline (also used by /test)."""

    action: Action
    reasons: list[str]
    deal: Optional[ProcessedDeal] = None
    resolved_links: list[ResolvedLink] = field(default_factory=list)
    label: Label = Label.SKIP
    score: float = 0.0
