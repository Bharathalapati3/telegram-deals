"""The deal pipeline: one RawMessage in → one PipelineResult out.

Used both by the live reader and by the admin ``/test`` command (``dry_run=True``),
so what you test is exactly what runs in production.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable, Optional

from config import Settings
from converters import ConverterSet
from db import Database
from dedupe import check_duplicate, normalize_title
from extract import extract_info, extract_urls, normalize_text
from filters import evaluate, is_ad
from formatter import find_foreign_links, format_post
from links import Expander
from models import Action, ConvertedLink, Label, PipelineResult, ProcessedDeal, RawMessage, ResolvedLink

log = logging.getLogger(__name__)

MAX_URLS_PER_MESSAGE = 10


class Pipeline:
    def __init__(self, settings: Settings, db: Database, expander: Expander,
                 converters: ConverterSet, clock: Callable[[], float] = time.time):
        self.settings = settings
        self.db = db
        self.expander = expander
        self.converters = converters
        self.clock = clock

    # ------------------------------------------------------------------ helpers
    async def _record(self, raw: RawMessage, result: PipelineResult, dry_run: bool) -> None:
        if dry_run:
            return
        d = result.deal
        try:
            await self.db.record_seen(
                source_id=raw.source_id, source_name=raw.source_name, message_id=raw.message_id,
                deal_key=d.deal_key if d else "", title=d.title if d else "",
                price=d.info.price if d else None, mrp=d.info.mrp if d else None,
                discount=d.info.discount if d else None, label=result.label.value,
                score=result.score, action=result.action.value, reasons=result.reasons)
            if result.action != Action.SKIP:
                await self.db.bump_stat(raw.source_id, raw.source_name, "passed")
        except Exception:  # noqa: BLE001
            log.exception("failed to record deal")

    def _skip(self, reasons: list[str], resolved: Optional[list[ResolvedLink]] = None,
              label: Label = Label.SKIP, score: float = 0.0,
              deal: Optional[ProcessedDeal] = None) -> PipelineResult:
        return PipelineResult(Action.SKIP, reasons, deal=deal, resolved_links=resolved or [],
                              label=label, score=score)

    # ------------------------------------------------------------------ main
    async def analyze(self, raw: RawMessage, dry_run: bool = False) -> PipelineResult:
        """Run the full pipeline. Never raises (errors become a SKIP with a reason)."""
        try:
            result = await self._analyze(raw, dry_run)
        except Exception as exc:  # noqa: BLE001
            log.exception("pipeline error source=%s msg=%s", raw.source_name, raw.message_id)
            result = self._skip([f"internal_error:{exc!r}"])
        log.info("deal source=%s msg=%s action=%s label=%s score=%s reasons=%s",
                 raw.source_name, raw.message_id, result.action.value, result.label.value,
                 result.score, "; ".join(result.reasons))
        await self._record(raw, result, dry_run)
        return result

    async def _analyze(self, raw: RawMessage, dry_run: bool) -> PipelineResult:
        cfg = self.settings.cfg
        text = normalize_text(raw.text)
        fc = self.settings.filter_config()

        if raw.is_forward and cfg.reader.ignore_forwards:
            return self._skip(["forwarded_message"])
        ad = is_ad(text, fc)
        if ad:
            return self._skip([f"ad_or_promo:{ad}"])

        urls = extract_urls(text, raw.urls)[:MAX_URLS_PER_MESSAGE]
        if not urls:
            return self._skip(["no_links"])

        resolved = list(await asyncio.gather(*(self.expander.resolve(u) for u in urls)))
        store_links = [r for r in resolved if r.store]
        enabled = set(self.settings.enabled_stores)
        usable: list[ResolvedLink] = []
        seen_keys: set[str] = set()
        for r in store_links:
            if r.store in enabled and r.key and r.key not in seen_keys:
                seen_keys.add(r.key)
                usable.append(r)
        unresolved = sum(1 for r in resolved if not r.resolved and self.expander.is_shortener(r.original))

        if store_links and not usable:
            stores = sorted({r.store for r in store_links if r.store})
            return self._skip([f"store_not_enabled:{','.join(stores)}"], resolved)

        info = extract_info(text, raw.strike_texts)
        fr = evaluate(text, info, fc, cfg.price_error, has_store_link=bool(usable),
                      product_count=len(usable), unresolved_links=unresolved)
        if fr.label == Label.SKIP:
            return self._skip(fr.reasons, resolved, score=fr.score)

        primary = usable[0]
        assert primary.store and primary.key
        store_name = cfg.stores[primary.store].name or primary.store
        title = info.title or f"Deal on {store_name}"
        title_norm = normalize_title(info.title)
        reasons = list(fr.reasons)

        # ---- dedupe
        dd = await check_duplicate(self.db, primary.key, title_norm, info.price, self.clock(), cfg.dedupe)
        if dd.duplicate:
            return self._skip([dd.reason], resolved, fr.label, fr.score)
        if dd.price_dropped:
            reasons.append(dd.reason)

        # ---- convert affiliate links
        results = await asyncio.gather(*(self.converters.convert(r) for r in usable))
        links: list[ConvertedLink] = []
        all_ok = True
        force_approval = False
        for r, res in zip(usable, results):
            if res.ok:
                links.append(ConvertedLink(r.store or "", res.url, True, res.note))
                continue
            all_ok = False
            reasons.append(f"convert_failed:{r.store}:{res.note}")
            policy = cfg.conversion.on_fail
            if policy == "skip":
                return self._skip(reasons, resolved, fr.label, fr.score)
            if policy == "approval":
                force_approval = True
            # clean, non-affiliate canonical link (never someone else's tag)
            links.append(ConvertedLink(r.store or "", r.canonical or r.final, False, res.note))

        # ---- decide action
        mode = self.settings.mode
        if mode == "auto" and not force_approval:
            action = Action.AUTO
        elif mode == "approval" or force_approval:
            action = Action.APPROVAL
        else:  # mixed
            pc = cfg.posting
            brand_ok = fr.brand_allowed or not pc.require_brand_allowlist
            pe_ok = not (fr.label == Label.PRICE_ERROR and pc.price_errors_need_approval)
            if fr.score >= pc.auto_min_score and all_ok and brand_ok and pe_ok:
                action = Action.AUTO
            else:
                action = Action.APPROVAL
                why = []
                if fr.score < pc.auto_min_score:
                    why.append(f"score {fr.score:g}<{pc.auto_min_score:g}")
                if not all_ok:
                    why.append("link not converted")
                if not brand_ok:
                    why.append("brand not in allowlist")
                if not pe_ok:
                    why.append("price errors need approval")
                reasons.append("needs_approval:" + ", ".join(why))

        deal = ProcessedDeal(
            source_id=raw.source_id, source_name=raw.source_name, message_id=raw.message_id,
            deal_key=primary.key, store=primary.store, product_id=primary.product_id,
            title=title, title_norm=title_norm, info=info, label=fr.label, score=fr.score,
            reasons=reasons, links=links, all_converted=all_ok, brand_allowed=fr.brand_allowed,
            price_dropped=dd.price_dropped, action=action)

        # ---- media (downloaded only for deals that passed)
        if raw.media_loader and not dry_run:
            try:
                deal.media_paths = await raw.media_loader()
            except Exception as exc:  # noqa: BLE001
                log.warning("media download failed: %r", exc)

        self.render(deal)

        # ---- final safety net: nothing but my links may appear
        foreign = self.foreign_links(deal.html, deal)
        if foreign:
            deal.action = Action.SKIP
            return self._skip([f"foreign_link_in_post:{foreign[0]}"] + reasons, resolved,
                              fr.label, fr.score, deal)
        return PipelineResult(action, reasons, deal=deal, resolved_links=resolved,
                              label=fr.label, score=fr.score)

    # ------------------------------------------------------------------ rendering
    def render(self, deal: ProcessedDeal) -> None:
        """(Re-)render ``deal.html`` from its facts (used after label changes too)."""
        cfg = self.settings.cfg
        r = format_post(title=deal.title, info=deal.info, label=deal.label, links=deal.links,
                        store=deal.store, t=cfg.templates, stores=cfg.stores,
                        price_dropped=deal.price_dropped, has_media=bool(deal.media_paths))
        deal.html, deal.header_html, deal.body_html, deal.fits_caption = (
            r.html, r.header_html, r.body_html, r.fits_caption)

    def foreign_links(self, html_text: str, deal: ProcessedDeal) -> list[str]:
        allowed = {l.url for l in deal.links}
        return find_foreign_links(html_text, allowed, self.converters.amazon.is_my_link,
                                  self.settings.env.target_channel)
