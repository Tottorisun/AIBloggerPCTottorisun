"""SQLite storage layer.

products/offers/unmatched as specified in the project brief. Offers are
append-only (one row per capture) so price history is preserved; nothing is
ever overwritten, only inserted.
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from pc_price_tracker.constants import Category
from pc_price_tracker.models import NormalizedProduct, Product, RawOffer

SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category TEXT NOT NULL,
    brand TEXT NOT NULL,
    model TEXT NOT NULL,
    normalized_key TEXT NOT NULL UNIQUE,
    specs_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS offers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL REFERENCES products(id),
    source TEXT NOT NULL,
    url TEXT NOT NULL,
    price INTEGER NOT NULL,
    in_stock INTEGER NOT NULL,
    availability TEXT,
    captured_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_offers_product ON offers(product_id);
CREATE INDEX IF NOT EXISTS idx_offers_source_captured ON offers(source, captured_at);

CREATE TABLE IF NOT EXISTS unmatched (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    category TEXT NOT NULL,
    external_id TEXT NOT NULL,
    title TEXT NOT NULL,
    price INTEGER NOT NULL,
    url TEXT NOT NULL,
    reason TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    UNIQUE(source, external_id)
);
"""


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, coltype: str) -> None:
    """SQLite has no `ADD COLUMN IF NOT EXISTS` — needed because `offers`
    already has years of scraped history on disk that predates the
    `availability` column, and CREATE TABLE IF NOT EXISTS is a no-op on a
    table that already exists."""
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")


def connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    _ensure_column(conn, "offers", "availability", "TEXT")
    return conn


def upsert_product(conn: sqlite3.Connection, normalized: NormalizedProduct) -> int:
    """Insert the product if new, otherwise merge specs into the existing row.

    Returns the product id.
    """
    row = conn.execute(
        "SELECT id, specs_json FROM products WHERE normalized_key = ?",
        (normalized.normalized_key,),
    ).fetchone()
    if row is None:
        cur = conn.execute(
            "INSERT INTO products (category, brand, model, normalized_key, specs_json) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                normalized.category,
                normalized.brand,
                normalized.model,
                normalized.normalized_key,
                json.dumps(normalized.specs, ensure_ascii=False),
            ),
        )
        return int(cur.lastrowid)

    existing_specs = json.loads(row["specs_json"])
    merged_specs = {**existing_specs, **normalized.specs}
    conn.execute(
        "UPDATE products SET brand = ?, model = ?, specs_json = ? WHERE id = ?",
        (normalized.brand, normalized.model, json.dumps(merged_specs, ensure_ascii=False), row["id"]),
    )
    return int(row["id"])


def insert_offer(conn: sqlite3.Connection, product_id: int, raw: RawOffer) -> int:
    cur = conn.execute(
        "INSERT INTO offers (product_id, source, url, price, in_stock, availability, captured_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            product_id,
            raw.source,
            raw.url,
            raw.price,
            int(raw.in_stock),
            raw.availability,
            raw.captured_at.isoformat(),
        ),
    )
    return int(cur.lastrowid)


def insert_unmatched(conn: sqlite3.Connection, raw: RawOffer, reason: str) -> None:
    conn.execute(
        "INSERT INTO unmatched (source, category, external_id, title, price, url, reason, captured_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(source, external_id) DO UPDATE SET "
        "title=excluded.title, price=excluded.price, url=excluded.url, "
        "reason=excluded.reason, captured_at=excluded.captured_at",
        (
            raw.source,
            raw.category,
            raw.external_id,
            raw.title,
            raw.price,
            raw.url,
            reason,
            raw.captured_at.isoformat(),
        ),
    )


def get_product(conn: sqlite3.Connection, product_id: int) -> Product | None:
    row = conn.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
    if row is None:
        return None
    return _row_to_product(row)


def _row_to_product(row: sqlite3.Row) -> Product:
    return Product(
        id=row["id"],
        category=row["category"],
        brand=row["brand"],
        model=row["model"],
        normalized_key=row["normalized_key"],
        specs=json.loads(row["specs_json"]),
    )


