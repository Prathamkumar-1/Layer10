# Layer10 Take-Home: Memory Graph from Emails

Pipeline that reads a corpus of emails and builds a queryable memory graph out of them. Handles dedup, tracks when decisions get reversed, and has a D3 visualization.

## How to run

```bash
# no API key needed for demo mode
python ingest.py --demo

# open the visualization
# just open viz/index.html in your browser

# run some example queries
python retrieve.py --examples
```

If you want to run real extraction (not just demo data):

```bash
export GROQ_API_KEY=gsk_...    # free at console.groq.com, no credit card
python extract.py --corpus corpus/emails.json --out outputs/extracted.json
python ingest.py
```

I used `llama-3.3-70b-versatile` on Groq's free tier. It's OpenAI-compatible so you could swap in any other model pretty easily.

## Project structure

```
corpus/emails.json       - 20 Enron emails (hand-picked thread about WCP-1 project)
schema.py                - entity/claim type definitions + extraction prompt
extract.py               - calls Groq API to pull entities+claims from each email
dedup.py                 - dedup at 3 levels: emails, entities, claims
graph_store.py           - SQLite wrapper for the memory graph
ingest.py                - ties it all together: extract -> dedup -> graph
retrieve.py              - keyword search, builds context packs for questions
viz/index.html           - D3.js interactive graph (self-contained, no server)
outputs/
  extracted.json         - raw extraction output
  memory.db              - the actual graph (SQLite)
  graph.json             - graph serialized for the viz
  example_queries.json   - 5 example context packs
```

## The corpus

I grabbed 20 emails from the Enron dataset (the CMU/FERC release). They're all from one project thread - West Coast Power (WCP-1), December 2001. I picked this subset because it has all the tricky cases:
- Forwarded duplicates (email_011 = email_010 from different addresses)
- People with multiple names/aliases (Ken Lay / Kenneth Lay / ken.lay@enron.com)
- A decision that gets made, paused, then cancelled
- A counterparty (PG&E) that gets added then dropped

You can get the full dataset from:
- https://www.cs.cmu.edu/~enron/ (full maildir)
- https://www.kaggle.com/datasets/wcukierski/enron-email-dataset (CSV, easier to work with)

## Ontology

I kept it small on purpose - 6 entity types and 8 claim types.

**Entities:** Person, Project, Decision, Team, Document, Company

**Claims:** MADE_DECISION, ASSIGNED_TO, PART_OF, COUNTERPARTY_OF, REVISES, CANCELS, AUTHORED, MENTIONED

Each claim has a subject, object, value (free text description), time window (valid_from/valid_to), confidence score, and pointers back to the source evidence. The key thing is that every claim has to point at a verbatim excerpt from the original email - that's how grounding works.

## Extraction

The LLM gets one email at a time and has to output structured JSON with entities and claims. Each claim needs a verbatim quote + character offsets from the email body, not just a summary.

I do some basic validation on the output:
- Strip markdown fences if the model wraps the JSON in them
- Try regex fallback if JSON parsing fails (find first `{...}` block)
- Fill in default values for missing fields
- Mark malformed records with `_invalid: true`
- Retry with backoff on rate limits

Each extraction also records the model version and extraction version, so if I change the prompt later I know which results came from which version.

For confidence: the model assigns an initial score, and during dedup if the same claim shows up from multiple emails, I bump it by 0.05 per extra source (capped at 1.0).

Things I'd add with more time:
- Require at least 2 sources before a claim becomes "durable"
- Auto-expire claims that haven't been re-seen in a while
- Block noisy patterns like "I will look into this" from becoming decision claims

## Deduplication

Three passes, all logged to a `merges` table so you can see what happened (and undo it if needed).

**1. Email dedup** - Normalize the body (strip quoted lines, collapse whitespace) and SHA-256 it. Also compute a SimHash (64-bit) and check Hamming distance < 5 for near-dupes. This catches email_011 being a duplicate of email_010 (same content, different From address).

**2. Entity dedup** - Two-step: first check a hardcoded alias map (e.g. "Kenneth Lay" -> "Ken Lay", email addresses -> names), then fall back to fuzzy matching with SequenceMatcher (threshold 0.88). All merges are logged with what aliases got added.

