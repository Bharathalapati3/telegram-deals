"""Link handling: expand short links, detect store, extract ASIN, canonicalise, strip tracking.

We only follow HTTP redirects (and simple meta/JS redirects on shortener pages).
We never download or scrape Amazon/Flipkart product pages: expansion stops as soon
as the URL points at a known store domain.
"""
from __future__ import annotations

import asyncio
import logging
import random
import re
from typing import Any, Optional, Protocol
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

import aiohttp

from config import ExpansionConfig, StoreDef
from models import ResolvedLink

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------- params

#: Query params that are tracking or affiliate identifiers. Matched case-insensitively.
TRACKING_PARAM_PATTERNS: list[re.Pattern[str]] = [re.compile(p, re.I) for p in [
    r"^utm_.*", r"^affid$", r"^affextparam\d*$", r"^aff_?id$", r"^aff$", r"^aff_.*", r"^affiliate.*",
    r"^tag$", r"^ascsubtag$", r"^linkcode$", r"^linkid$", r"^camp$", r"^creative$", r"^creativeasin$",
    r"^ref$", r"^ref_$", r"^refrid$", r"^referrer$", r"^pd_rd_.*", r"^pf_rd_.*", r"^_encoding$",
    r"^psc$", r"^smid$", r"^th$", r"^sr$", r"^qid$", r"^sprefix$", r"^keywords$", r"^crid$",
    r"^dib.*", r"^content-id$", r"^sp_csd$", r"^spla$", r"^fbclid$", r"^gclid$", r"^igshid$",
    r"^mc_.*", r"^cmpid$", r"^otracker.*", r"^ssid$", r"^iid$", r"^ppt$", r"^ppn$", r"^srno$",
    r"^source$", r"^src$", r"^share.*", r"^si$", r"^subid.*", r"^sub_?id\d*$", r"^clickid$",
    r"^click_id$", r"^cid$", r"^campaign.*", r"^medium$", r"^gad_.*", r"^_branch.*", r"^deeplink$",
    r"^dl$", r"^ekaro.*", r"^wishlink.*", r"^partner.*", r"^pid_?ref$", r"^irclickid$", r"^ranmid$",
    r"^raneaid$", r"^ransiteid$", r"^siteid$", r"^offer_id$", r"^transaction_id$",
]]

#: Params that indicate *someone's* affiliate/referral identity (never allowed through).
AFFILIATE_PARAM_PATTERNS: list[re.Pattern[str]] = [re.compile(p, re.I) for p in [
    r"^tag$", r"^ascsubtag$", r"^affid$", r"^affextparam\d*$", r"^aff_?id$", r"^aff$", r"^aff_.*",
    r"^affiliate.*", r"^ref(?:errer|code|_?id)?$", r"^refrid$", r"^subid.*", r"^sub_?id\d*$",
    r"^partner.*", r"^clickid$", r"^click_id$", r"^irclickid$", r"^ranmid$", r"^raneaid$",
    r"^ransiteid$", r"^siteid$", r"^utm_source$", r"^utm_medium$", r"^utm_campaign$", r"^cid$",
    r"^invite.*", r"^referral.*",
]]

ASIN_PATTERNS: list[re.Pattern[str]] = [re.compile(p, re.I) for p in [
    r"/dp/(?:product/)?([A-Z0-9]{10})(?:[/?#]|$)",
    r"/gp/product/(?:glance/)?([A-Z0-9]{10})(?:[/?#]|$)",
    r"/gp/aw/d/([A-Z0-9]{10})(?:[/?#]|$)",
    r"/gp/offer-listing/([A-Z0-9]{10})(?:[/?#]|$)",
    r"/exec/obidos/(?:tg/detail/-/|ASIN/)([A-Z0-9]{10})(?:[/?#]|$)",
    r"/o/ASIN/([A-Z0-9]{10})(?:[/?#]|$)",
    r"/product-reviews/([A-Z0-9]{10})(?:[/?#]|$)",
    r"/d/([A-Z0-9]{10})(?:[/?#]|$)",
]]
ASIN_QUERY_KEYS = ("asin", "ASIN", "pd_rd_i", "creativeASIN")
ASIN_VALID_RE = re.compile(r"^(?:B0[A-Z0-9]{8}|\d{9}[\dX])$")


def host_of(url: str) -> str:
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def domain_matches(host: str, domains: list[str]) -> bool:
    host = host.lower()
    return any(host == d or host.endswith("." + d) for d in domains)


