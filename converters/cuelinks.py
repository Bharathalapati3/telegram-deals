"""Cuelinks converter (stub — fill in your API details).

TODO(you): Cuelinks publishers can request API access. Copy the exact endpoint,
auth scheme and request/response format from your Cuelinks dashboard (API section)
and set these in ``.env``:

    CUELINKS_API_URL=        # TODO: link-generation endpoint from your Cuelinks dashboard
    CUELINKS_API_KEY=        # TODO: your API token
    CUELINKS_METHOD=GET      # as documented
    CUELINKS_AUTH_HEADER=Authorization
    CUELINKS_AUTH_VALUE=     # TODO: header value format, use {key} where the token goes
    CUELINKS_REQUEST_FIELD=  # TODO: param carrying the product URL
    CUELINKS_EXTRA_BODY=     # optional JSON object of extra constant params
    CUELINKS_RESPONSE_PATH=  # TODO: dot path to the affiliate link in the JSON reply
    CUELINKS_OUTPUT_DOMAINS=linksredirect.com

No endpoint is hard-coded on purpose: use only what your account's docs show.
"""
from __future__ import annotations

from typing import Any

from .base import HttpApiConverter, HttpApiSpec


class CuelinksConverter(HttpApiConverter):
    name = "cuelinks"
    stores = None

    def __init__(self, spec: HttpApiSpec, output_domains: list[str], **kw: Any):
        super().__init__(spec, **kw)
        self.output_domains = output_domains
