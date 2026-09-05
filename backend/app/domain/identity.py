"""
Patient identity helpers.

Uniqueness is layered on purpose:

  hard    external_id, the clinic's own patient number, UNIQUE and never null
  soft    normalised (first_name, last_name, date_of_birth) flags a probable
          duplicate for a human to confirm, rather than blocking outright
  none    phone is normalised and searchable but NOT unique

Phone is not a key because families share one mobile, so a unique constraint
would reject the second family member. Name is not a key because spelling and
transliteration vary, so it produces a different key for the same person.
Together they manage to both over-block and under-catch, which is why identity
is anchored on a system-owned id instead.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Optional

EXTERNAL_ID_PREFIX = "P"


def normalise_name(value: Optional[str]) -> str:
    """Casefold, strip accents, and collapse whitespace for comparison only.

    Used for duplicate detection. The original spelling is what gets stored.
    """
    if not value:
        return ""
    decomposed = unicodedata.normalize("NFKD", value)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", stripped).strip().casefold()


def normalise_phone(value: Optional[str]) -> Optional[str]:
    """Reduce a phone number to digits, keeping a leading + if present.

    Not validation: the goal is that '+91 98765 43210' and '+919876543210'
    compare equal when searching.
    """
    if not value:
        return None
    cleaned = re.sub(r"[^\d+]", "", value.strip())
    if not cleaned:
        return None
    if cleaned.startswith("+"):
        return "+" + re.sub(r"\D", "", cleaned)
    return re.sub(r"\D", "", cleaned)


def format_external_id(year: int, sequence: int) -> str:
    """Build a readable clinic patient number, e.g. 'P-2026-00042'.

    Readable on purpose: front-desk staff need something they can say aloud,
    which a UUID is not.
    """
    return f"{EXTERNAL_ID_PREFIX}-{year:04d}-{sequence:05d}"