def detect_store(url: str, stores: dict[str, StoreDef]) -> Optional[str]:
    """Return the store key for a URL (e.g. ``amazon``) or None."""
    host = host_of(url)
    if not host:
        return None
    for key, sd in stores.items():
        if domain_matches(host, sd.domains):
            return key
    return None


def extract_asin(url: str) -> Optional[str]:
    """Extract an Amazon ASIN from any common URL shape; None if not found."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    path = unquote(parts.path or "")
    for pat in ASIN_PATTERNS:
        m = pat.search(path)
        if m:
            asin = m.group(1).upper()
            if ASIN_VALID_RE.match(asin):
                return asin
    params = dict(parse_qsl(parts.query, keep_blank_values=True))
    for key in ASIN_QUERY_KEYS:
        val = (params.get(key) or "").upper()
        if ASIN_VALID_RE.match(val):
            return val
    return None


def is_tracking_param(name: str) -> bool:
    return any(p.match(name) for p in TRACKING_PARAM_PATTERNS)


def is_affiliate_param(name: str) -> bool:
    return any(p.match(name) for p in AFFILIATE_PARAM_PATTERNS)


def strip_tracking(url: str, keep_params: Optional[list[str]] = None) -> str:
    """Remove tracking / affiliate params. If ``keep_params`` is given, keep only those."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    query = parse_qsl(parts.query, keep_blank_values=True)
    if keep_params is not None:
        keep = {k.lower() for k in keep_params}
        query = [(k, v) for k, v in query if k.lower() in keep]
    else:
        query = [(k, v) for k, v in query if not is_tracking_param(k)]
    path = parts.path
    # Amazon-style "/ref=xyz" path segments carry tracking info
    path = re.sub(r"/ref=[^/]*$", "", path)
    return urlunsplit((parts.scheme or "https", parts.netloc.lower(), path, urlencode(query), ""))


def canonicalize(url: str, store: Optional[str], stores: dict[str, StoreDef]) -> tuple[Optional[str], str]:
    """Return (product_id, canonical_url) for a store URL."""
    if store == "amazon":
        asin = extract_asin(url)
        if asin:
            return asin, f"https://www.amazon.in/dp/{asin}"
        return None, strip_tracking(url)
    keep = stores[store].keep_params if store and store in stores else None
    return None, strip_tracking(url, keep)


def affiliate_params_in(url: str) -> list[str]:
    """List the affiliate/referral params present in a URL."""
    try:
        q = parse_qsl(urlsplit(url).query, keep_blank_values=True)
    except ValueError:
        return []
    return [k for k, _ in q if is_affiliate_param(k)]


def embedded_target(url: str, stores: dict[str, StoreDef]) -> Optional[str]:
    """Some redirectors carry the destination in a query param; return it if it is a store URL."""
    try:
        q = parse_qsl(urlsplit(url).query, keep_blank_values=True)
    except ValueError:
        return None
    for _, v in q:
        for candidate in (v, unquote(v)):
            if candidate.startswith(("http://", "https://")) and detect_store(candidate, stores):
                return candidate
    return None


# --------------------------------------------------------------------------- cache protocol

class LinkCache(Protocol):
    async def get_link_cache(self, url: str) -> Optional[tuple[str, bool]]: ...
    async def put_link_cache(self, url: str, final_url: str, ok: bool) -> None: ...


META_REFRESH_RE = re.compile(
    r"""<meta[^>]+http-equiv=["']?refresh["']?[^>]*content=["']?\s*\d+\s*;\s*url=([^"'>\s]+)""", re.I)
JS_REDIRECT_RE = re.compile(
    r"""(?:window\.)?location(?:\.href)?\s*(?:=|\.replace\(|\.assign\()\s*["']([^"']+)["']""", re.I)


