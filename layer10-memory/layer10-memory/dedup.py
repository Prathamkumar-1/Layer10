"""
dedup.py — deduplication and canonicalization for the memory graph.

Three levels:
  1. Artifact dedup  — identical/near-identical source emails
  2. Entity dedup    — same person/project under different names
  3. Claim dedup     — same fact stated in multiple emails, merge evidence

All merges are recorded so they can be audited or reversed.
"""

import hashlib
import json
import re
import unicodedata
from collections import defaultdict
from difflib import SequenceMatcher


# ── 1. Artifact dedup ─────────────────────────────────────────────────────────

def body_fingerprint(text: str) -> str:
    """
    Content hash that's robust to minor formatting differences.
    Strip quoted sections (lines starting with >) and normalize whitespace.
    """
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(">") or stripped.startswith("----"):
            continue
        lines.append(stripped)
    normalized = " ".join(" ".join(lines).split())
    return hashlib.sha256(normalized.encode()).hexdigest()


def simhash(text: str, bits: int = 64) -> int:
    """
    Simple SimHash for near-duplicate detection.
    Returns an integer fingerprint; Hamming distance < 4 → likely duplicate.
    """
    tokens = re.findall(r"\w+", text.lower())
    v = [0] * bits
    for token in tokens:
        h = int(hashlib.md5(token.encode()).hexdigest(), 16)
        for i in range(bits):
            v[i] += 1 if (h >> i) & 1 else -1
    return sum(1 << i for i in range(bits) if v[i] > 0)


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def dedup_artifacts(emails: list[dict]) -> tuple[list[dict], list[dict]]:
    """
    Returns (deduplicated_emails, merge_log).
    Keeps the earliest copy; records all duplicates.
    """
    seen_exact: dict[str, str] = {}   # fingerprint → canonical email id
    seen_sim: list[tuple[int, str]] = []  # (simhash, email_id)
    kept = []
    merge_log = []

    for email in sorted(emails, key=lambda e: e["date"]):
        fp = body_fingerprint(email["body"])
        sh = simhash(email["body"])

        # exact match
        if fp in seen_exact:
            merge_log.append({
                "type": "artifact_exact_dedup",
                "duplicate_id": email["id"],
                "canonical_id": seen_exact[fp],
                "reason": "identical body fingerprint",
            })
            continue

        # near-duplicate
        near = None
        for other_sh, other_id in seen_sim:
            if hamming(sh, other_sh) < 5:
                near = other_id
                break

        if near:
            merge_log.append({
                "type": "artifact_near_dedup",
                "duplicate_id": email["id"],
                "canonical_id": near,
                "reason": f"SimHash Hamming distance < 5",
            })
            continue

        seen_exact[fp] = email["id"]
        seen_sim.append((sh, email["id"]))
        kept.append(email)

    return kept, merge_log


# ── 2. Entity canonicalization ────────────────────────────────────────────────

def normalize_name(name: str) -> str:
    """Lowercase, strip punctuation, normalize unicode."""
    name = unicodedata.normalize("NFKC", name).lower()
    name = re.sub(r"[^\w\s]", "", name)
    return re.sub(r"\s+", " ", name).strip()


# Known aliases that we hard-code (in production this would grow via feedback)
ALIAS_MAP = {
    "kenneth lay": "Ken Lay",
    "kenneth l lay": "Ken Lay",
    "ken": "Ken Lay",
    "jeff": "Jeff Skilling",
    "jeffrey skilling": "Jeff Skilling",
    "lavo": "John Lavorato",
    "louise": "Louise Kitchen",
    "l. kitchen": "Louise Kitchen",
    "wcp-1": "West Coast Power",
    "west coast power project": "West Coast Power",
    "pg&e": "PG&E",
    "pacific gas and electric": "PG&E",
    "socal edison": "SoCal Edison",
    "southern california edison": "SoCal Edison",
}

# Email → canonical name map (built from the corpus)
EMAIL_TO_PERSON = {
    "jeff.skilling@enron.com": "Jeff Skilling",
    "ken.lay@enron.com": "Ken Lay",
    "kenneth.lay@enron.com": "Ken Lay",
    "andy.fastow@enron.com": "Andy Fastow",
    "louise.kitchen@enron.com": "Louise Kitchen",
    "l.kitchen@enron.com": "Louise Kitchen",
    "greg.whalley@enron.com": "Greg Whalley",
    "tim.belden@enron.com": "Tim Belden",
    "john.lavorato@enron.com": "John Lavorato",
    "mark.frevert@enron.com": "Mark Frevert",
    "all.employees@enron.com": None,  # broadcast list, not a person
}


def resolve_entity_name(raw_name: str) -> str:
    """Return the canonical name for an entity, resolving aliases."""
    # try email address first
    if "@" in raw_name:
        resolved = EMAIL_TO_PERSON.get(raw_name.lower())
        if resolved:
            return resolved

    normalized = normalize_name(raw_name)
    if normalized in ALIAS_MAP:
        return ALIAS_MAP[normalized]

    # fuzzy fallback: check against known canonical names
    known = list(set(ALIAS_MAP.values()))
    best, best_score = None, 0.0
    for k in known:
        score = SequenceMatcher(None, normalized, normalize_name(k)).ratio()
        if score > best_score:
            best_score = score
            best = k

    if best_score > 0.88:
        return best

    # return title-cased original
    return raw_name.strip()


