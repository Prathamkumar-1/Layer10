# graph_store.py - SQLite-backed memory graph
#
# Tables: entities, claims, evidence, merges
# All writes are idempotent (INSERT OR REPLACE on stable IDs)

import json
import sqlite3
from pathlib import Path
from typing import Optional


DDL = """
CREATE TABLE IF NOT EXISTS entities (
    id              TEXT PRIMARY KEY,
    type            TEXT NOT NULL,
    canonical_name  TEXT NOT NULL,
    aliases         TEXT,          -- JSON array
    attributes      TEXT,          -- JSON object
    first_seen      TEXT,
    last_seen       TEXT,
    created_at      TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS claims (
    id              TEXT PRIMARY KEY,
    type            TEXT NOT NULL,
    subject_id      TEXT NOT NULL REFERENCES entities(id),
    object_id       TEXT REFERENCES entities(id),
    value           TEXT,
    valid_from      TEXT,
    valid_to        TEXT,
    confidence      REAL DEFAULT 0.7,
    created_at      TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (subject_id) REFERENCES entities(id),
    FOREIGN KEY (object_id)  REFERENCES entities(id)
);

CREATE TABLE IF NOT EXISTS evidence (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_id        TEXT REFERENCES claims(id),
    entity_id       TEXT REFERENCES entities(id),
    source_id       TEXT NOT NULL,   -- email_001, etc.
    excerpt         TEXT,
    char_start      INTEGER,
    char_end        INTEGER,
    source_ts       TEXT,            -- timestamp of the source artifact
    extraction_ver  TEXT
);

CREATE TABLE IF NOT EXISTS merges (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    merge_type      TEXT NOT NULL,
    details         TEXT,
    created_at      TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_claims_subject ON claims(subject_id);
CREATE INDEX IF NOT EXISTS idx_claims_object  ON claims(object_id);
CREATE INDEX IF NOT EXISTS idx_claims_type    ON claims(type);
CREATE INDEX IF NOT EXISTS idx_evidence_claim ON evidence(claim_id);
CREATE INDEX IF NOT EXISTS idx_evidence_src   ON evidence(source_id);
"""


