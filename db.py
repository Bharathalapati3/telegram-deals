"""SQLite storage (aiosqlite) with simple numbered migrations (PRAGMA user_version)."""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Optional

import aiosqlite

from models import ProcessedDeal

log = logging.getLogger(__name__)

# Each entry is one migration. Never edit an old entry; append a new one instead.
MIGRATIONS: list[str] = [
    # 1 — initial schema
    """
    CREATE TABLE IF NOT EXISTS deals_seen (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source_id INTEGER, source_name TEXT, message_id INTEGER,
        deal_key TEXT, title TEXT, price REAL, mrp REAL, discount REAL,
        label TEXT, score REAL, action TEXT, reasons TEXT,
        created_at REAL NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_seen_msg ON deals_seen(source_id, message_id);
    CREATE INDEX IF NOT EXISTS idx_seen_created ON deals_seen(created_at);

    CREATE TABLE IF NOT EXISTS posted (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        deal_key TEXT NOT NULL, store TEXT, product_id TEXT, title TEXT, title_norm TEXT,
        price REAL, label TEXT, source_id INTEGER, message_id INTEGER,
        channel_msg_ids TEXT NOT NULL, header_html TEXT, body_html TEXT,
        has_media INTEGER DEFAULT 0, deal_over INTEGER DEFAULT 0,
        posted_at REAL NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_posted_key ON posted(deal_key, posted_at);
    CREATE INDEX IF NOT EXISTS idx_posted_at ON posted(posted_at);

    CREATE TABLE IF NOT EXISTS pending_approvals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        deal_key TEXT NOT NULL, title_norm TEXT, price REAL,
        payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
        admin_msgs TEXT NOT NULL DEFAULT '[]', custom_text TEXT,
        created_at REAL NOT NULL, expires_at REAL NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_pending_status ON pending_approvals(status, expires_at);

    CREATE TABLE IF NOT EXISTS post_queue (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        deal_key TEXT NOT NULL, title_norm TEXT, price REAL,
        payload TEXT NOT NULL, priority INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'queued',
        created_at REAL NOT NULL, expires_at REAL NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_queue_status ON post_queue(status, priority, created_at);

    CREATE TABLE IF NOT EXISTS link_cache (
        url TEXT PRIMARY KEY, final_url TEXT NOT NULL, ok INTEGER NOT NULL, created_at REAL NOT NULL
    );

    CREATE TABLE IF NOT EXISTS source_stats (
        source_id INTEGER PRIMARY KEY, source_name TEXT,
        seen INTEGER DEFAULT 0, passed INTEGER DEFAULT 0, posted INTEGER DEFAULT 0,
        last_seen_at REAL
    );

    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at REAL NOT NULL
    );
    """,
]


