"""SQLite storage backend.

Chosen for the first version because it needs no server, ships with Python and
is easy to inspect. The schema is intentionally flat with JSON columns for the
nested fields (social links, raw payload, score breakdown), so it maps cleanly
onto a relational database later.

Duplicate protection uses ``INSERT ... ON CONFLICT(dedupe_key) DO UPDATE``: a
re-discovered business refreshes its data instead of creating a new row, and
non-empty existing values are never overwritten with empty ones.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional

from lead_finder_agent.models import Lead
from lead_finder_agent.storage.base import BaseLeadRepository, LeadFilter
from lead_finder_agent.utils.logging_utils import get_logger

log = get_logger("storage.sqlite")

SCHEMA_VERSION = 2

_SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    id                  TEXT PRIMARY KEY,
    dedupe_key          TEXT NOT NULL UNIQUE,
    business_name       TEXT NOT NULL,
    business_type       TEXT,
    country             TEXT,
    city                TEXT,
    address             TEXT,
    phone               TEXT,
    email               TEXT,
    source              TEXT,
    source_url          TEXT,
    website_url         TEXT,
    website_status      TEXT NOT NULL DEFAULT 'website_not_checked',
    website_quality     TEXT NOT NULL DEFAULT 'unknown',
    website_checked_at  TEXT,
    social_links        TEXT NOT NULL DEFAULT '{}',
    description         TEXT,
    latitude            REAL,
    longitude           REAL,
    business_status     TEXT NOT NULL DEFAULT 'unknown',
    review_count        INTEGER,
    rating              REAL,
    categories          TEXT NOT NULL DEFAULT '[]',
    sources             TEXT NOT NULL DEFAULT '[]',
    provider_ids        TEXT NOT NULL DEFAULT '{}',
    source_urls         TEXT NOT NULL DEFAULT '{}',
    raw                 TEXT NOT NULL DEFAULT '{}',
    lead_score          INTEGER NOT NULL DEFAULT 0,
    score_confidence    TEXT NOT NULL DEFAULT 'low',
    score_reason        TEXT NOT NULL DEFAULT '[]',
    score_breakdown     TEXT NOT NULL DEFAULT '{}',
    priority            TEXT NOT NULL DEFAULT 'cold',
    discovered_at       TEXT,
    last_checked_at     TEXT
);

CREATE INDEX IF NOT EXISTS idx_leads_score      ON leads(lead_score DESC);
CREATE INDEX IF NOT EXISTS idx_leads_city       ON leads(city);
CREATE INDEX IF NOT EXISTS idx_leads_website    ON leads(website_status);
CREATE INDEX IF NOT EXISTS idx_leads_source     ON leads(source);
CREATE INDEX IF NOT EXISTS idx_leads_discovered ON leads(discovered_at DESC);

CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

#: JSON columns whose empty value is a list.
_JSON_LIST_FIELDS = ("categories", "score_reason", "sources")

#: Every JSON-encoded column. Empty value is ``[]`` for the list fields above
#: and ``{}`` for the rest.
_JSON_FIELDS = (
    "social_links",
    "categories",
    "sources",
    "provider_ids",
    "source_urls",
    "raw",
    "score_reason",
    "score_breakdown",
)

#: Columns added after the first release. Existing databases are upgraded in
#: place by :meth:`SQLiteLeadRepository._migrate`, because ``CREATE TABLE IF
#: NOT EXISTS`` leaves an already-created table untouched.
_ADDED_COLUMNS = {
    "sources": "TEXT NOT NULL DEFAULT '[]'",
    "provider_ids": "TEXT NOT NULL DEFAULT '{}'",
    "source_urls": "TEXT NOT NULL DEFAULT '{}'",
}

_COLUMNS = (
    "id",
    "dedupe_key",
    "business_name",
    "business_type",
    "country",
    "city",
    "address",
    "phone",
    "email",
    "source",
    "source_url",
    "website_url",
    "website_status",
    "website_quality",
    "website_checked_at",
    "social_links",
    "description",
    "latitude",
    "longitude",
    "business_status",
    "review_count",
    "rating",
    "categories",
    "sources",
    "provider_ids",
    "source_urls",
    "raw",
    "lead_score",
    "score_confidence",
    "score_reason",
    "score_breakdown",
    "priority",
    "discovered_at",
    "last_checked_at",
)


class SQLiteLeadRepository(BaseLeadRepository):
    """A :class:`BaseLeadRepository` backed by SQLite."""

    def __init__(self, db_path: str | Path = "data/leads.db") -> None:
        self.db_path = Path(db_path)
        self._memory = str(db_path) == ":memory:"
        if not self._memory:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = self._connect()
        self._init_schema()

    # -- connection --------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        target = ":memory:" if self._memory else str(self.db_path)
        conn = sqlite3.connect(target, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_schema(self) -> None:
        self._conn.executescript(_SCHEMA)
        self._migrate()
        self._conn.execute(
            "INSERT OR REPLACE INTO schema_meta(key, value) VALUES ('version', ?)",
            (str(SCHEMA_VERSION),),
        )
        self._conn.commit()

    def _migrate(self) -> None:
        """Add columns introduced after a database was first created.

        ``CREATE TABLE IF NOT EXISTS`` is a no-op on an existing table, so a
        database written by an earlier version would otherwise be missing the
        provenance columns and every read would fail.
        """
        existing = {
            row["name"] for row in self._conn.execute("PRAGMA table_info(leads)").fetchall()
        }
        for column, definition in _ADDED_COLUMNS.items():
            if column not in existing:
                log.info("Adding missing column %s to leads", column)
                self._conn.execute(f"ALTER TABLE leads ADD COLUMN {column} {definition}")

    @contextmanager
    def _cursor(self) -> Iterator[sqlite3.Cursor]:
        cursor = self._conn.cursor()
        try:
            yield cursor
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        finally:
            cursor.close()

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:  # pragma: no cover - already closed
            pass

    # -- serialization -----------------------------------------------------

    @staticmethod
    def _to_row(lead: Lead) -> Dict[str, Any]:
        data = lead.to_dict()
        row: Dict[str, Any] = {}
        for column in _COLUMNS:
            value = data.get(column)
            if column in _JSON_FIELDS:
                if value is None:
                    value = [] if column in _JSON_LIST_FIELDS else {}
                value = json.dumps(value, ensure_ascii=False)
            row[column] = value
        if not row.get("id"):
            row["id"] = lead.dedupe_key
        if not row.get("dedupe_key"):
            row["dedupe_key"] = lead.dedupe_key
        return row

    @staticmethod
    def _from_row(row: sqlite3.Row) -> Lead:
        data = dict(row)
        for column in _JSON_FIELDS:
            raw = data.get(column)
            if isinstance(raw, str) and raw:
                try:
                    data[column] = json.loads(raw)
                except json.JSONDecodeError:
                    data[column] = [] if column in _JSON_LIST_FIELDS else {}
        return Lead.from_dict(data)

    # -- writes ------------------------------------------------------------

    def add(self, lead: Lead) -> Lead:
        """Insert or refresh a lead. Returns the stored lead."""
        row = self._to_row(lead)
        placeholders = ", ".join(f":{c}" for c in _COLUMNS)
        columns = ", ".join(_COLUMNS)
        # On conflict we keep existing non-empty values when the incoming record
        # is blank, so a re-check never blanks out previously known data.
        json_updates = []
        for column in _COLUMNS:
            if column in ("id", "dedupe_key"):
                continue
            if column in _JSON_FIELDS:
                json_updates.append(
                    f"{column} = CASE WHEN excluded.{column} IN ('{{}}', '[]', '') "
                    f"THEN {column} ELSE excluded.{column} END"
                )
            elif column in ("lead_score", "priority", "score_confidence"):
                json_updates.append(f"{column} = excluded.{column}")
            else:
                json_updates.append(
                    f"{column} = COALESCE(NULLIF(excluded.{column}, ''), {column})"
                )
        sql = (
            f"INSERT INTO leads ({columns}) VALUES ({placeholders}) "
            f"ON CONFLICT(dedupe_key) DO UPDATE SET {', '.join(json_updates)}"
        )
        with self._cursor() as cursor:
            cursor.execute(sql, row)
        return lead

    def add_many(self, leads: Iterable[Lead]) -> int:
        count = 0
        for lead in leads:
            self.add(lead)
            count += 1
        return count

    def delete(self, lead_id: str) -> bool:
        with self._cursor() as cursor:
            cursor.execute("DELETE FROM leads WHERE id = ? OR dedupe_key = ?", (lead_id, lead_id))
            return cursor.rowcount > 0

    def clear(self) -> None:
        with self._cursor() as cursor:
            cursor.execute("DELETE FROM leads")

    # -- reads -------------------------------------------------------------

    def get(self, lead_id: str) -> Optional[Lead]:
        cursor = self._conn.execute(
            "SELECT * FROM leads WHERE id = ? OR dedupe_key = ?", (lead_id, lead_id)
        )
        row = cursor.fetchone()
        return self._from_row(row) if row else None

    def exists(self, lead: Lead) -> bool:
        cursor = self._conn.execute(
            "SELECT 1 FROM leads WHERE dedupe_key = ? LIMIT 1", (lead.dedupe_key,)
        )
        return cursor.fetchone() is not None

    def count(self) -> int:
        cursor = self._conn.execute("SELECT COUNT(*) AS n FROM leads")
        return int(cursor.fetchone()["n"])

    def find(self, filters: Optional[LeadFilter] = None) -> List[Lead]:
        filters = filters or LeadFilter()
        clauses: List[str] = []
        params: Dict[str, Any] = {}

        if filters.city:
            clauses.append("LOWER(city) = LOWER(:city)")
            params["city"] = filters.city
        if filters.country:
            clauses.append("LOWER(country) = LOWER(:country)")
            params["country"] = filters.country
        if filters.business_type:
            clauses.append(
                "(LOWER(business_type) LIKE :btype OR LOWER(categories) LIKE :btype)"
            )
            params["btype"] = f"%{filters.business_type.lower()}%"
        if filters.source:
            clauses.append("LOWER(source) = LOWER(:source)")
            params["source"] = filters.source
        if filters.website_status:
            clauses.append("website_status = :website_status")
            params["website_status"] = str(filters.website_status)
        if filters.priority:
            clauses.append("priority = :priority")
            params["priority"] = str(filters.priority)
        if filters.min_score is not None:
            clauses.append("lead_score >= :min_score")
            params["min_score"] = filters.min_score
        if filters.max_score is not None:
            clauses.append("lead_score <= :max_score")
            params["max_score"] = filters.max_score
        if filters.has_phone is not None:
            clauses.append("phone IS NOT NULL AND phone != ''" if filters.has_phone else "(phone IS NULL OR phone = '')")

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"SELECT * FROM leads {where} ORDER BY {filters.resolved_order()}"
        if filters.limit is not None:
            sql += " LIMIT :limit OFFSET :offset"
            params["limit"] = int(filters.limit)
            params["offset"] = int(filters.offset)

        cursor = self._conn.execute(sql, params)
        return [self._from_row(row) for row in cursor.fetchall()]

    def all(self) -> List[Lead]:
        return self.find(LeadFilter(order_by="lead_score"))

    def top(self, limit: int = 10) -> List[Lead]:
        return self.find(LeadFilter(limit=limit, order_by="lead_score"))

    # -- introspection -----------------------------------------------------

    def stats(self) -> Dict[str, Any]:
        """Small summary used by the CLI's ``stats`` command."""
        total = self.count()
        with_site = self._conn.execute(
            "SELECT COUNT(*) AS n FROM leads WHERE website_status = 'website_exists'"
        ).fetchone()["n"]
        without_site = self._conn.execute(
            "SELECT COUNT(*) AS n FROM leads WHERE website_status = 'website_not_found'"
        ).fetchone()["n"]
        return {
            "total": total,
            "with_website": int(with_site),
            "confirmed_no_website": int(without_site),
            "db_path": str(self.db_path),
        }

    def __len__(self) -> int:
        return self.count()

    def __repr__(self) -> str:  # pragma: no cover - debugging sugar
        return f"<SQLiteLeadRepository db={self.db_path} count={self.count()}>"

    def __del__(self) -> None:  # pragma: no cover - interpreter shutdown
        try:
            self.close()
        except Exception:
            pass


__all__ = ["SQLiteLeadRepository", "SCHEMA_VERSION"]
