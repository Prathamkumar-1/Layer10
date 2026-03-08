"""retrieve.py - answer questions using the memory graph.

Does keyword matching against entities and claims, builds a ranked
context pack with evidence and conflict info.

    python retrieve.py "Who is leading the West Coast Power project?"
    python retrieve.py --examples
"""

import argparse
import json
import re
import sys
from graph_store import GraphStore



STOP = {
    "the", "a", "an", "is", "was", "are", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "shall", "can", "need", "dare", "ought",
    "who", "what", "when", "where", "why", "how", "which", "that", "this",
    "these", "those", "it", "its", "of", "in", "on", "at", "to", "for",
    "with", "by", "from", "about", "as", "into", "through", "during",
    "and", "or", "but", "not", "any", "all", "both", "each", "few",
}


def tokenize(text: str) -> list[str]:
    tokens = re.findall(r"\b\w+\b", text.lower())
    return [t for t in tokens if t not in STOP and len(t) > 2]


def score_text(query_tokens: list[str], text: str) -> float:
    if not text:
        return 0.0
    text_lower = text.lower()
    hits = sum(1 for t in query_tokens if t in text_lower)
    return hits / max(len(query_tokens), 1)


def retrieve(db: GraphStore, question: str, top_k: int = 8) -> dict:
    tokens = tokenize(question)

    # match entities
    entity_scores: dict[str, float] = {}
    all_entities = db.all_entities()
    for ent in all_entities:
        score = score_text(tokens, ent["canonical_name"])
        # also check aliases
        aliases = json.loads(ent.get("aliases") or "[]")
        for alias in aliases:
            score = max(score, score_text(tokens, alias) * 0.8)
        if score > 0:
            entity_scores[ent["id"]] = score

    matched_entities = sorted(
        [e for e in all_entities if e["id"] in entity_scores],
        key=lambda e: entity_scores[e["id"]],
        reverse=True,
    )[:5]

    # match claims
    all_claims = db.all_claims()
    claim_scores: dict[str, float] = {}
    for claim in all_claims:
        score = score_text(tokens, claim.get("value") or "")
        score += score_text(tokens, claim.get("type") or "") * 0.5
        # boost if subject/object entity was matched
        if claim.get("subject_id") in entity_scores:
            score += entity_scores[claim["subject_id"]] * 0.5
        if claim.get("object_id") in entity_scores:
            score += entity_scores.get(claim["object_id"], 0) * 0.5
        # weight by confidence
        score *= claim.get("confidence", 0.7)
        if score > 0:
            claim_scores[claim["id"]] = score

    matched_claims = sorted(
        [c for c in all_claims if c["id"] in claim_scores],
        key=lambda c: claim_scores[c["id"]],
        reverse=True,
    )[:top_k]

    # pull evidence
    evidence_items = []
    seen_excerpts: set[str] = set()
    for claim in matched_claims:
        evs = db.evidence_for_claim(claim["id"])
        for ev in evs:
            exc = (ev.get("excerpt") or "").strip()
            if exc and exc not in seen_excerpts:
                seen_excerpts.add(exc)
                evidence_items.append({
                    "source_id": ev["source_id"],
                    "excerpt": exc,
                    "source_ts": ev.get("source_ts", ""),
                    "claim_id": claim["id"],
                    "claim_type": claim["type"],
                    "claim_value": claim.get("value"),
                })

    # check for conflicts (superseded claims)
    conflicts = []
    closed = [c for c in matched_claims if c.get("valid_to")]
    for c in closed:
        conflicts.append({
            "note": "This claim was superseded",
            "claim_id": c["id"],
            "claim_type": c["type"],
            "value": c.get("value"),
            "valid_from": c.get("valid_from"),
            "valid_to": c.get("valid_to"),
        })

    return {
        "question": question,
        "matched_entities": [
            {
                "id": e["id"],
                "type": e["type"],
                "name": e["canonical_name"],
                "aliases": json.loads(e.get("aliases") or "[]"),
                "score": round(entity_scores.get(e["id"], 0), 3),
            }
            for e in matched_entities
        ],
        "matched_claims": [
            {
                "id": c["id"],
                "type": c["type"],
                "subject_id": c.get("subject_id"),
                "object_id": c.get("object_id"),
                "value": c.get("value"),
                "confidence": c.get("confidence"),
                "valid_from": c.get("valid_from"),
                "valid_to": c.get("valid_to"),
                "is_current": c.get("valid_to") is None,
                "score": round(claim_scores.get(c["id"], 0), 3),
            }
            for c in matched_claims
        ],
        "evidence": evidence_items,
        "conflicts": conflicts,
    }



def print_context_pack(pack: dict):
    print(f"\n{'='*60}")
    print(f"QUESTION: {pack['question']}")
    print(f"{'='*60}\n")

    if pack["matched_entities"]:
        print("MATCHED ENTITIES:")
        for e in pack["matched_entities"]:
            aliases = ", ".join(e["aliases"][:3]) if e["aliases"] else "none"
            print(f"  [{e['type']}] {e['name']}  (aliases: {aliases})")
        print()

    if pack["matched_claims"]:
        print("MATCHED CLAIMS:")
        for c in pack["matched_claims"]:
            status = "[current]" if c["is_current"] else f"[superseded {c['valid_to']}]"
            print(f"  [{c['type']}] {c['value']}")
            print(f"    confidence={c['confidence']:.2f}  {status}")
        print()

    if pack["evidence"]:
        print("SUPPORTING EVIDENCE:")
        for ev in pack["evidence"][:6]:
            print(f"  [{ev['source_id']} @ {ev['source_ts'][:10]}]")
            print(f"  \"{ev['excerpt'][:120]}\"")
            print()

    if pack["conflicts"]:
        print("CONFLICTS / REVISIONS:")
        for c in pack["conflicts"]:
            print(f"  {c['claim_type']}: \"{c['value']}\"")
            print(f"  -> valid {c['valid_from'][:10]} to {c['valid_to'][:10]}")
        print()



EXAMPLE_QUESTIONS = [
    "Who is leading the West Coast Power project?",
    "What decisions were made about the project capacity?",
    "Which counterparties were considered for the deal?",
    "What happened to the go decision from December 5?",
    "What is the status of the Raptors financing?",
]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("question", nargs="?", help="Question to answer")
    parser.add_argument("--db", default="outputs/memory.db")
    parser.add_argument("--out", help="Write context packs to JSON file")
    parser.add_argument("--examples", action="store_true", help="Run all example questions")
    args = parser.parse_args()

    db = GraphStore(args.db)

    questions = EXAMPLE_QUESTIONS if args.examples else [args.question]
    if not questions[0]:
        parser.print_help()
        sys.exit(1)

    packs = []
    for q in questions:
        pack = retrieve(db, q)
        print_context_pack(pack)
        packs.append(pack)

    if args.out:
        with open(args.out, "w") as f:
            json.dump(packs, f, indent=2)
        print(f"\nContext packs written to {args.out}")

    db.close()


if __name__ == "__main__":
    main()
