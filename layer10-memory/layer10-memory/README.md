# Layer10 Take-Home: Grounded Long-Term Memory Graph

A pipeline that turns a corpus of organizational emails into a queryable, grounded memory graph — with deduplication, conflict tracking, and an interactive visualization.

---

## Quick start (no API key needed)

```bash
# 1. clone / unzip the repo
cd layer10-memory

# 2. run the full pipeline in demo mode
python ingest.py --demo

# 3. open the visualization
open viz/index.html          # macOS
# or just drag viz/index.html into your browser

# 4. run example retrieval queries
python retrieve.py --examples
```

To run extraction against a real model, get a **free** Groq key at [console.groq.com](https://console.groq.com) (no credit card needed), then:

```bash
export GROQ_API_KEY=gsk_...
python3 extract.py --corpus corpus/emails.json --out outputs/extracted.json
python3 ingest.py
```

Model used: `llama-3.3-70b-versatile` via Groq free tier.

---

## Corpus

**Enron Email Dataset** — the CMU/FERC release, widely used in NLP research.

Download:
- CMU: https://www.cs.cmu.edu/~enron/  (full maildir, ~423k emails)
- Kaggle mirror: https://www.kaggle.com/datasets/wcukierski/enron-email-dataset  (CSV, easier)

For this demo I use 20 hand-picked emails covering a single project thread (West Coast Power / WCP-1, December 2001). They capture the key challenges: forwarded duplicates, identity aliases, a decision that gets reversed twice, and an entity (PG&E) that's added then dropped as a counterparty.

To reproduce:
```bash
# kaggle CLI
kaggle datasets download -d wcukierski/enron-email-dataset
# then filter to the inboxes of skilling-j, lay-k, kitchen-l, etc.
# I've included a representative 20-email sample in corpus/emails.json
```

---

## Project structure

```
layer10-memory/
├── corpus/
│   └── emails.json          # 20 representative Enron emails
├── schema.py                # ontology: entity types, claim types, extraction prompt
├── extract.py               # LLM extraction pipeline (Anthropic API or --demo)
├── dedup.py                 # artifact dedup, entity canonicalization, claim merging
├── graph_store.py           # SQLite-backed memory graph
├── ingest.py                # orchestrates everything: extract → dedup → build graph
├── retrieve.py              # keyword retrieval, context packs, conflict surfacing
├── viz/index.html           # self-contained D3.js graph visualization
└── outputs/
    ├── extracted.json       # raw extraction results
    ├── memory.db            # SQLite graph
    ├── graph.json           # serialized graph for the viz
    └── example_queries.json # 5 example context packs
```

---

## Ontology

I kept the schema small and coherent rather than trying to cover everything at once. It can grow incrementally.

**Entity types**

| Type | Description |
|------|-------------|
| Person | Individuals — employees, contractors, external contacts |
| Project | Named initiatives or workstreams |
| Decision | A documented choice (including reversals) |
| Team | Org units or working groups |
| Document | Files, contracts, term sheets |
| Company | External organizations / counterparties |

**Claim types**

| Type | Meaning |
|------|---------|
| MADE_DECISION | Person (or group) made a Decision |
| ASSIGNED_TO | Person is assigned to a Project |
| PART_OF | Person is a member of a Team or Project |
| COUNTERPARTY_OF | Company is a counterparty on a Project |
| REVISES | Decision supersedes a prior Decision |
| CANCELS | Decision cancels a Project or prior Decision |
| AUTHORED | Person authored a Document |
| MENTIONED | Catch-all for softer associations |

Every claim has: `subject_id`, `object_id`, `value` (freetext gloss), `valid_from`, `valid_to` (null = current), `confidence`, and a list of evidence pointers.

---

## Extraction

The extraction prompt asks the model to produce typed entities and grounded claims from a single email. Each claim must include a verbatim excerpt and character offsets — not just a paraphrase. This is the grounding contract.

The model used is **Llama 3.3 70B** running on **Groq's free tier** — genuinely free, no credit card, fast inference. The API is OpenAI-compatible so swapping to another free model (Mixtral, Gemma, etc.) is a one-line change.

**Validation and repair**

- JSON is parsed with a regex fallback if the model wraps output in markdown fences
- Missing fields get defaults; `_invalid: true` is set on malformed records
- Retries with exponential backoff on rate limit errors
- Every result records `_extraction_version` and `_model` so we can backfill if the schema changes

**Confidence**

Claims start at whatever confidence the model assigns (0.7 default). When the same claim is corroborated by multiple emails during dedup, confidence is bumped by 0.05 per additional source (capped at 1.0).

**Quality gates**

In production I'd add:
1. A cross-evidence support requirement before a claim becomes durable: claims seen in only one source get a `needs_corroboration` flag
2. Decay: claims not re-observed within N days are soft-expired and marked for human review
3. A blocked list of high-noise extraction patterns (e.g. "I will look into this" → no decision claim)

---

## Deduplication

Three levels, each logged to the `merges` table so they can be audited and reversed.

**1. Artifact dedup**

Each email body is normalized (strip quoted lines, collapse whitespace) and hashed. Near-duplicates are caught with SimHash (64-bit); Hamming distance < 5 → same content. The earliest copy is kept; later copies are logged with `artifact_exact_dedup` or `artifact_near_dedup`.

In the demo corpus, `email_011` is an exact duplicate of `email_010` (same timestamp, different From address `l.kitchen` vs `louise.kitchen`), and `email_017` duplicates `email_016`. Both are caught.

**2. Entity canonicalization**

A two-step process:

1. Hard alias map: known identity pairs (`kenneth lay → Ken Lay`, `l.kitchen@enron.com → Louise Kitchen`, `WCP-1 → West Coast Power`, etc.)
2. Fuzzy fallback: SequenceMatcher ratio > 0.88 against the set of canonical names

Merges are logged with `entity_merge` events that record what aliases were added. To reverse a merge, restore the logged aliases and re-split the entity ID.

**3. Claim dedup**

Claims are fingerprinted by `(type, canonical_subject, canonical_object)`. Claims with the same fingerprint are merged: evidence from all copies is pooled, and the highest-confidence version is used as the primary. This means "Jeff approved the project" in email_001 and "Jeff gave the go-ahead" in email_007 merge into one claim if they resolve to the same triple.

**Conflicts and revisions**

`REVISES` and `CANCELS` claim types trigger `close_claim()` on the superseded claims, setting `valid_to` to the timestamp of the revising event. The graph therefore distinguishes:

- *current* claims (`valid_to IS NULL`) — what's true now
- *historical* claims (`valid_to SET`) — what used to be true

In the demo corpus, the Dec 5 go-decision is superseded by the Dec 14 pause, which is itself superseded by the Dec 18 cancellation. All three are in the graph; retrieval surfaces the supersession chain.

---

## Memory graph (SQLite)

Four tables:

```sql
entities  — canonical records; aliases as JSON array
claims    — typed relations; valid_from / valid_to for bi-temporal tracking
evidence  — grounded excerpts linked to claims; char offsets into source
merges    — audit log of all dedup/merge decisions
```

Writes are `INSERT OR REPLACE` keyed on stable IDs so re-ingestion is idempotent. All reads go through `GraphStore` methods — the schema is never queried directly from outside.

**Time semantics**

- `valid_from` = timestamp of the source artifact that asserted the claim
- `valid_to` = timestamp of the artifact that superseded it (or NULL)
- For "what's current", query `WHERE valid_to IS NULL`
- For "what was true on date X", query `WHERE valid_from <= X AND (valid_to IS NULL OR valid_to > X)`

**Permissions (conceptual)**

Each evidence row stores a `source_id`. In a real deployment, `source_id` would map to an access-controlled artifact. Before returning a claim to a user, we'd filter `evidence` to only sources that user can read — and suppress the claim entirely if no accessible evidence remains.

**Observability**

I'd log: extraction error rates per source type, claim merge rate (spikes = schema drift), entity fanout per extraction (high = hallucination), and claims without any evidence (should be zero).

---

## Retrieval

`retrieve.py` implements a simple keyword retrieval loop:

1. Tokenize the question; remove stop words
2. Score every entity and claim by token overlap (case-insensitive)
3. Boost claim scores by entity match scores of their subject/object
4. Weight by `confidence`
5. Pull evidence for the top-K claims
6. Surface superseded claims as explicit conflicts

The returned context pack includes: matched entities with aliases, ranked claims with current/historical status, evidence excerpts with source IDs and timestamps, and a conflict list.

In production the right move is dense retrieval: embed entities and claim values, then do ANN search over the vector index. Keyword is a reasonable baseline that's easy to debug and trace.

---

## Visualization

`viz/index.html` is a self-contained file — no server needed. It uses D3 v7 (CDN).

- **Graph view**: force-directed layout, node color by entity type, dashed edges for superseded claims
- **Filters**: by entity type, by search term, toggle superseded claims on/off
- **Entity panel**: click a node to see its aliases, attributes, related claims (with confidence bars and current/historical badges), and dedup log
- **Claim panel**: click an edge to see the grounding evidence (source ID, timestamp, verbatim excerpt), confidence, and validity window

---

## Adapting to Layer10's environment

Layer10's target is email, Slack/Teams, Jira/Linear, and docs. Here's what would change.

**Ontology additions**

Add `Thread` (a conversation across multiple messages), `Ticket` (structured issue with state machine), `Status` (enum attribute on Projects and Tickets), and `Mention` (soft reference, useful for Slack). The `REVISES` pattern maps naturally to Jira status transitions and PR reviews.

**Unstructured + structured fusion**

The key is cross-reference resolution: when an email says "see the Linear ticket" and includes a ticket ID, that's a `REFERENCES` edge between a Thread and a Ticket. In extraction, we'd train the model to emit `REFERENCES` claims aggressively and then resolve ticket IDs against the Jira/Linear API to hydrate attributes (status, assignee, priority) as structured facts.

**Extraction contract**

Slack messages are shorter and noisier — the extraction prompt would need few-shot examples tuned to that register. Jira descriptions are more structured, so we'd parse status transitions deterministically (not via LLM) and only use LLM for comment text and free-form fields.

**Long-term memory vs ephemeral context**

A claim becomes durable memory when it's corroborated by at least two independent sources, has confidence ≥ 0.85, and hasn't been retracted. Below that threshold, claims live in an "ephemeral zone" used for short-term context (current sprint, active threads) but not surfaced in historical queries. Decay: claims about ephemeral things (a Slack standup, a draft doc) have a shorter TTL than claims about structural things (org changes, project decisions).

**Grounding and deletions**

Every memory item must point to at least one accessible evidence source. If a source is deleted or redacted, we re-evaluate whether the claim can stand on remaining evidence. If not, it's soft-deleted from retrieval but kept in the audit log. This is why the evidence table is separate from the claim table.

**Permissions**

Evidence rows carry source ACLs inherited from the originating system (email recipient list, Slack channel membership, Jira project permissions). Retrieval filters evidence before returning claims. A claim that only has private evidence is invisible to users without access to those sources — even if it's structurally in the graph.

**Operational reality**

Scaling: extraction is embarrassingly parallel per message. Dedup at artifact level is fast (hash lookup). Entity and claim dedup are the bottleneck at scale — a blocking-key strategy (LSH or field-level sharding) keeps it manageable. Incremental updates: new messages are processed and merged without reprocessing old ones; only the affected claim fingerprints are re-evaluated. Evaluation: I'd want a labeled eval set of ~500 claims with human-annotated ground truth to track extraction precision/recall over time, and a separate set to test that revision chains are correctly ordered.

---

## What I'd do with more time

- **Better entity linking**: use an entity embedding model (e.g. a fine-tuned sentence-transformer) to catch aliases that pattern matching misses
- **Temporal reasoning**: parse relative dates ("end of Q1", "by Christmas") into absolute timestamps using the email's send date as anchor
- **Confidence calibration**: add a small held-out set to tune the confidence model — right now it's a heuristic
- **Web UI with Flask**: replace the static HTML with a server that queries the SQLite graph live, supports free-text search, and renders diffs between claim versions
- **Provenance diff view**: side-by-side comparison of two versions of the same claim (useful for auditing reversals)
