"""Legacy-source access for the ETL (02-schema-etl.md §8).

Reads the legacy data straight from the MariaDB staging container (the loaded `gahk_dk` dump) via
PyMySQL. Connecting with charset utf8mb4 lets MariaDB transcode each column from its declared charset
(latin1 / utf8mb3) to proper Unicode on read.
"""

from __future__ import annotations  # PyMySQL's Connection isn't subscriptable at runtime; defer annots

import contextlib
import datetime
import os
from collections.abc import Generator
from typing import Any

import pymysql
from django.utils import timezone
from pymysql.connections import Connection
from pymysql.cursors import DictCursor


def legacy_connect() -> Connection[DictCursor]:
    return pymysql.connect(
        host=os.environ.get("LEGACY_DB_HOST", "127.0.0.1"),
        port=int(os.environ.get("LEGACY_DB_PORT", "3306")),
        user=os.environ.get("LEGACY_DB_USER", "root"),
        password=os.environ.get("LEGACY_DB_PASSWORD", "root"),
        database=os.environ.get("LEGACY_DB_NAME", "gahk_dk"),
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
    )


@contextlib.contextmanager
def legacy_cursor() -> Generator[DictCursor, None, None]:
    conn = legacy_connect()
    try:
        with conn.cursor() as cur:
            yield cur
    finally:
        conn.close()


def fetch_all(sql: str, args: tuple | None = None) -> list[dict[str, Any]]:
    # Legacy DB rows are dynamically typed (columns vary per query), so Any is the honest boundary
    # type here — it lets the ETL commands assign row values into model fields without per-column casts.
    with legacy_cursor() as cur:
        cur.execute(sql, args or ())
        return cur.fetchall()


def decode_month_number(mn: int | None) -> tuple[int, int] | None:
    """Legacy monthNumber = 12*year + month  ->  (year, month) with month in 1..12 (delt.php)."""
    if mn is None:
        return None
    month = mn % 12 or 12
    year = (mn - 1) // 12
    return year, month


def epoch_to_dt(ts: int | None) -> datetime.datetime | None:
    """Legacy int epoch -> aware datetime (Europe/Copenhagen). None/0/garbage -> None."""
    if not ts:
        return None
    try:
        return datetime.datetime.fromtimestamp(int(ts), tz=timezone.get_current_timezone())
    except (ValueError, OSError, OverflowError):
        return None


def resident_id_remap() -> dict[int, int]:
    """Map every legacy intern_alumne.ID -> the kept resident ID (dedupe by email, keep highest ID).

    Mirrors etl_residents so downstream ETLs attach references to the merged resident. Legacy IDs whose
    email was empty (dropped) or that aren't in intern_alumne (former residents — see A) accept) are
    simply absent from the map and should be skipped by callers.
    """
    by_email: dict[str, list[int]] = {}
    for r in fetch_all("SELECT ID, email FROM intern_alumne"):
        key = (r["email"] or "").strip().lower()
        if key:
            by_email.setdefault(key, []).append(r["ID"])
    remap = {}
    for ids in by_email.values():
        keep = max(ids)
        for i in ids:
            remap[i] = keep
    return remap
