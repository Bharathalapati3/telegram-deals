"""Build the channel post (Telegram HTML parse mode) from template + deal facts.

The post is written in *our* style from extracted facts only — source text is never
copied, so source branding, @mentions, t.me links and emoji spam never leak through.
"""
from __future__ import annotations

import html
import re
import string
from dataclasses import dataclass
from typing import Callable, Iterable, Optional

from config import StoreDef, TemplateConfig
from models import ConvertedLink, ExtractedInfo, Label

CAPTION_LIMIT = 1024
TEXT_LIMIT = 4096
OPTIONAL_FIELDS_DROP_ORDER = ["bank_offer", "hashtags", "effective_price", "price_drop", "coupon"]


def utf16_len(s: str) -> int:
    """Telegram counts message length in UTF-16 code units (emoji = 2)."""
    return len(s.encode("utf-16-le")) // 2


TAG_RE = re.compile(r"<[^>]+>")


def visible_text(html_text: str) -> str:
    """Text as the user sees it (tags removed, entities unescaped)."""
    return html.unescape(TAG_RE.sub("", html_text))


def visible_len(html_text: str) -> int:
    return utf16_len(visible_text(html_text).strip())


def fmt_inr(value: Optional[float]) -> str:
    """Indian digit grouping: 123456.5 -> '1,23,456.50'; 499.0 -> '499'."""
    if value is None:
        return ""
    whole = int(value)
    frac = round(value - whole, 2)
    s = str(whole)
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        s = ",".join(groups + [tail])
    return s + (f"{frac:.2f}"[1:] if frac else "")


def header_for(label: Label, t: TemplateConfig) -> str:
    return {Label.PRICE_ERROR: t.header_price_error, Label.HOT_DEAL: t.header_hot_deal}.get(
        label, t.header_normal)


def _hashtag(word: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "", word.title() if word.islower() else word)


def render_links(links: Iterable[ConvertedLink], t: TemplateConfig, stores: dict[str, StoreDef]) -> str:
    lines: list[str] = []
    counts: dict[str, int] = {}
    for link in links:
        name = stores[link.store].name if link.store in stores and stores[link.store].name else link.store.title()
        counts[name] = counts.get(name, 0) + 1
        label = name if counts[name] == 1 else f"{name} #{counts[name]}"
        lines.append(t.link_line.format(url=html.escape(link.url, quote=True), store=html.escape(label)))
    return "\n".join(lines)


def _render(template: str, values: dict[str, str]) -> str:
    """Fill placeholders; any line whose placeholders are all empty is dropped."""
    out_lines: list[str] = []
    fmt = string.Formatter()
    for line in template.split("\n"):
        fields = [f for _, f, _, _ in fmt.parse(line) if f]
        if fields and any(not values.get(f, "") for f in fields):
            continue
        out_lines.append(line.format_map(_SafeDict(values)) if fields else line)
    text = "\n".join(out_lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


class _SafeDict(dict):
    def __missing__(self, key: str) -> str:
        return ""


@dataclass
class Rendered:
    html: str
    header_html: str
    body_html: str
    fits_caption: bool


def format_post(*, title: str, info: ExtractedInfo, label: Label, links: list[ConvertedLink],
                store: str, t: TemplateConfig, stores: dict[str, StoreDef],
                price_dropped: bool = False, has_media: bool = False) -> Rendered:
    """Render a post and trim it to Telegram's limits (1024 caption / 4096 text)."""
    store_name = stores[store].name if store in stores else store.title()
    label_tag = {Label.PRICE_ERROR: "#PriceError", Label.HOT_DEAL: "#HotDeal"}.get(label, "")
    hashtags = t.hashtags.format(store=_hashtag(store_name))
    if label_tag:
        hashtags = f"{label_tag} {hashtags}"
    discount = ""
    if info.discount is not None:
        discount = f"{'Up to ' if info.discount_source == 'upto' else ''}{info.discount:g}"
    values = {
        "header": header_for(label, t),
        "price_drop": t.price_drop_line if price_dropped else "",
        "title": html.escape(title or f"Deal on {store_name}"),
        "price": fmt_inr(info.price),
        "mrp": fmt_inr(info.mrp) if info.mrp and info.price and info.mrp > info.price else "",
        "discount": discount,
        "effective_price": fmt_inr(info.effective_price),
        "coupon": html.escape(info.coupon or ""),
        "bank_offer": html.escape(info.bank_offer or ""),
        "links": render_links(links, t, stores),
        "hashtags": html.escape(hashtags),
        "disclosure": t.disclosure,
        "store": html.escape(store_name),
    }
    limit = CAPTION_LIMIT if has_media else TEXT_LIMIT

    def build(vals: dict[str, str]) -> str:
        return _render(t.post, vals)

    text = build(values)
    if visible_len(text) > limit:
        # 1) shorten the title
        over = visible_len(text) - limit
        plain_title = html.unescape(values["title"])
        keep = max(20, len(plain_title) - over - 1)
        if keep < len(plain_title):
            values["title"] = html.escape(plain_title[:keep].rstrip() + "…")
            text = build(values)
        # 2) drop optional lines one by one
        for f in OPTIONAL_FIELDS_DROP_ORDER:
            if visible_len(text) <= limit:
                break
            values[f] = ""
            text = build(values)
    fits = visible_len(text) <= CAPTION_LIMIT
    header_html = values["header"]
    body_html = text[len(header_html):].lstrip("\n") if text.startswith(header_html) else text
    return Rendered(text, header_html, body_html, fits)


def render_deal_over(body_html: str, t: TemplateConfig) -> str:
    """Replace the header with the DEAL OVER banner."""
    return f"{t.deal_over_line}\n{body_html}"


URL_IN_TEXT_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
HREF_RE = re.compile(r"""href\s*=\s*["']([^"']+)["']""", re.IGNORECASE)


def urls_in_post(html_text: str) -> list[str]:
    """All URLs in a post: hrefs + bare URLs in the visible text."""
    urls = [html.unescape(u) for u in HREF_RE.findall(html_text)]
    urls += [u.rstrip(".,!?)") for u in URL_IN_TEXT_RE.findall(visible_text(html_text))]
    return list(dict.fromkeys(urls))


def find_foreign_links(html_text: str, allowed_urls: set[str],
                       is_my_link: Optional[Callable[[str], bool]] = None,
                       own_channel: str = "") -> list[str]:
    """Return URLs that are NOT mine. An empty list means the post is safe to publish."""
    bad: list[str] = []
    own = own_channel.lstrip("@").lower()
    for u in urls_in_post(html_text):
        if u in allowed_urls:
            continue
        if is_my_link and is_my_link(u):
            continue
        if own and re.match(rf"^https?://(?:t\.me|telegram\.me)/{re.escape(own)}/?$", u, re.I):
            continue
        bad.append(u)
    # Any @mention other than our own channel is also a leak of someone else's branding
    for m in re.finditer(r"(?<![\w/])@([A-Za-z0-9_]{4,})", visible_text(html_text)):
        if m.group(1).lower() != own:
            bad.append("@" + m.group(1))
    return bad


def finalize_custom_text(text: str, links: list[ConvertedLink], t: TemplateConfig,
                         stores: dict[str, StoreDef]) -> str:
    """Admin-edited text: add my links if none present and ensure the #ad disclosure."""
    out = text.strip()
    if not urls_in_post(out):
        out += "\n\n" + render_links(links, t, stores)
    if "#ad" not in out.lower():
        out += "\n\n" + t.disclosure
    return out