def latest_offers(
    conn: sqlite3.Connection,
    category: Category | None = None,
    in_stock_only: bool = True,
) -> list[dict[str, Any]]:
    """Latest captured offer per (product, source), joined with product info.

    Used by the config builder, which wants "what can I buy right now".
    """
    query = """
    SELECT p.id AS product_id, p.category, p.brand, p.model, p.normalized_key,
           p.specs_json, o.id AS offer_id, o.source, o.url, o.price,
           o.in_stock, o.availability, o.captured_at
    FROM offers o
    JOIN products p ON p.id = o.product_id
    JOIN (
        SELECT product_id, source, MAX(captured_at) AS max_captured
        FROM offers
        GROUP BY product_id, source
    ) latest
      ON o.product_id = latest.product_id
     AND o.source = latest.source
     AND o.captured_at = latest.max_captured
    """
    params: list[Any] = []
    if category is not None:
        query += " WHERE p.category = ?"
        params.append(category)

    rows = conn.execute(query, params).fetchall()
    results = []
    for row in rows:
        if in_stock_only and not row["in_stock"]:
            continue
        results.append(
            {
                "product_id": row["product_id"],
                "category": row["category"],
                "brand": row["brand"],
                "model": row["model"],
                "normalized_key": row["normalized_key"],
                "specs": json.loads(row["specs_json"]),
                "offer_id": row["offer_id"],
                "source": row["source"],
                "url": row["url"],
                "price": row["price"],
                "in_stock": bool(row["in_stock"]),
                "availability": row["availability"],
                "captured_at": row["captured_at"],
            }
        )
    return results


def cheapest_offer_per_product(
    conn: sqlite3.Connection,
    category: Category | None = None,
    in_stock_only: bool = True,
) -> list[dict[str, Any]]:
    """Latest offers collapsed to one (cheapest) row per product.

    in_stock_only defaults to True (existing behavior for export/reporting
    callers). The build CLI passes False so preorder/out-of-stock offers
    reach the builder too, which then applies its own per-profile
    require_availability floor — that's a real, config-driven, logged
    filter (see builder._apply_floors), unlike this blunt boolean gate.
    """
    best: dict[int, dict[str, Any]] = {}
    for offer in latest_offers(conn, category=category, in_stock_only=in_stock_only):
        pid = offer["product_id"]
        if pid not in best or offer["price"] < best[pid]["price"]:
            best[pid] = offer
    return list(best.values())


def offer_counts_by_product(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    """normalized_key -> {category, brand, model, offer_count} for every
    product. offer_count is the total number of captures ever recorded for
    it (across all scrape runs/sources) — a proxy for "how consistently
    this listing shows up", used to prioritize which tier estimates are
    worth a human's time to verify first."""
    rows = conn.execute(
        """
        SELECT p.normalized_key, p.category, p.brand, p.model, COUNT(o.id) AS offer_count
        FROM products p
        LEFT JOIN offers o ON o.product_id = p.id
        GROUP BY p.id
        """
    ).fetchall()
    return {
        r["normalized_key"]: {
            "category": r["category"],
            "brand": r["brand"],
            "model": r["model"],
            "offer_count": r["offer_count"],
        }
        for r in rows
    }


def movers(conn: sqlite3.Connection, days: int) -> list[dict[str, Any]]:
    """Products with the largest price change over the last `days` days.

    For each (product, source) time series, compares the most recent capture
    to the most recent capture at or before `now - days`. If the whole
    history is younger than the window, falls back to the oldest capture
    available so early runs still produce output.
    """
    rows = conn.execute(
        """
        SELECT o.product_id, o.source, o.price, o.captured_at,
               p.category, p.brand, p.model
        FROM offers o
        JOIN products p ON p.id = o.product_id
        ORDER BY o.product_id, o.source, o.captured_at ASC
        """
    ).fetchall()

    series: dict[tuple[int, str], list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        series[(row["product_id"], row["source"])].append(row)

    cutoff = datetime.now() - timedelta(days=days)
    results = []
    for (product_id, source), captures in series.items():
        latest = captures[-1]
        baseline = captures[0]
        for capture in captures:
            captured_at = datetime.fromisoformat(capture["captured_at"])
            if captured_at <= cutoff:
                baseline = capture
        if baseline["captured_at"] == latest["captured_at"]:
            continue

        old_price = baseline["price"]
        new_price = latest["price"]
        if old_price == 0:
            continue
        delta = new_price - old_price
        pct = (delta / old_price) * 100
        results.append(
            {
                "product_id": product_id,
                "source": source,
                "category": latest["category"],
                "brand": latest["brand"],
                "model": latest["model"],
                "old_price": old_price,
                "new_price": new_price,
                "delta": delta,
                "pct": round(pct, 1),
                "baseline_at": baseline["captured_at"],
                "latest_at": latest["captured_at"],
            }
        )

    results.sort(key=lambda r: abs(r["pct"]), reverse=True)
    return results