class Expander:
    """Resolve short links by following redirects with limits, retries and caching."""

    def __init__(self, cfg: ExpansionConfig, stores: dict[str, StoreDef],
                 cache: Optional[LinkCache] = None, session: Any = None):
        self.cfg = cfg
        self.stores = stores
        self.cache = cache
        self._session = session
        self._own_session = session is None
        self._sem = asyncio.Semaphore(cfg.concurrency)
        self._inflight: dict[str, asyncio.Future[tuple[str, bool]]] = {}

    async def _get_session(self) -> Any:
        if self._session is None:
            timeout = aiohttp.ClientTimeout(total=self.cfg.timeout_seconds)
            self._session = aiohttp.ClientSession(
                timeout=timeout,
                headers={"User-Agent": self.cfg.user_agent,
                         "Accept-Language": "en-IN,en;q=0.9",
                         "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"},
            )
        return self._session

    async def close(self) -> None:
        if self._own_session and self._session is not None:
            await self._session.close()
            self._session = None

    def is_shortener(self, url: str) -> bool:
        return domain_matches(host_of(url), self.cfg.all_shorteners)

    def needs_expansion(self, url: str) -> bool:
        if detect_store(url, self.stores) and not self.is_shortener(url):
            return False
        return self.is_shortener(url) or self.cfg.expand_unknown_domains

    async def resolve(self, url: str) -> ResolvedLink:
        """Expand (if needed) and identify a URL. Never raises."""
        final, ok = url, True
        if self.needs_expansion(url):
            try:
                final, ok = await self.expand(url)
            except Exception as exc:  # noqa: BLE001 - must never crash the pipeline
                log.warning("expand failed url=%s err=%r", url, exc)
                final, ok = url, False
        store = detect_store(final, self.stores)
        product_id, canonical = canonicalize(final, store, self.stores) if store else (None, None)
        return ResolvedLink(original=url, final=final, resolved=ok, store=store,
                            product_id=product_id, canonical=canonical)

    async def expand(self, url: str) -> tuple[str, bool]:
        """Return (final_url, ok). Uses cache and de-duplicates concurrent requests."""
        if self.cache:
            cached = await self.cache.get_link_cache(url)
            if cached:
                return cached
        if url in self._inflight:
            return await self._inflight[url]
        fut: asyncio.Future[tuple[str, bool]] = asyncio.get_running_loop().create_future()
        self._inflight[url] = fut
        try:
            result = await self._expand_with_retries(url)
            if self.cache:
                await self.cache.put_link_cache(url, result[0], result[1])
            fut.set_result(result)
            return result
        except Exception as exc:  # noqa: BLE001
            fut.set_result((url, False))
            log.warning("expand error url=%s err=%r", url, exc)
            return url, False
        finally:
            self._inflight.pop(url, None)

    async def _expand_with_retries(self, url: str) -> tuple[str, bool]:
        last_exc: Optional[BaseException] = None
        for attempt in range(self.cfg.retries + 1):
            try:
                async with self._sem:
                    return await self._follow(url)
            except (aiohttp.ClientError, asyncio.TimeoutError, _RetryableStatus) as exc:
                last_exc = exc
                if attempt < self.cfg.retries:
                    delay = min(8.0, (2 ** attempt)) + random.uniform(0, 0.5)
                    log.debug("expand retry %s/%s url=%s err=%r", attempt + 1, self.cfg.retries, url, exc)
                    await asyncio.sleep(delay)
        log.info("expand gave up url=%s err=%r", url, last_exc)
        return url, False

    async def _follow(self, url: str) -> tuple[str, bool]:
        session = await self._get_session()
        current = url
        for _ in range(self.cfg.max_redirects + 1):
            # Stop as soon as we reach a store page (no product-page scraping).
            if detect_store(current, self.stores) and not self.is_shortener(current):
                return current, True
            target = embedded_target(current, self.stores)
            if target:
                return target, True
            async with session.get(current, allow_redirects=False,
                                   timeout=aiohttp.ClientTimeout(total=self.cfg.timeout_seconds)) as resp:
                status = resp.status
                if status in (301, 302, 303, 307, 308):
                    loc = resp.headers.get("Location")
                    if not loc:
                        return current, False
                    current = _join(current, loc)
                    continue
                if status >= 500 or status == 429:
                    raise _RetryableStatus(status)
                if status == 200 and "html" in (resp.headers.get("Content-Type") or "").lower():
                    # Shortener landing pages sometimes use meta-refresh / JS redirects.
                    body = (await resp.content.read(65536)).decode("utf-8", "ignore")
                    nxt = _html_redirect(body)
                    if nxt and nxt != current:
                        current = _join(current, nxt)
                        continue
                ok = status < 400 and (detect_store(current, self.stores) is not None
                                       or not self.is_shortener(current))
                return current, ok
        log.info("too many redirects url=%s", url)
        return current, detect_store(current, self.stores) is not None


class _RetryableStatus(Exception):
    def __init__(self, status: int):
        super().__init__(f"HTTP {status}")
        self.status = status


def _join(base: str, loc: str) -> str:
    from urllib.parse import urljoin
    return urljoin(base, loc.strip())


def _html_redirect(body: str) -> Optional[str]:
    for rx in (META_REFRESH_RE, JS_REDIRECT_RE):
        m = rx.search(body)
        if m and m.group(1).startswith(("http://", "https://", "/")):
            return m.group(1).replace("&amp;", "&")
    return None