class GraphStore:
    def __init__(self, db_path: str):
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(DDL)
        self.conn.commit()


    def upsert_entity(self, entity: dict):
        self.conn.execute(
            """
            INSERT INTO entities (id, type, canonical_name, aliases, attributes, first_seen, last_seen)
            VALUES (:id, :type, :canonical_name, :aliases, :attributes, :first_seen, :last_seen)
            ON CONFLICT(id) DO UPDATE SET
                aliases    = excluded.aliases,
                attributes = excluded.attributes,
                last_seen  = excluded.last_seen
            """,
            {
                "id": entity["id"],
                "type": entity["type"],
                "canonical_name": entity["canonical_name"],
                "aliases": json.dumps(entity.get("aliases", [])),
                "attributes": json.dumps(entity.get("attributes", {})),
                "first_seen": entity.get("first_seen", ""),
                "last_seen": entity.get("last_seen", ""),
            },
        )

    def get_entity(self, entity_id: str) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT * FROM entities WHERE id = ?", (entity_id,)
        ).fetchone()
        return dict(row) if row else None

    def all_entities(self) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM entities").fetchall()
        return [dict(r) for r in rows]


    def upsert_claim(self, claim: dict):
        self.conn.execute(
            """
            INSERT INTO claims (id, type, subject_id, object_id, value, valid_from, valid_to, confidence)
            VALUES (:id, :type, :subject_id, :object_id, :value, :valid_from, :valid_to, :confidence)
            ON CONFLICT(id) DO UPDATE SET
                confidence = excluded.confidence,
                valid_to   = excluded.valid_to
            """,
            {
                "id": claim["id"],
                "type": claim["type"],
                "subject_id": claim["subject_id"],
                "object_id": claim.get("object_id"),
                "value": claim.get("value"),
                "valid_from": claim.get("valid_from", ""),
                "valid_to": claim.get("valid_to"),
                "confidence": claim.get("confidence", 0.7),
            },
        )

    def close_claim(self, claim_id: str, valid_to: str):
        """Mark a claim as superseded."""
        self.conn.execute(
            "UPDATE claims SET valid_to = ? WHERE id = ?",
            (valid_to, claim_id),
        )

    def all_claims(self) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM claims").fetchall()
        return [dict(r) for r in rows]

    def claims_for_entity(self, entity_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM claims WHERE subject_id = ? OR object_id = ?",
            (entity_id, entity_id),
        ).fetchall()
        return [dict(r) for r in rows]


    def add_evidence(self, ev: dict):
        self.conn.execute(
            """
            INSERT INTO evidence (claim_id, entity_id, source_id, excerpt, char_start, char_end, source_ts, extraction_ver)
            VALUES (:claim_id, :entity_id, :source_id, :excerpt, :char_start, :char_end, :source_ts, :extraction_ver)
            """,
            {
                "claim_id": ev.get("claim_id"),
                "entity_id": ev.get("entity_id"),
                "source_id": ev["source_id"],
                "excerpt": ev.get("excerpt", ""),
                "char_start": ev.get("char_start", 0),
                "char_end": ev.get("char_end", 0),
                "source_ts": ev.get("source_ts", ""),
                "extraction_ver": ev.get("extraction_ver", ""),
            },
        )

    def evidence_for_claim(self, claim_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM evidence WHERE claim_id = ?", (claim_id,)
        ).fetchall()
        return [dict(r) for r in rows]


    def log_merge(self, merge: dict):
        self.conn.execute(
            "INSERT INTO merges (merge_type, details) VALUES (?, ?)",
            (merge.get("type", "unknown"), json.dumps(merge)),
        )

    def all_merges(self) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM merges ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    def search_entities(self, query: str) -> list[dict]:
        """Case-insensitive substring search on name + aliases."""
        q = f"%{query.lower()}%"
        rows = self.conn.execute(
            """
            SELECT * FROM entities
            WHERE lower(canonical_name) LIKE ?
               OR lower(aliases) LIKE ?
            ORDER BY canonical_name
            """,
            (q, q),
        ).fetchall()
        return [dict(r) for r in rows]

    def search_claims(self, query: str) -> list[dict]:
        q = f"%{query.lower()}%"
        rows = self.conn.execute(
            """
            SELECT c.*, e_sub.canonical_name AS subject_name, e_obj.canonical_name AS object_name
            FROM claims c
            LEFT JOIN entities e_sub ON c.subject_id = e_sub.id
            LEFT JOIN entities e_obj ON c.object_id  = e_obj.id
            WHERE lower(c.value) LIKE ?
               OR lower(e_sub.canonical_name) LIKE ?
               OR lower(e_obj.canonical_name) LIKE ?
            ORDER BY c.confidence DESC
            """,
            (q, q, q),
        ).fetchall()
        return [dict(r) for r in rows]

    def search_evidence(self, query: str) -> list[dict]:
        q = f"%{query.lower()}%"
        rows = self.conn.execute(
            """
            SELECT ev.*, c.type AS claim_type, c.value AS claim_value
            FROM evidence ev
            LEFT JOIN claims c ON ev.claim_id = c.id
            WHERE lower(ev.excerpt) LIKE ?
            ORDER BY ev.source_ts DESC
            """,
            (q,),
        ).fetchall()
        return [dict(r) for r in rows]

    def commit(self):
        self.conn.commit()

    def close(self):
        self.conn.close()

    def export_graph_json(self) -> dict:
        """Dump the whole graph as a dict for the viz."""
        entities = self.all_entities()
        claims = self.all_claims()

        # attach evidence to each claim
        for claim in claims:
            claim["evidence"] = self.evidence_for_claim(claim["id"])

        # attach claims to entities (for the side panel)
        entity_map = {e["id"]: e for e in entities}
        for e in entities:
            e["aliases"] = json.loads(e.get("aliases") or "[]")
            e["attributes"] = json.loads(e.get("attributes") or "{}")

        return {
            "entities": entities,
            "claims": claims,
            "merges": self.all_merges(),
        }
