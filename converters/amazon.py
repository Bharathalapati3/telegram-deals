"""Amazon Associates converter: rebuild a clean link with MY tag only."""
from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from links import extract_asin, strip_tracking

from .base import ConvertResult, Converter


class AmazonConverter(Converter):
    name = "amazon"
    stores = {"amazon"}
    output_domains = ["amazon.in"]

    def __init__(self, tag: str):
        if not tag:
            raise ValueError("AMAZON_TAG is required")
        self.tag = tag

    async def convert(self, url: str) -> ConvertResult:
        asin = extract_asin(url)
        if asin:
            return ConvertResult(f"https://www.amazon.in/dp/{asin}?tag={self.tag}", True, "amazon",
                                 f"ASIN {asin}")
        # Non-product Amazon page (deal page, search, storefront): clean it and set my tag.
        parts = urlsplit(strip_tracking(url))
        if (parts.hostname or "").lower().removeprefix("www.") != "amazon.in":
            return ConvertResult(url, False, "amazon", "not an amazon.in URL")
        query = [(k, v) for k, v in parse_qsl(parts.query) if k.lower() != "tag"]
        query.append(("tag", self.tag))
        clean = urlunsplit(("https", "www.amazon.in", parts.path, urlencode(query), ""))
        return ConvertResult(clean, True, "amazon", "non-product page, tag applied")

    def is_my_link(self, url: str) -> bool:
        """True if ``url`` is an amazon.in link carrying only my tag and no other affiliate data."""
        parts = urlsplit(url)
        if (parts.hostname or "").lower().removeprefix("www.") != "amazon.in":
            return False
        q = parse_qsl(parts.query, keep_blank_values=True)
        tags = [v for k, v in q if k.lower() == "tag"]
        from links import is_affiliate_param
        others = [k for k, _ in q if is_affiliate_param(k) and k.lower() != "tag"]
        return tags == [self.tag] and not others
