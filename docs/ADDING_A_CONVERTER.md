# How to add a new affiliate network converter

A *converter* turns a clean store link (e.g. `https://www.flipkart.com/…?pid=X`)
into **your** affiliate link. The rest of the bot handles everything else: link
expansion, cleaning, failure policy and the final "only my links" check.

## Option A — the network has a simple "URL in, URL out" JSON API (no code)

Most networks do. `converters/base.py` has a generic `HttpApiConverter` that is
configured entirely from `.env`. The EarnKaro and Cuelinks converters are just
this class with a name. To use one of them, fill in its `EARNKARO_*` /
`CUELINKS_*` values in `.env` from **your account's API docs** and set
`AFFILIATE_NETWORK`.

## Option B — add a new network (about 20 lines)

1. **Create `converters/mynetwork.py`:**

   ```python
   from typing import Any
   from .base import HttpApiConverter, HttpApiSpec

   class MyNetworkConverter(HttpApiConverter):
       name = "mynetwork"
       stores = {"flipkart", "myntra"}   # or None = every store except Amazon

       def __init__(self, spec: HttpApiSpec, output_domains: list[str], **kw: Any):
           super().__init__(spec, **kw)
           self.output_domains = output_domains
   ```

   If the API is unusual (signed requests, XML, two steps…), subclass
   `Converter` instead and write `async def convert(self, url) -> ConvertResult`
   yourself. Rules:
   - **Never raise.** Return `ConvertResult(url, False, store, "reason")` on failure.
   - Return `ok=True` **only** when the URL is really your affiliate link.
   - Use timeouts. Never scrape product pages.

2. **Register it** in `converters/__init__.py`:

   ```python
   from .mynetwork import MyNetworkConverter

   def _make_mynetwork(env, cfg):
       spec = _spec_from_env("MYNETWORK", os.environ.get("MYNETWORK_API_URL", ""),
                             os.environ.get("MYNETWORK_API_KEY", ""), cfg.conversion.timeout_seconds)
       return MyNetworkConverter(spec, _domains("MYNETWORK", "mynet.link"),
                                 stores_map=cfg.stores, concurrency=cfg.conversion.concurrency)

   NETWORKS = {
       "earnkaro": _make_earnkaro,
       "cuelinks": _make_cuelinks,
       "mynetwork": _make_mynetwork,
   }
   ```

3. **Configure** `.env`: `AFFILIATE_NETWORK=mynetwork` plus `MYNETWORK_API_URL`,
   `MYNETWORK_API_KEY`, `MYNETWORK_REQUEST_FIELD`, `MYNETWORK_RESPONSE_PATH`, etc.
   (same names as the EarnKaro block, with your prefix).

4. **Enable the store**: add it under `enabled_stores` in `config.yaml`, or send
   `/enablestore flipkart` to the bot.

5. **Test** without posting: send `/test <a deal message with a flipkart link>` to
   the bot. The dry run shows whether conversion worked. Add a unit test like
   `tests/test_converters.py::test_http_converter_success_with_response_path`
   (it uses a fake HTTP session, so no real API calls).

## Adding a new *store* (no code)

Add it to `config.yaml`:

```yaml
stores:
  snapdeal:
    name: Snapdeal
    domains: [snapdeal.com]
    keep_params: []        # query params to keep in the clean link
enabled_stores: [amazon, snapdeal]
```

If the store uses its own short-link domain, add it to
`expansion.extra_shortener_domains`.
