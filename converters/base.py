"""Converter interface shared by all affiliate networks.

A converter turns a *clean* store URL into *my* affiliate URL. See
``docs/ADDING_A_CONVERTER.md`` for how to add a new network.
"""
from __future__ import annotations

import abc
import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Optional

import aiohttp

log = logging.getLogger(__name__)


@dataclass
class ConvertResult:
    """Result of a conversion attempt."""

    url: str                 # converted URL (or the input URL when ok is False)
    ok: bool                 # True only if ``url`` is MY affiliate link
    store: Optional[str]     # store key, e.g. "flipkart"
    note: str = ""           # human-readable reason / info


class Converter(abc.ABC):
    """Base class. Subclasses implement :meth:`convert`."""

    #: short unique name, used in AFFILIATE_NETWORK
    name: str = "base"
    #: store keys this converter handles; ``None`` means "every store except Amazon"
    stores: Optional[set[str]] = None
    #: domains of URLs produced by this converter (used for the final link check)
    output_domains: list[str] = []

    def supports(self, store: Optional[str]) -> bool:
        if not store:
            return False
        if self.stores is None:
            return store != "amazon"
        return store in self.stores

    @abc.abstractmethod
    async def convert(self, url: str) -> ConvertResult:
        """Convert ``url`` into my affiliate link. Must never raise."""

    async def close(self) -> None:  # pragma: no cover - default no-op
        return None


@dataclass
class HttpApiSpec:
    """Describes a simple JSON HTTP conversion API. Everything is configurable via .env.

    ``auth_header`` / ``auth_value`` e.g. ("Authorization", "Bearer {key}").
    ``request_field``: name of the JSON field (POST) or query param (GET) carrying the URL.
    ``extra_body``: extra constant fields to send.
    ``response_path``: dot path into the JSON response where the converted URL is,
    e.g. "data.link" or "data.0.url".
    """

    api_url: str = ""
    api_key: str = ""
    method: str = "POST"
    auth_header: str = "Authorization"
    auth_value: str = "Bearer {key}"
    request_field: str = "url"
    extra_body: dict[str, Any] = field(default_factory=dict)
    response_path: str = "url"
    timeout_seconds: float = 15.0

    @property
    def configured(self) -> bool:
        return bool(self.api_url and self.api_key)


def dig(data: Any, path: str) -> Any:
    """Follow a dot path (``a.b.0.c``) into nested dicts/lists. Returns None if missing."""
    cur = data
    for part in [p for p in path.split(".") if p]:
        if isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return None
        elif isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
        if cur is None:
            return None
    return cur


class HttpApiConverter(Converter):
    """Generic converter for networks with a "give URL, get affiliate URL" JSON API."""

    def __init__(self, spec: HttpApiSpec, stores_map: Any = None, concurrency: int = 3,
                 retries: int = 2, session: Any = None):
        self.spec = spec
        self.stores_map = stores_map   # dict[str, StoreDef] for store detection
        self._session = session
        self._own_session = session is None
        self._sem = asyncio.Semaphore(concurrency)
        self.retries = retries

    async def _get_session(self) -> Any:
        if self._session is None:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.spec.timeout_seconds))
        return self._session

    async def close(self) -> None:
        if self._own_session and self._session is not None:
            await self._session.close()
            self._session = None

    def _store_of(self, url: str) -> Optional[str]:
        from links import detect_store  # local import to avoid a cycle
        return detect_store(url, self.stores_map or {})

    async def convert(self, url: str) -> ConvertResult:
        store = self._store_of(url)
        if not self.spec.configured:
            return ConvertResult(url, False, store, f"{self.name}: API URL/key not configured")
        headers = {"Accept": "application/json"}
        if self.spec.auth_header:
            headers[self.spec.auth_header] = self.spec.auth_value.format(key=self.spec.api_key)
        last = ""
        for attempt in range(self.retries + 1):
            try:
                session = await self._get_session()
                async with self._sem:
                    if self.spec.method.upper() == "GET":
                        params = {self.spec.request_field: url, **self.spec.extra_body}
                        req = session.get(self.spec.api_url, params=params, headers=headers)
                    else:
                        body = {self.spec.request_field: url, **self.spec.extra_body}
                        req = session.post(self.spec.api_url, json=body, headers=headers)
                    async with req as resp:
                        if resp.status in (429,) or resp.status >= 500:
                            last = f"HTTP {resp.status}"
                            raise aiohttp.ClientError(last)
                        if resp.status >= 400:
                            text = (await resp.text())[:200]
                            return ConvertResult(url, False, store, f"{self.name}: HTTP {resp.status} {text}")
                        data = await resp.json(content_type=None)
                converted = dig(data, self.spec.response_path)
                if isinstance(converted, str) and converted.startswith(("http://", "https://")):
                    return ConvertResult(converted, True, store, f"{self.name}: converted")
                return ConvertResult(url, False, store,
                                     f"{self.name}: no URL at '{self.spec.response_path}' in response")
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
                last = repr(exc)
                if attempt < self.retries:
                    await asyncio.sleep(min(8, 2 ** attempt))
            except Exception as exc:  # noqa: BLE001 - never crash the pipeline
                log.exception("%s converter unexpected error", self.name)
                return ConvertResult(url, False, store, f"{self.name}: error {exc!r}")
        return ConvertResult(url, False, store, f"{self.name}: failed after retries ({last})")
