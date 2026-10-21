"""Company-name normalization, used for aliases and entity resolution."""

from __future__ import annotations

import re

# Legal-form words that carry no identity. Stripped only as trailing tokens, so a name like
# "Corporate Office Properties" keeps its first word.
_LEGAL_SUFFIXES = {
    "inc", "incorporated", "corp", "corporation", "co", "company", "ltd", "limited",
    "llc", "lp", "llp", "plc", "sa", "nv", "ag", "se",
}  # fmt: skip

_STATE_TAG = re.compile(r"/[a-z]{2,3}/?")  # EDGAR's '/DE/' style state-of-incorporation tags
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def normalize_company_name(name: str) -> str:
    """'REVLON INC /DE/' -> 'revlon'; 'Bed Bath & Beyond Inc.' -> 'bed bath and beyond'."""
    text = name.lower().replace("&", " and ")
    text = _STATE_TAG.sub(" ", text)
    tokens = _NON_ALNUM.sub(" ", text).split()
    if tokens and tokens[0] == "the":
        tokens = tokens[1:]
    while len(tokens) > 1 and tokens[-1] in _LEGAL_SUFFIXES:
        tokens.pop()
    return " ".join(tokens)
