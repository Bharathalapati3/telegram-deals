"""Affiliate converters and a registry that picks the right one per store."""
from __future__ import annotations

import json
import logging
import os
from typing import Optional

from config import AppConfig, EnvConfig, StoreDef
from models import ResolvedLink

from .amazon import AmazonConverter
from .base import ConvertResult, Converter, HttpApiConverter, HttpApiSpec
from .cuelinks import CuelinksConverter
from .earnkaro import EarnKaroConverter

log = logging.getLogger(__name__)

__all__ = ["ConvertResult", "Converter", "HttpApiConverter", "HttpApiSpec", "AmazonConverter",
           "EarnKaroConverter", "CuelinksConverter", "ConverterSet", "build_converters",
           "NETWORKS"]


def _spec_from_env(prefix: str, api_url: str, api_key: str, timeout: float) -> HttpApiSpec:
    g = os.environ.get
    extra_raw = g(f"{prefix}_EXTRA_BODY", "").strip()
    try:
        extra = json.loads(extra_raw) if extra_raw else {}
    except json.JSONDecodeError:
        log.error("%s_EXTRA_BODY is not valid JSON — ignored", prefix)
        extra = {}
    return HttpApiSpec(
        api_url=api_url,
        api_key=api_key,
        method=g(f"{prefix}_METHOD", "POST"),
        auth_header=g(f"{prefix}_AUTH_HEADER", "Authorization"),
        auth_value=g(f"{prefix}_AUTH_VALUE", "Bearer {key}"),
        request_field=g(f"{prefix}_REQUEST_FIELD", "url") or "url",
        extra_body=extra,
        response_path=g(f"{prefix}_RESPONSE_PATH", "url") or "url",
        timeout_seconds=timeout,
    )


def _domains(prefix: str, default: str) -> list[str]:
    raw = os.environ.get(f"{prefix}_OUTPUT_DOMAINS", default)
    return [d.strip().lower() for d in raw.split(",") if d.strip()]


def _make_earnkaro(env: EnvConfig, cfg: AppConfig) -> Converter:
    spec = _spec_from_env("EARNKARO", env.earnkaro_api_url, env.earnkaro_api_key,
                          cfg.conversion.timeout_seconds)
    return EarnKaroConverter(spec, _domains("EARNKARO", "ekaro.in"), stores_map=cfg.stores,
                             concurrency=cfg.conversion.concurrency)


def _make_cuelinks(env: EnvConfig, cfg: AppConfig) -> Converter:
    spec = _spec_from_env("CUELINKS", env.cuelinks_api_url, env.cuelinks_api_key,
                          cfg.conversion.timeout_seconds)
    return CuelinksConverter(spec, _domains("CUELINKS", "linksredirect.com"), stores_map=cfg.stores,
                             concurrency=cfg.conversion.concurrency)


#: AFFILIATE_NETWORK value → factory. Register new networks here.
NETWORKS = {
    "earnkaro": _make_earnkaro,
    "cuelinks": _make_cuelinks,
}


class ConverterSet:
    """Routes each store link to the Amazon converter or the configured network."""

    def __init__(self, amazon: AmazonConverter, network: Optional[Converter],
                 stores: dict[str, StoreDef]):
        self.amazon = amazon
        self.network = network
        self.stores = stores

    async def convert(self, link: ResolvedLink) -> ConvertResult:
        """Convert a resolved store link. Never raises."""
        url = link.canonical or link.final
        try:
            if link.store == "amazon":
                return await self.amazon.convert(link.final if link.product_id is None else url)
            if self.network and self.network.supports(link.store):
                return await self.network.convert(url)
        except Exception as exc:  # noqa: BLE001
            log.exception("converter crashed")
            return ConvertResult(url, False, link.store, f"converter error {exc!r}")
        return ConvertResult(url, False, link.store, "no affiliate network configured for this store")

    async def close(self) -> None:
        if self.network:
            await self.network.close()


def build_converters(env: EnvConfig, cfg: AppConfig) -> ConverterSet:
    network: Optional[Converter] = None
    name = env.affiliate_network
    if name and name != "none":
        factory = NETWORKS.get(name)
        if factory is None:
            log.error("Unknown AFFILIATE_NETWORK=%s (known: %s) — non-Amazon links will not be "
                      "converted", name, ", ".join(NETWORKS))
        else:
            network = factory(env, cfg)
    return ConverterSet(AmazonConverter(env.amazon_tag), network, cfg.stores)