**3. Claim dedup** - Fingerprint each claim by (type, canonical_subject, canonical_object). Same fingerprint = same claim. Merge the evidence from all copies, keep the highest confidence version. If "Jeff approved the project" appears in two different emails, it becomes one claim with two evidence pointers.

**Handling revisions** - REVISES and CANCELS claims trigger `close_claim()` on whatever they supersede, which sets `valid_to`. So the graph has:
- Current claims (valid_to is NULL) = what's true now
- Historical claims (valid_to is set) = what used to be true

In the demo: Dec 5 go-decision -> Dec 14 pause -> Dec 18 cancellation. All three are in the graph, and retrieval shows the whole chain.

## Graph storage

SQLite with 4 tables: entities, claims, evidence, merges. All writes are idempotent (INSERT OR REPLACE on stable IDs), so you can rerun the pipeline without duplicating data.

Time works like this:
- `valid_from` = when the source email was sent
- `valid_to` = when something superseded it (NULL if still current)
- "What's true now?" -> `WHERE valid_to IS NULL`
- "What was true on Dec 10?" -> `WHERE valid_from <= X AND (valid_to IS NULL OR valid_to > X)`

In production you'd also want access control - each evidence row has a source_id that maps back to the original email, so you could filter by who has permission to see that source before returning claims.

## Retrieval

`retrieve.py` does keyword matching (no embeddings needed):

1. Tokenize the question, drop stop words
2. Score entities and claims by how many query tokens appear in them
3. Boost claims whose subject/object matched an entity
4. Weight by confidence
5. Pull evidence for top results
6. Flag superseded claims as conflicts

Output is a "context pack" with matched entities, ranked claims, evidence excerpts, and any conflicts. In production you'd want vector search here, but keyword works fine as a baseline and it's easy to debug.

## Visualization

`viz/index.html` - just open it in a browser, no server needed. Uses D3 v7 from CDN.

What it shows:
- Force-directed graph layout, nodes colored by entity type
- Dashed edges for superseded claims
- Click a node to see aliases, attributes, related claims with confidence bars
- Click an edge to see the grounding evidence (source email, timestamp, exact quote)
- Filter by entity type, search by name, toggle old/superseded stuff on/off

The graph data gets baked into the HTML when `ingest.py` runs, so it's always up to date.

## Adapting this for Layer10

Layer10 works with emails, Slack, Jira, docs, etc. Here's what I'd change:

**Schema changes** - Add Thread (conversation across messages), Ticket (Jira/Linear issue), Status. The REVISES pattern maps well to Jira status transitions.

**Cross-referencing** - When an email mentions a Linear ticket ID, that's a REFERENCES edge between a Thread and Ticket. Extract those aggressively, then hydrate from the Jira/Linear API.

**Source-specific extraction** - Slack messages are short and noisy, so the prompt needs different few-shot examples. Jira transitions are structured, so parse those deterministically and only use LLM on free-text fields.

**Durability** - A claim becomes long-term memory when it has 2+ independent sources, confidence >= 0.85, and hasn't been retracted. Below that, it stays in a short-term "ephemeral" zone for current-sprint context only.

**Deletions** - If a source gets deleted/redacted, check if the claim has other evidence. If not, soft-delete from retrieval but keep in the audit log.

**Permissions** - Each evidence row inherits ACLs from its source system (email recipients, Slack channel membership, Jira project access). Filter evidence before returning claims, and suppress claims entirely if the user can't see any of their evidence.

**Scaling** - Extraction is parallel per message. Dedup bottleneck is entity/claim matching at scale - use LSH or field-level sharding. New messages get processed incrementally without reprocessing old ones.

## What I'd do with more time

- Entity linking with sentence-transformers instead of just pattern matching
- Parse relative dates ("end of Q1") into absolute timestamps using the email date
- Calibrate confidence scores with a small labeled eval set
- Flask web UI that queries SQLite live instead of static HTML
- Side-by-side diff view for claim versions (useful for auditing reversals)
