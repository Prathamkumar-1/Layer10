"""extract.py - LLM extraction of entities and claims from emails.

Uses Groq free tier (llama-3.3-70b-versatile).
Get a key at: https://console.groq.com  (free, no credit card)

    export GROQ_API_KEY=gsk_...
    python3 extract.py --corpus corpus/emails.json --out outputs/extracted.json
    python3 extract.py --demo
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

from schema import EXTRACTION_PROMPT

# groq config
MODEL = "llama-3.3-70b-versatile"
API_URL = "https://api.groq.com/openai/v1/chat/completions"
MAX_TOKENS = 1500
RETRY_LIMIT = 3
EXTRACTION_VERSION = "v1.0"


def call_llm(prompt: str, api_key: str) -> str:
    payload = json.dumps({
        "model": MODEL,
        "max_tokens": MAX_TOKENS,
        "temperature": 0.0,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()

    req = urllib.request.Request(
        API_URL,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
    )

    for attempt in range(RETRY_LIMIT):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
                return data["choices"][0]["message"]["content"]
        except urllib.error.HTTPError as e:
            body = e.read().decode()
            if e.code == 429:
                wait = 2 ** attempt
                print(f"  rate limited, waiting {wait}s...", file=sys.stderr)
                time.sleep(wait)
            else:
                raise RuntimeError(f"API error {e.code}: {body}") from e
        except Exception as e:
            if attempt == RETRY_LIMIT - 1:
                raise
            time.sleep(1)
    raise RuntimeError("Exceeded retry limit")



def parse_and_validate(raw: str, email_id: str) -> dict:
    """Try to parse JSON from the model output. Do light repair if needed."""
    # strip any accidental markdown fences
    raw = re.sub(r"^```[a-z]*\n?", "", raw.strip(), flags=re.MULTILINE)
    raw = re.sub(r"```$", "", raw.strip())

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        # last-ditch: find the first {...} block
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            try:
                data = json.loads(m.group())
            except Exception:
                print(f"  [warn] could not parse output for {email_id}: {e}", file=sys.stderr)
                return {"entities": [], "claims": [], "_parse_error": str(e)}
        else:
            return {"entities": [], "claims": [], "_parse_error": str(e)}

    # minimal field enforcement
    data.setdefault("entities", [])
    data.setdefault("claims", [])

    for ent in data["entities"]:
        ent.setdefault("aliases", [])
        ent.setdefault("attributes", {})
        if "type" not in ent or "name" not in ent:
            ent["_invalid"] = True

    for claim in data["claims"]:
        claim.setdefault("object", None)
        claim.setdefault("value", None)
        claim.setdefault("confidence", 0.7)
        claim.setdefault("char_start", 0)
        claim.setdefault("char_end", 0)
        claim.setdefault("excerpt", "")

    return data



def extract_email(email: dict, api_key: str) -> dict:
    prompt = EXTRACTION_PROMPT.format(
        email_id=email["id"],
        sender=email["from"],
        recipients=", ".join(email.get("to", [])),
        date=email["date"],
        subject=email["subject"],
        body=email["body"],
    )
    raw = call_llm(prompt, api_key)
    result = parse_and_validate(raw, email["id"])
    result["_source_id"] = email["id"]
    result["_timestamp"] = email["date"]
    result["_extraction_version"] = EXTRACTION_VERSION
    result["_model"] = MODEL
    return result


def extract_corpus(emails: list[dict], api_key: str) -> list[dict]:
    results = []
    for i, email in enumerate(emails):
        print(f"  [{i+1}/{len(emails)}] extracting {email['id']} ...")
        try:
            result = extract_email(email, api_key)
        except Exception as e:
            print(f"  [error] {email['id']}: {e}", file=sys.stderr)
            result = {
                "_source_id": email["id"],
                "_timestamp": email["date"],
                "entities": [],
                "claims": [],
                "_error": str(e),
            }
        results.append(result)
        time.sleep(0.3)  # rate limit
    return results


# pre-seeded demo data so you can run without an API key

DEMO_EXTRACTIONS = [
    {
        "_source_id": "email_001", "_timestamp": "2001-12-05T13:45:12Z",
        "_extraction_version": "v1.0", "_model": "demo",
        "entities": [
            {"type": "Person", "name": "Jeff Skilling", "aliases": ["jeff.skilling@enron.com", "Jeff"], "attributes": {"role": "CEO"}},
            {"type": "Person", "name": "Ken Lay", "aliases": ["ken.lay@enron.com", "Ken"], "attributes": {"role": "Chairman"}},
            {"type": "Person", "name": "Andy Fastow", "aliases": ["andy.fastow@enron.com", "Andy"], "attributes": {"role": "CFO"}},
            {"type": "Person", "name": "Greg Whalley", "aliases": ["greg.whalley@enron.com", "Greg"], "attributes": {"role": "President"}},
            {"type": "Person", "name": "Louise Kitchen", "aliases": ["louise.kitchen@enron.com", "Louise"], "attributes": {"role": "COO"}},
            {"type": "Project", "name": "West Coast Power", "aliases": ["WCP-1", "West Coast Power project"], "attributes": {"target_mw": "800", "budget": "$200M"}},
            {"type": "Decision", "name": "WCP-1 Go Decision", "aliases": [], "attributes": {"date": "2001-12-05"}},
            {"type": "Document", "name": "Raptors III Term Sheet", "aliases": ["Raptors financing"], "attributes": {}},
        ],
        "claims": [
            {"type": "MADE_DECISION", "subject": "Jeff Skilling", "object": "WCP-1 Go Decision", "value": "decided to move forward with West Coast Power project at 800 MW", "excerpt": "I've decided we should move forward with the West Coast Power project. Target capacity is 800 MW.", "char_start": 78, "char_end": 163, "confidence": 0.97},
            {"type": "ASSIGNED_TO", "subject": "Louise Kitchen", "object": "West Coast Power", "value": "assigned as project lead with full authority to negotiate contracts", "excerpt": "Assigning project lead to Louise Kitchen. She has full authority to negotiate contracts.", "char_start": 264, "char_end": 351, "confidence": 0.97},
            {"type": "ASSIGNED_TO", "subject": "Andy Fastow", "object": "Raptors III Term Sheet", "value": "tasked with closing Raptors financing by end of Q1", "excerpt": "We'll need Andy to close the Raptors financing by end of Q1.", "char_start": 164, "char_end": 222, "confidence": 0.9},
        ],
    },
    {
        "_source_id": "email_002", "_timestamp": "2001-12-05T15:18:23Z",
        "_extraction_version": "v1.0", "_model": "demo",
        "entities": [
            {"type": "Person", "name": "Ken Lay", "aliases": ["ken.lay@enron.com", "Ken"], "attributes": {}},
            {"type": "Person", "name": "Jeff Skilling", "aliases": [], "attributes": {}},
            {"type": "Person", "name": "Greg Whalley", "aliases": [], "attributes": {}},
            {"type": "Project", "name": "West Coast Power", "aliases": [], "attributes": {"budget_confirmed": "$200M"}},
        ],
        "claims": [
            {"type": "MADE_DECISION", "subject": "Ken Lay", "object": "WCP-1 Go Decision", "value": "Board approved up to $200M for West Coast Power", "excerpt": "The Board approved up to $200M for this.", "char_start": 24, "char_end": 64, "confidence": 0.95},
        ],
    },
    {
        "_source_id": "email_003", "_timestamp": "2001-12-06T09:23:41Z",
        "_extraction_version": "v1.0", "_model": "demo",
        "entities": [
            {"type": "Person", "name": "Louise Kitchen", "aliases": [], "attributes": {}},
            {"type": "Person", "name": "Tim Belden", "aliases": ["tim.belden@enron.com", "Tim"], "attributes": {"role": "Trading Strategy"}},
            {"type": "Person", "name": "John Lavorato", "aliases": ["john.lavorato@enron.com", "Lavo"], "attributes": {"role": "Operations"}},
            {"type": "Person", "name": "Mark Frevert", "aliases": ["mark.frevert@enron.com"], "attributes": {}},
            {"type": "Project", "name": "West Coast Power", "aliases": ["WCP-1"], "attributes": {"milestone": "counterparty term sheets by Dec 20"}},
        ],
        "claims": [
            {"type": "PART_OF", "subject": "Tim Belden", "object": "West Coast Power", "value": "assigned to trading strategy", "excerpt": "pulling in Tim Belden for trading strategy, John Lavorato for ops", "char_start": 211, "char_end": 276, "confidence": 0.92},
            {"type": "PART_OF", "subject": "John Lavorato", "object": "West Coast Power", "value": "assigned to operations", "excerpt": "pulling in Tim Belden for trading strategy, John Lavorato for ops", "char_start": 211, "char_end": 276, "confidence": 0.92},
        ],
    },
    {
        "_source_id": "email_005", "_timestamp": "2001-12-07T08:30:12Z",
        "_extraction_version": "v1.0", "_model": "demo",
        "entities": [
            {"type": "Person", "name": "Greg Whalley", "aliases": [], "attributes": {}},
            {"type": "Decision", "name": "WCP-1 Risk Envelope Decision", "aliases": [], "attributes": {"mw_primary": "800", "mw_fallback": "620", "fallback_trigger": "Feb 1"}},
        ],
        "claims": [
            {"type": "MADE_DECISION", "subject": "Greg Whalley", "object": "WCP-1 Risk Envelope Decision", "value": "hold at 800 MW with Nevada corridor fallback at 620 MW if transmission constrained by Feb 1", "excerpt": "we hold at 800 MW but add a Nevada corridor option as fallback. If transmission is constrained by Feb 1, we automatically drop to 620 MW.", "char_start": 148, "char_end": 283, "confidence": 0.96},
        ],
    },
    {
        "_source_id": "email_010", "_timestamp": "2001-12-12T11:30:45Z",
        "_extraction_version": "v1.0", "_model": "demo",
        "entities": [
            {"type": "Person", "name": "Louise Kitchen", "aliases": ["l.kitchen@enron.com"], "attributes": {}},
            {"type": "Company", "name": "PG&E", "aliases": ["Pacific Gas and Electric"], "attributes": {"status": "dropped"}},
            {"type": "Company", "name": "Nevada Power", "aliases": [], "attributes": {"status": "active counterparty"}},
            {"type": "Company", "name": "SoCal Edison", "aliases": ["Southern California Edison"], "attributes": {"status": "active counterparty"}},
            {"type": "Decision", "name": "Drop PG&E Decision", "aliases": [], "attributes": {"date": "2001-12-12"}},
        ],
        "claims": [
            {"type": "MADE_DECISION", "subject": "Louise Kitchen", "object": "Drop PG&E Decision", "value": "dropped PG&E from scope due to Chapter 11; focus on Nevada Power and SoCal Edison", "excerpt": "Agreed on PG&E - dropping them from scope. Confirmed targets are Nevada Power and SoCal Edison.", "char_start": 0, "char_end": 95, "confidence": 0.98},
            {"type": "COUNTERPARTY_OF", "subject": "Nevada Power", "object": "West Coast Power", "value": "active counterparty at $42/MWh", "excerpt": "SoCal Edison and Nevada Power both interested at $42/MWh", "char_start": 210, "char_end": 266, "confidence": 0.88},
            {"type": "COUNTERPARTY_OF", "subject": "SoCal Edison", "object": "West Coast Power", "value": "active counterparty at $42/MWh", "excerpt": "SoCal Edison and Nevada Power both interested at $42/MWh", "char_start": 210, "char_end": 266, "confidence": 0.88},
        ],
    },
    {
        "_source_id": "email_012", "_timestamp": "2001-12-14T09:45:00Z",
        "_extraction_version": "v1.0", "_model": "demo",
        "entities": [
            {"type": "Person", "name": "Ken Lay", "aliases": ["Kenneth Lay", "kenneth.lay@enron.com"], "attributes": {}},
            {"type": "Decision", "name": "WCP-1 Pause Decision", "aliases": [], "attributes": {"date": "2001-12-14", "authority": "Board"}},
        ],
        "claims": [
            {"type": "MADE_DECISION", "subject": "Ken Lay", "object": "WCP-1 Pause Decision", "value": "Board paused all capital commitments over $100M; WCP-1 on hold", "excerpt": "Board had an emergency session this morning. Given Enron's current liquidity situation, they're asking us to pause all new capital commitments over $100M", "char_start": 23, "char_end": 176, "confidence": 0.97},
            {"type": "REVISES", "subject": "WCP-1 Pause Decision", "object": "WCP-1 Go Decision", "value": "pause decision reverses the Dec 5 go-decision", "excerpt": "WCP-1 is on hold. Do not sign any contracts.", "char_start": 198, "char_end": 243, "confidence": 0.95},
        ],
    },
    {
        "_source_id": "email_018", "_timestamp": "2001-12-18T14:15:00Z",
        "_extraction_version": "v1.0", "_model": "demo",
        "entities": [
            {"type": "Person", "name": "Greg Whalley", "aliases": [], "attributes": {}},
            {"type": "Decision", "name": "WCP-1 Cancellation", "aliases": [], "attributes": {"date": "2001-12-18", "reason": "Chapter 11 filing"}},
        ],
        "claims": [
            {"type": "CANCELS", "subject": "WCP-1 Cancellation", "object": "West Coast Power", "value": "project cancelled after Enron filed Chapter 11", "excerpt": "WCP-1 is cancelled, not just paused. Enron filed Chapter 11 this morning. The project is dead.", "char_start": 37, "char_end": 131, "confidence": 0.99},
            {"type": "REVISES", "subject": "WCP-1 Cancellation", "object": "WCP-1 Pause Decision", "value": "cancellation replaces earlier pause", "excerpt": "WCP-1 is cancelled, not just paused.", "char_start": 37, "char_end": 73, "confidence": 0.95},
        ],
    },
]


def main():
    parser = argparse.ArgumentParser(description="Extract entities and claims from emails.")
    parser.add_argument("--corpus", default="corpus/emails.json")
    parser.add_argument("--out", default="outputs/extracted.json")
    parser.add_argument("--demo", action="store_true", help="Use pre-seeded demo data (no API key needed)")
    args = parser.parse_args()

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)

    if args.demo:
        print("Running in demo mode, using pre-seeded extractions.")
        results = DEMO_EXTRACTIONS
    else:
        api_key = os.environ.get("GROQ_API_KEY")
        if not api_key:
            print("Error: set GROQ_API_KEY or use --demo flag.", file=sys.stderr)
            print("Get a free key at: https://console.groq.com", file=sys.stderr)
            sys.exit(1)

        with open(args.corpus) as f:
            emails = json.load(f)

        print(f"Extracting {len(emails)} emails with {MODEL}...")
        results = extract_corpus(emails, api_key)

    with open(args.out, "w") as f:
        json.dump(results, f, indent=2)

    total_entities = sum(len(r.get("entities", [])) for r in results)
    total_claims = sum(len(r.get("claims", [])) for r in results)
    print(f"Done. {total_entities} entities, {total_claims} claims -> {args.out}")


if __name__ == "__main__":
    main()
