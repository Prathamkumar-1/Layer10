"""
ingest.py - orchestrates extraction -> dedup -> graph build.

Usage:
    python ingest.py --demo          # no API key required
    python ingest.py                 # uses GROQ_API_KEY env var
"""

import argparse
import json
import sys
from pathlib import Path

from dedup import run_dedup, make_entity_id, resolve_entity_name, claim_fingerprint
from graph_store import GraphStore


DB_PATH = "outputs/memory.db"
GRAPH_JSON_PATH = "outputs/graph.json"


def load_emails(corpus_path: str) -> list[dict]:
    with open(corpus_path) as f:
        return json.load(f)


def load_extractions(path: str) -> list[dict]:
    with open(path) as f:
        return json.load(f)


def build_graph(emails: list[dict], extractions: list[dict], db_path: str) -> GraphStore:
    print("Running deduplication pass…")
    deduped = run_dedup(emails, extractions)

    db = GraphStore(db_path)

    # ── Write entities ────────────────────────────────────────────────────────
    print(f"Writing {len(deduped['canonical_entities'])} entities…")
    # figure out first/last seen from evidence
    entity_ts: dict[str, list[str]] = {}
    for extraction in extractions:
        src_ts = extraction.get("_timestamp", "")
        for ent in extraction.get("entities", []):
            name = resolve_entity_name(ent.get("name", ""))
            eid = make_entity_id(ent["type"], name)
            entity_ts.setdefault(eid, []).append(src_ts)

    for eid, ent in deduped["canonical_entities"].items():
        timestamps = sorted(entity_ts.get(eid, []))
        ent["first_seen"] = timestamps[0] if timestamps else ""
        ent["last_seen"] = timestamps[-1] if timestamps else ""
        db.upsert_entity(ent)

    # ── Write claims + evidence ───────────────────────────────────────────────
    print(f"Writing {len(deduped['merged_claims'])} claims…")

    # track REVISES/CANCELS to close superseded claims
    revision_pairs = []

    for claim in deduped["merged_claims"]:
        subject_name = claim.get("subject", "")
        object_name = claim.get("object")

        # need to figure out entity type for subject/object
        # look it up in canonical entities
        subject_id = _find_entity_id(subject_name, deduped["canonical_entities"])
        object_id = _find_entity_id(object_name, deduped["canonical_entities"]) if object_name else None

        # skip claims where subject entity isn't known
        if not subject_id:
            continue

        db_claim = {
            "id": claim["id"],
            "type": claim["type"],
            "subject_id": subject_id,
            "object_id": object_id,
            "value": claim.get("value"),
            "valid_from": claim.get("_timestamp", ""),
            "valid_to": None,
            "confidence": claim.get("confidence", 0.7),
        }
        db.upsert_claim(db_claim)

        # add evidence
        evidences = claim.get("all_evidence") or [
            {
                "source_id": claim.get("_source_id", "unknown"),
                "excerpt": claim.get("excerpt", ""),
                "char_start": claim.get("char_start", 0),
                "char_end": claim.get("char_end", 0),
                "timestamp": claim.get("_timestamp", ""),
            }
        ]
        for ev in evidences:
            db.add_evidence({
                "claim_id": claim["id"],
                "entity_id": subject_id,
                "source_id": ev.get("source_id", "unknown"),
                "excerpt": ev.get("excerpt", ""),
                "char_start": ev.get("char_start", 0),
                "char_end": ev.get("char_end", 0),
                "source_ts": ev.get("timestamp", ""),
                "extraction_ver": "v1.0",
            })

        if claim["type"] in ("REVISES", "CANCELS") and object_id:
            revision_pairs.append((claim["id"], object_id, db_claim["valid_from"]))

    # close superseded claims
    for _, obj_id, valid_to in revision_pairs:
        # find claims where this entity was the subject of a MADE_DECISION
        for c in db.claims_for_entity(obj_id):
            if c["type"] == "MADE_DECISION" and not c["valid_to"]:
                db.close_claim(c["id"], valid_to)

    # ── Write merge log ───────────────────────────────────────────────────────
    print(f"Writing {len(deduped['merge_log'])} merge events…")
    for merge in deduped["merge_log"]:
        db.log_merge(merge)

    db.commit()
    return db


def _find_entity_id(name: str, canonical: dict) -> str | None:
    if not name:
        return None
    resolved = resolve_entity_name(name)
    # try all entity types
    for etype in ["person", "project", "decision", "company", "team", "document"]:
        from dedup import make_entity_id
        eid = make_entity_id(etype.capitalize(), resolved)
        if eid in canonical:
            return eid
    # fallback: substring search
    resolved_lower = resolved.lower()
    for eid, ent in canonical.items():
        if resolved_lower in ent["canonical_name"].lower():
            return eid
    return None


def main():
    parser = argparse.ArgumentParser(description="Ingest emails into the memory graph.")
    parser.add_argument("--corpus", default="corpus/emails.json")
    parser.add_argument("--extractions", default="outputs/extracted.json")
    parser.add_argument("--db", default=DB_PATH)
    parser.add_argument("--demo", action="store_true", help="Run extraction in demo mode first")
    args = parser.parse_args()

    # if demo mode or extractions file doesn't exist, run extraction first
    extraction_path = Path(args.extractions)
    if args.demo or not extraction_path.exists():
        print("Running extraction in demo mode…")
        import subprocess
        flags = ["--demo"] if args.demo else []
        subprocess.run(
            [sys.executable, "extract.py", "--corpus", args.corpus,
             "--out", args.extractions] + flags,
            check=True,
        )

    emails = load_emails(args.corpus)
    extractions = load_extractions(args.extractions)

    print(f"Loaded {len(emails)} emails, {len(extractions)} extraction results.")

    db = build_graph(emails, extractions, args.db)

    # export graph JSON for visualization
    print(f"Exporting graph JSON → {GRAPH_JSON_PATH}")
    graph = db.export_graph_json()
    Path(GRAPH_JSON_PATH).parent.mkdir(parents=True, exist_ok=True)
    with open(GRAPH_JSON_PATH, "w") as f:
        json.dump(graph, f, indent=2)

    db.close()
    print(f"\nDone! Memory graph at: {args.db}")
    print(f"Graph JSON at:         {GRAPH_JSON_PATH}")
    print(f"\nEntities: {len(graph['entities'])}")
    print(f"Claims:   {len(graph['claims'])}")
    print(f"Merges:   {len(graph['merges'])}")


if __name__ == "__main__":
    main()