class Database:
    """Thin async wrapper around SQLite with app-specific queries."""

    def __init__(self, path: str | Path, link_ttl_hours: float = 168, failed_ttl_minutes: float = 60):
        self.path = str(path)
        self.link_ttl = link_ttl_hours * 3600
        self.failed_ttl = failed_ttl_minutes * 60
        self.conn: Optional[aiosqlite.Connection] = None

    # ------------------------------------------------------------------ lifecycle
    async def connect(self) -> None:
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.execute("PRAGMA journal_mode=WAL")
        await self.conn.execute("PRAGMA busy_timeout=5000")
        await self.migrate()

    async def close(self) -> None:
        if self.conn:
            await self.conn.close()
            self.conn = None

    async def migrate(self) -> None:
        assert self.conn
        cur = await self.conn.execute("PRAGMA user_version")
        version = (await cur.fetchone())[0]
        for i, sql in enumerate(MIGRATIONS[version:], start=version + 1):
            log.info("Applying DB migration %s", i)
            await self.conn.executescript(sql)
            await self.conn.execute(f"PRAGMA user_version = {i}")
            await self.conn.commit()

    async def _exec(self, sql: str, args: tuple[Any, ...] = ()) -> aiosqlite.Cursor:
        assert self.conn, "database not connected"
        cur = await self.conn.execute(sql, args)
        await self.conn.commit()
        return cur

    async def _all(self, sql: str, args: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        assert self.conn, "database not connected"
        cur = await self.conn.execute(sql, args)
        return [dict(r) for r in await cur.fetchall()]

    async def _one(self, sql: str, args: tuple[Any, ...] = ()) -> Optional[dict[str, Any]]:
        rows = await self._all(sql, args)
        return rows[0] if rows else None

    # ------------------------------------------------------------------ settings
    async def get_all_settings(self) -> dict[str, Any]:
        return {r["key"]: json.loads(r["value"]) for r in await self._all("SELECT key, value FROM settings")}

    async def set_setting(self, key: str, value: Any) -> None:
        await self._exec(
            "INSERT INTO settings(key, value, updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, json.dumps(value), time.time()))

    # ------------------------------------------------------------------ link cache
    async def get_link_cache(self, url: str) -> Optional[tuple[str, bool]]:
        row = await self._one("SELECT final_url, ok, created_at FROM link_cache WHERE url=?", (url,))
        if not row:
            return None
        ttl = self.link_ttl if row["ok"] else self.failed_ttl
        if time.time() - row["created_at"] > ttl:
            return None
        return row["final_url"], bool(row["ok"])

    async def put_link_cache(self, url: str, final_url: str, ok: bool) -> None:
        await self._exec("INSERT OR REPLACE INTO link_cache(url, final_url, ok, created_at) VALUES(?,?,?,?)",
                         (url, final_url, int(ok), time.time()))

    # ------------------------------------------------------------------ deals_seen / stats
    async def record_seen(self, *, source_id: int, source_name: str, message_id: int,
                          deal_key: str = "", title: str = "", price: Optional[float] = None,
                          mrp: Optional[float] = None, discount: Optional[float] = None,
                          label: str = "", score: float = 0, action: str = "",
                          reasons: list[str] | None = None) -> None:
        await self._exec(
            "INSERT INTO deals_seen(source_id, source_name, message_id, deal_key, title, price, mrp, "
            "discount, label, score, action, reasons, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (source_id, source_name, message_id, deal_key, title, price, mrp, discount, label, score,
             action, json.dumps(reasons or []), time.time()))

    async def last_skipped(self, limit: int = 10) -> list[dict[str, Any]]:
        rows = await self._all("SELECT * FROM deals_seen WHERE action='skip' ORDER BY id DESC LIMIT ?", (limit,))
        for r in rows:
            r["reasons"] = json.loads(r["reasons"] or "[]")
        return rows

    async def was_handled(self, source_id: int, message_id: int) -> Optional[str]:
        """Last action recorded for a source message (used to decide on edits)."""
        row = await self._one("SELECT action FROM deals_seen WHERE source_id=? AND message_id=? "
                              "ORDER BY id DESC LIMIT 1", (source_id, message_id))
        return row["action"] if row else None

    async def bump_stat(self, source_id: int, source_name: str, field: str) -> None:
        assert field in {"seen", "passed", "posted"}
        await self._exec(
            f"INSERT INTO source_stats(source_id, source_name, {field}, last_seen_at) VALUES(?,?,1,?) "
            f"ON CONFLICT(source_id) DO UPDATE SET {field}={field}+1, source_name=excluded.source_name, "
            f"last_seen_at=excluded.last_seen_at",
            (source_id, source_name, time.time()))

    async def get_stats(self) -> list[dict[str, Any]]:
        return await self._all("SELECT * FROM source_stats ORDER BY seen DESC")

    async def count_seen_since(self, since: float) -> int:
        row = await self._one("SELECT COUNT(*) AS n FROM deals_seen WHERE created_at>=?", (since,))
        return int(row["n"]) if row else 0

    async def count_actions_since(self, since: float) -> dict[str, int]:
        rows = await self._all("SELECT action, COUNT(*) AS n FROM deals_seen WHERE created_at>=? "
                               "GROUP BY action", (since,))
        return {r["action"]: int(r["n"]) for r in rows}

    # ------------------------------------------------------------------ dedupe lookups
    async def recent_by_key(self, deal_key: str, since_ts: float) -> Optional[dict[str, Any]]:
        return await self._one(
            "SELECT price, ts FROM ("
            " SELECT price, posted_at AS ts FROM posted WHERE deal_key=? AND posted_at>=?"
            " UNION ALL SELECT price, created_at FROM pending_approvals "
            "   WHERE deal_key=? AND status='pending' AND created_at>=?"
            " UNION ALL SELECT price, created_at FROM post_queue "
            "   WHERE deal_key=? AND status='queued' AND created_at>=?"
            ") ORDER BY price ASC LIMIT 1",
            (deal_key, since_ts) * 3)

    async def recent_titles(self, since_ts: float) -> list[dict[str, Any]]:
        return await self._all(
            "SELECT title_norm, MIN(price) AS price FROM ("
            " SELECT title_norm, price FROM posted WHERE posted_at>=?"
            " UNION ALL SELECT title_norm, price FROM pending_approvals WHERE status='pending' AND created_at>=?"
            " UNION ALL SELECT title_norm, price FROM post_queue WHERE status='queued' AND created_at>=?"
            ") WHERE title_norm IS NOT NULL AND title_norm != '' GROUP BY title_norm",
            (since_ts,) * 3)

    # ------------------------------------------------------------------ posted
    async def add_posted(self, deal: ProcessedDeal, channel_msg_ids: list[int], header_html: str,
                         body_html: str, has_media: bool) -> int:
        cur = await self._exec(
            "INSERT INTO posted(deal_key, store, product_id, title, title_norm, price, label, source_id, "
            "message_id, channel_msg_ids, header_html, body_html, has_media, posted_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (deal.deal_key, deal.store, deal.product_id, deal.title, deal.title_norm, deal.info.price,
             deal.label.value, deal.source_id, deal.message_id, json.dumps(channel_msg_ids),
             header_html, body_html, int(has_media), time.time()))
        return int(cur.lastrowid or 0)

    async def get_posted(self, post_id: int) -> Optional[dict[str, Any]]:
        return await self._one("SELECT * FROM posted WHERE id=?", (post_id,))

    async def get_posted_by_channel_msg(self, msg_id: int) -> Optional[dict[str, Any]]:
        rows = await self._all("SELECT * FROM posted ORDER BY id DESC LIMIT 2000")
        for r in rows:
            if msg_id in json.loads(r["channel_msg_ids"]):
                return r
        return None

    async def mark_deal_over(self, post_id: int) -> None:
        await self._exec("UPDATE posted SET deal_over=1 WHERE id=?", (post_id,))

    async def count_posts_since(self, since: float, labels: Optional[list[str]] = None) -> int:
        if labels:
            q = ",".join("?" * len(labels))
            row = await self._one(f"SELECT COUNT(*) AS n FROM posted WHERE posted_at>=? AND label IN ({q})",
                                  (since, *labels))
        else:
            row = await self._one("SELECT COUNT(*) AS n FROM posted WHERE posted_at>=?", (since,))
        return int(row["n"]) if row else 0

    async def last_post_time(self) -> float:
        row = await self._one("SELECT MAX(posted_at) AS t FROM posted")
        return float(row["t"] or 0) if row else 0.0

    async def recent_posts(self, limit: int = 10) -> list[dict[str, Any]]:
        return await self._all("SELECT * FROM posted ORDER BY id DESC LIMIT ?", (limit,))

    # ------------------------------------------------------------------ pending approvals
    async def add_pending(self, deal: ProcessedDeal, expires_at: float) -> int:
        cur = await self._exec(
            "INSERT INTO pending_approvals(deal_key, title_norm, price, payload, created_at, expires_at) "
            "VALUES(?,?,?,?,?,?)",
            (deal.deal_key, deal.title_norm, deal.info.price, json.dumps(deal.to_dict()),
             time.time(), expires_at))
        return int(cur.lastrowid or 0)

    async def get_pending(self, pid: int) -> Optional[dict[str, Any]]:
        row = await self._one("SELECT * FROM pending_approvals WHERE id=?", (pid,))
        if row:
            row["deal"] = ProcessedDeal.from_dict(json.loads(row["payload"]))
            row["admin_msgs"] = json.loads(row["admin_msgs"] or "[]")
        return row

    async def update_pending(self, pid: int, *, status: Optional[str] = None,
                             admin_msgs: Optional[list[list[int]]] = None,
                             custom_text: Optional[str] = None,
                             deal: Optional[ProcessedDeal] = None) -> None:
        sets, args = [], []
        if status is not None:
            sets.append("status=?"); args.append(status)
        if admin_msgs is not None:
            sets.append("admin_msgs=?"); args.append(json.dumps(admin_msgs))
        if custom_text is not None:
            sets.append("custom_text=?"); args.append(custom_text)
        if deal is not None:
            sets.append("payload=?"); args.append(json.dumps(deal.to_dict()))
        if sets:
            await self._exec(f"UPDATE pending_approvals SET {', '.join(sets)} WHERE id=?", (*args, pid))

    async def claim_pending(self, pid: int, new_status: str) -> bool:
        """Atomically move a pending item out of 'pending'. False if someone else already did."""
        cur = await self._exec("UPDATE pending_approvals SET status=? WHERE id=? AND status='pending'",
                               (new_status, pid))
        return cur.rowcount == 1

    async def expired_pending(self, now: float) -> list[dict[str, Any]]:
        rows = await self._all("SELECT id FROM pending_approvals WHERE status='pending' AND expires_at<?", (now,))
        return [r for r in [await self.get_pending(x["id"]) for x in rows] if r]

    async def count_pending(self) -> int:
        row = await self._one("SELECT COUNT(*) AS n FROM pending_approvals WHERE status='pending'")
        return int(row["n"]) if row else 0

    # ------------------------------------------------------------------ post queue
    async def enqueue(self, deal: ProcessedDeal, priority: int, expires_at: float) -> int:
        cur = await self._exec(
            "INSERT INTO post_queue(deal_key, title_norm, price, payload, priority, created_at, expires_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (deal.deal_key, deal.title_norm, deal.info.price, json.dumps(deal.to_dict()), priority,
             time.time(), expires_at))
        return int(cur.lastrowid or 0)

    async def queued_items(self) -> list[dict[str, Any]]:
        rows = await self._all(
            "SELECT * FROM post_queue WHERE status='queued' ORDER BY priority DESC, created_at ASC")
        for r in rows:
            r["deal"] = ProcessedDeal.from_dict(json.loads(r["payload"]))
        return rows

    async def set_queue_status(self, qid: int, status: str) -> None:
        await self._exec("UPDATE post_queue SET status=? WHERE id=?", (status, qid))

    async def count_queued(self) -> int:
        row = await self._one("SELECT COUNT(*) AS n FROM post_queue WHERE status='queued'")
        return int(row["n"]) if row else 0

    # ------------------------------------------------------------------ housekeeping
    async def referenced_media(self) -> set[str]:
        """Media files still needed by pending approvals or queued posts."""
        paths: set[str] = set()
        for table, status in (("pending_approvals", "pending"), ("post_queue", "queued")):
            for r in await self._all(f"SELECT payload FROM {table} WHERE status=?", (status,)):
                paths.update(json.loads(r["payload"]).get("media_paths", []))
        return paths

    async def prune(self, older_than_days: int = 30) -> None:
        cutoff = time.time() - older_than_days * 86400
        await self._exec("DELETE FROM deals_seen WHERE created_at<?", (cutoff,))
        await self._exec("DELETE FROM link_cache WHERE created_at<?", (cutoff,))
        await self._exec("DELETE FROM pending_approvals WHERE status!='pending' AND created_at<?", (cutoff,))
        await self._exec("DELETE FROM post_queue WHERE status!='queued' AND created_at<?", (cutoff,))
