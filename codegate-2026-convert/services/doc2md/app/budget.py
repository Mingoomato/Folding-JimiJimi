"""Token estimation and routing, so a huge document cannot quietly blow the bill.

Feeding a whole converted document to a model costs tokens proportional to its
size, and the corpus is lopsided: one 710K-char 입찰안내서 is larger than the
other thirty files put together. Callers need to know *before* they spend
anything whether a document is worth processing whole.

No tokenizer dependency: every model tokenizes differently, and chunker.py
already settled on character counts for the same reason. Character-class weights
get within a rough factor of the truth, which is all a budget guard needs — it
answers "is this ~700K or ~70M", not "is this 712,340 exactly".

doc2md only *reports* the decision. It is a stateless HTTP service that does not
own an output directory, so moving files is the batch CLI's job (batch_cli.py).
"""

from __future__ import annotations

import os
import re

# Rough tokens-per-character by script. Korean and other CJK sit near one token
# per character; Latin text packs ~4 characters into a token.
_WEIGHTS = {
    "cjk": 1.0,
    "latin": 0.25,
    "digit": 0.5,
    "space": 0.25,
    "other": 0.5,
}

_CJK = re.compile(r"[가-힣ㄱ-ㅎㅏ-ㅣ一-鿿぀-ゟ゠-ヿ]")
_LATIN = re.compile(r"[A-Za-z]")
_DIGIT = re.compile(r"[0-9]")
_SPACE = re.compile(r"\s")

# gpt-nano 5.5's context. Deliberately an env var, not a constant: no file in
# the current corpus comes close (the largest is ~700K tokens, the whole corpus
# ~1.2M), so the useful threshold is whatever the operator's cost ceiling is,
# not the model's hard limit.
DEFAULT_BUDGET_TOKENS = int(os.getenv("DOC2MD_TOKEN_BUDGET", str(27_000_000)))

PROCESSED = "processed"
EXCEPTED = "excepted"
REASON_OVER_BUDGET = "token_budget_exceeded"


def estimate_tokens(text: str) -> int:
    """Approximate token count. Counts by regex rather than per-character so a
    700K-char document costs milliseconds."""
    if not text:
        return 0
    cjk = len(_CJK.findall(text))
    latin = len(_LATIN.findall(text))
    digit = len(_DIGIT.findall(text))
    space = len(_SPACE.findall(text))
    other = max(0, len(text) - cjk - latin - digit - space)
    return int(
        cjk * _WEIGHTS["cjk"]
        + latin * _WEIGHTS["latin"]
        + digit * _WEIGHTS["digit"]
        + space * _WEIGHTS["space"]
        + other * _WEIGHTS["other"]
    )


def decide(body: str, budget_tokens: int | None = None) -> tuple[int, int, str, str | None]:
    """Return ``(est_tokens, budget_tokens, routing, reason)``.

    Judged on the whole document, not per chunk — a 4000-char chunk can never
    exceed a budget, so a per-chunk check would never fire.

    An over-budget document is still converted and chunked in full; only its
    classification differs. The caller decides what to do with that.
    """
    budget = DEFAULT_BUDGET_TOKENS if budget_tokens is None else budget_tokens
    est = estimate_tokens(body)
    if est > budget:
        return est, budget, EXCEPTED, REASON_OVER_BUDGET
    return est, budget, PROCESSED, None
