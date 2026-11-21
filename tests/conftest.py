"""Shared fixtures: config, in-memory DB, fake HTTP session (no real network)."""
from __future__ import annotations

from typing import Any, Optional

import pytest

from config import AppConfig, EnvConfig, Settings
from db import Database


class FakeResponse:
    def __init__(self, status: int = 200, headers: Optional[dict[str, str]] = None,
                 body: bytes = b"", json_data: Any = None):
        self.status = status
        self.headers = headers or {}
        self._body = body
        self._json = json_data

        class _Content:
            def __init__(self, b: bytes):
                self.b = b

            async def read(self, n: int = -1) -> bytes:
                return self.b if n < 0 else self.b[:n]
        self.content = _Content(body)

    async def json(self, content_type: Any = None) -> Any:
        return self._json

    async def text(self) -> str:
        return self._body.decode()

    async def __aenter__(self) -> "FakeResponse":
        return self

    async def __aexit__(self, *a: Any) -> None:
        return None


class FakeSession:
    """Maps URL → FakeResponse (or an Exception to raise). Records calls."""

    def __init__(self, routes: dict[str, Any]):
        self.routes = routes
        self.calls: list[str] = []
        self.posts: list[dict[str, Any]] = []

    def _get(self, url: str) -> FakeResponse:
        self.calls.append(url)
        r = self.routes.get(url)
        if isinstance(r, Exception):
            raise r
        return r or FakeResponse(404)

    def get(self, url: str, **kw: Any) -> FakeResponse:
        return self._get(url)

    def post(self, url: str, json: Any = None, **kw: Any) -> FakeResponse:
        self.posts.append({"url": url, "json": json, "headers": kw.get("headers")})
        return self._get(url)

    async def close(self) -> None:
        return None


def redirect(to: str, status: int = 301) -> FakeResponse:
    return FakeResponse(status, {"Location": to})


@pytest.fixture
def cfg() -> AppConfig:
    c = AppConfig()
    c.expansion.retries = 0
    return c


@pytest.fixture
def env(tmp_path: Any) -> EnvConfig:
    return EnvConfig(api_id=1, api_hash="x", bot_token="t", admin_ids=[1], target_channel="@mychannel",
                     amazon_tag="mytag-21", data_dir=tmp_path)


@pytest.fixture
async def db() -> Any:
    d = Database(":memory:")
    await d.connect()
    yield d
    await d.close()


@pytest.fixture
async def settings(cfg: AppConfig, env: EnvConfig, db: Database) -> Settings:
    s = Settings(cfg, env, db)
    await s.load_overrides()
    return s
