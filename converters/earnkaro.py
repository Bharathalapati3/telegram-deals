"""EarnKaro converter (stub — fill in your API details).

TODO(you): EarnKaro gives API access to approved affiliates. Look up the exact
endpoint, auth header and request/response format in your EarnKaro account
(Profile → API / "Link Converter API") and set these in ``.env``:

    EARNKARO_API_URL=        # TODO: the converter endpoint from your EarnKaro dashboard
    EARNKARO_API_KEY=        # TODO: your API token
    EARNKARO_METHOD=POST     # POST or GET, as documented
    EARNKARO_AUTH_HEADER=Authorization
    EARNKARO_AUTH_VALUE=Bearer {key}
    EARNKARO_REQUEST_FIELD=  # TODO: JSON field / query param that carries the product URL
    EARNKARO_EXTRA_BODY=     # optional JSON object of extra constant fields, e.g. {"a": "b"}
    EARNKARO_RESPONSE_PATH=  # TODO: dot path to the converted link in the JSON reply, e.g. data.link
    EARNKARO_OUTPUT_DOMAINS=ekaro.in   # domains of the links it returns (used by the link check)

No endpoint is hard-coded on purpose: use only what your account's docs show.
"""
from __future__ import annotations

from typing import Any

from .base import HttpApiConverter, HttpApiSpec


class EarnKaroConverter(HttpApiConverter):
    name = "earnkaro"
    stores = None  # every non-Amazon store EarnKaro supports; failures fall back per config

    def __init__(self, spec: HttpApiSpec, output_domains: list[str], **kw: Any):
        super().__init__(spec, **kw)
        self.output_domains = output_domains