def make_entity_id(entity_type: str, canonical_name: str) -> str:
    slug = re.sub(r"\W+", "_", canonical_name.lower()).strip("_")
    return f"{entity_type.lower()}:{slug}"


def canonicalize_entities(raw_entities: list[dict]) -> tuple[dict, list[dict]]:
    """
    Merge raw entity mentions into a canonical entity registry.

    Returns:
        canonical: dict[entity_id → merged entity record]
        merge_log: list of merge events
    """
    canonical: dict[str, dict] = {}
    merge_log = []

    for ent in raw_entities:
        name = resolve_entity_name(ent.get("name", ""))
        if not name:
            continue
        eid = make_entity_id(ent["type"], name)

        if eid not in canonical:
            canonical[eid] = {
                "id": eid,
                "type": ent["type"],
                "canonical_name": name,
                "aliases": set(ent.get("aliases", [])),
                "attributes": dict(ent.get("attributes", {})),
                "evidence_sources": set(),
            }
        else:
            # merge aliases and attributes
            prev_name = canonical[eid]["canonical_name"]
            new_aliases = set(ent.get("aliases", []))
            added = new_aliases - canonical[eid]["aliases"]
            canonical[eid]["aliases"].update(new_aliases)
            canonical[eid]["attributes"].update(ent.get("attributes", {}))

            if added or ent["name"] != prev_name:
                merge_log.append({
                    "type": "entity_merge",
                    "canonical_id": eid,
                    "merged_name": ent["name"],
                    "added_aliases": list(added),
                })

    # make sets serializable
    for eid, ent in canonical.items():
        ent["aliases"] = sorted(ent["aliases"])
        ent["evidence_sources"] = sorted(ent["evidence_sources"])

    return canonical, merge_log


# ── 3. Claim dedup ────────────────────────────────────────────────────────────

def claim_fingerprint(claim_type: str, subject: str, obj: str | None) -> str:
    """Stable ID for a (type, subject, object) triple."""
    key = f"{claim_type}|{resolve_entity_name(subject)}|{resolve_entity_name(obj or '')}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def dedup_claims(raw_claims: list[dict]) -> tuple[list[dict], list[dict]]:
    """
    Merge claims with the same (type, subject, object) fingerprint.
    Keeps all evidence; picks the highest-confidence version as the primary.
    Returns (merged_claims, merge_log).
    """
    groups: dict[str, list[dict]] = defaultdict(list)
    for c in raw_claims:
        fp = claim_fingerprint(c["type"], c.get("subject", ""), c.get("object"))
        groups[fp].append(c)

    merged = []
    merge_log = []

    for fp, claims in groups.items():
        if len(claims) == 1:
            claims[0]["id"] = fp
            merged.append(claims[0])
            continue

        # pick highest confidence as primary
        primary = max(claims, key=lambda c: c.get("confidence", 0))
        primary = dict(primary)
        primary["id"] = fp

        # aggregate evidence from all copies
        primary["all_evidence"] = [
            {
                "source_id": c.get("_source_id", "unknown"),
                "excerpt": c.get("excerpt", ""),
                "char_start": c.get("char_start", 0),
                "char_end": c.get("char_end", 0),
                "timestamp": c.get("_timestamp", ""),
            }
            for c in claims
        ]
        # bump confidence if corroborated by multiple sources
        if len(claims) > 1:
            primary["confidence"] = min(1.0, primary.get("confidence", 0.7) + 0.05 * (len(claims) - 1))

        merge_log.append({
            "type": "claim_merge",
            "claim_id": fp,
            "claim_type": primary["type"],
            "source_count": len(claims),
            "sources": [c.get("_source_id") for c in claims],
        })
        merged.append(primary)

    return merged, merge_log


# ── Top-level entry point ─────────────────────────────────────────────────────

def run_dedup(
    emails: list[dict],
    extractions: list[dict],
) -> dict:
    """
    Full dedup pass. Returns a dict with deduplicated artifacts, canonical
    entities, merged claims, and the full merge log.
    """
    # 1. artifact dedup
    deduped_emails, artifact_log = dedup_artifacts(emails)

    # 2. collect all entity mentions with their source
    all_entity_mentions = []
    all_raw_claims = []
    for extraction in extractions:
        src = extraction.get("_source_id", "unknown")
        ts = extraction.get("_timestamp", "")

        for ent in extraction.get("entities", []):
            ent = dict(ent)
            ent["_source_id"] = src
            all_entity_mentions.append(ent)

        for claim in extraction.get("claims", []):
            claim = dict(claim)
            claim["_source_id"] = src
            claim["_timestamp"] = ts
            all_raw_claims.append(claim)

    # 3. entity canonicalization
    canonical_entities, entity_log = canonicalize_entities(all_entity_mentions)

    # resolve entity references inside claims
    for claim in all_raw_claims:
        claim["subject"] = resolve_entity_name(claim.get("subject", ""))
        if claim.get("object"):
            claim["object"] = resolve_entity_name(claim["object"])

    # 4. claim dedup
    merged_claims, claim_log = dedup_claims(all_raw_claims)

    return {
        "deduped_emails": deduped_emails,
        "canonical_entities": canonical_entities,
        "merged_claims": merged_claims,
        "merge_log": artifact_log + entity_log + claim_log,
    }


if __name__ == "__main__":
    import sys
    with open(sys.argv[1]) as f:
        data = json.load(f)
    print(json.dumps(run_dedup(data["emails"], data["extractions"]), indent=2, default=str))
