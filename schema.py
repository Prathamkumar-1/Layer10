# schema.py - entity types, claim types, and the extraction prompt

from dataclasses import dataclass, field
from typing import Optional
import time


ENTITY_TYPES = {
    "Person":    "An individual (employee, contractor, external contact).",
    "Project":   "A named initiative or workstream.",
    "Decision":  "A documented choice that was made (or reversed).",
    "Team":      "A group of people (org unit, working group).",
    "Document":  "A file, contract, term sheet, or report.",
    "Company":   "An external organization or counterparty.",
}


CLAIM_TYPES = {
    "SENT_MESSAGE":    "Person sent a message to Person(s).",
    "MADE_DECISION":   "Person (or group) made a Decision.",
    "REVISES":         "Decision supersedes or reverses a prior Decision.",
    "CANCELS":         "Decision cancels a Project or prior Decision.",
    "ASSIGNED_TO":     "Person is assigned to lead a Project.",
    "PART_OF":         "Person is part of a Team.",
    "MENTIONED":       "Entity is mentioned in the context of another entity.",
    "COUNTERPARTY_OF": "Company is a counterparty on a Project or Document.",
    "AUTHORED":        "Person authored a Document.",
    "REPORTED_TO":     "Person reports to another Person.",
}


@dataclass
class Evidence:
    source_id: str          # e.g. email_001
    excerpt: str            # verbatim snippet (≤ 300 chars)
    char_start: int         # character offset in the source body
    char_end: int
    timestamp: str          # ISO-8601 from the source
    extraction_model: str


@dataclass
class Entity:
    id: str
    type: str
    canonical_name: str
    aliases: list[str] = field(default_factory=list)
    attributes: dict = field(default_factory=dict)
    first_seen: str = ""
    last_seen: str = ""
    evidence: list[Evidence] = field(default_factory=list)


@dataclass
class Claim:
    id: str
    type: str
    subject_id: str
    object_id: Optional[str]
    value: Optional[str]
    valid_from: str
    valid_to: Optional[str]       # None = still current
    confidence: float
    evidence: list[Evidence] = field(default_factory=list)


# prompt template for the LLM
EXTRACTION_PROMPT = """
You are an information extraction engine. Given an email, extract entities and
claims according to the schema below. Be conservative - only extract things
clearly supported by the text.

ENTITY TYPES: Person, Project, Decision, Team, Document, Company

CLAIM TYPES:
  MADE_DECISION    (subject=Person, object=Decision)
  ASSIGNED_TO      (subject=Person, object=Project)
  COUNTERPARTY_OF  (subject=Company, object=Project)
  REVISES          (subject=Decision, object=Decision being revised)
  CANCELS          (subject=Decision, object=Project or Decision)
  PART_OF          (subject=Person, object=Team or Project)
  MENTIONED        (subject=Entity, object=Entity)

Return ONLY valid JSON matching this structure (no markdown fences):
{
  "entities": [
    {
      "type": "<type>",
      "name": "<canonical name>",
      "aliases": ["<alias>"],
      "attributes": {}
    }
  ],
  "claims": [
    {
      "type": "<CLAIM_TYPE>",
      "subject": "<entity name>",
      "object": "<entity name or null>",
      "value": "<short description>",
      "excerpt": "<verbatim quote ≤ 200 chars from the email body>",
      "char_start": <int>,
      "char_end": <int>,
      "confidence": <0.0–1.0>
    }
  ]
}

EMAIL METADATA:
  id: {email_id}
  from: {sender}
  to: {recipients}
  date: {date}
  subject: {subject}

EMAIL BODY:
{body}
""".strip()
