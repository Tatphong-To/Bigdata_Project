"""Fuzzy-match a TheMealDB ingredient name to a USDA FDC search result
(Phase 2b).

Given the raw ingredient name and the candidate :class:`UsdaFood` list from
:func:`food_pipeline.usda_client.UsdaClient.search_foods`, pick the best
candidate and attach a **confidence score in [0, 1]**.

Policy (CLAUDE.md, Phase 2b):
  * the acceptance threshold is **config, never hard-coded at call sites**
    (``MatchConfig.min_confidence``);
  * a best score **below the threshold is rejected** — the match is not used,
    and a WARNING is logged. Nothing is guessed;
  * low-confidence *accepted* matches (just above the threshold) are **always
    logged** too (INFO), so borderline estimates are visible.

No external fuzzy-match dependency — uses ``difflib`` + token overlap.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Iterable

from .usda_client import UsdaFood

logger = logging.getLogger(__name__)

_DEFAULT_NOISE = frozenset(
    {
        "raw", "fresh", "frozen", "dried", "canned", "cooked", "uncooked",
        "chopped", "sliced", "diced", "minced", "ground", "grated", "shredded",
        "whole", "large", "small", "medium", "extra", "lean", "boneless",
        "skinless", "ripe", "peeled", "unsalted", "salted", "organic",
        "of", "the", "a", "an", "and", "with", "without", "in", "for",
        "plain", "pure", "prepared", "all", "purpose",
    }
)

_PARENS = re.compile(r"\([^)]*\)")
_NON_ALNUM = re.compile(r"[^a-z0-9\s]")
_WS = re.compile(r"\s+")


@dataclass(frozen=True)
class MatchConfig:
    min_confidence: float = 0.6
    # accepted matches with confidence below this are still logged (INFO)
    log_accepted_below: float = 0.75
    noise_tokens: frozenset[str] = _DEFAULT_NOISE
    # only consider candidates whose macros are all present
    require_all_macros: bool = True


@dataclass(frozen=True)
class IngredientMatch:
    query: str
    accepted: bool
    confidence: float
    food: UsdaFood | None = None  # set iff accepted
    candidate_description: str | None = None  # best candidate seen (even if rejected)
    reason: str | None = None  # set iff not accepted


def _normalize(text: str, noise: frozenset[str]) -> tuple[str, frozenset[str]]:
    t = _PARENS.sub(" ", text.lower())
    t = t.replace("&", " and ")
    t = _NON_ALNUM.sub(" ", t)
    t = _WS.sub(" ", t).strip()
    tokens = [_singularize(tok) for tok in t.split(" ") if tok]
    kept = [tok for tok in tokens if tok not in noise]
    # if noise removal emptied it, fall back to the raw tokens
    final = kept or tokens
    return " ".join(final), frozenset(final)


def _singularize(token: str) -> str:
    if len(token) > 4 and token.endswith("ies"):
        return token[:-3] + "y"
    if len(token) > 3 and token.endswith("es") and token[-3] in "sxzo":
        return token[:-2]
    if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def score_names(query: str, candidate: str, config: MatchConfig | None = None) -> float:
    """Similarity in [0, 1] between an ingredient name and a food description."""
    cfg = config or MatchConfig()
    q_str, q_tok = _normalize(query, cfg.noise_tokens)
    c_str, c_tok = _normalize(candidate, cfg.noise_tokens)
    if not q_str or not c_str:
        return 0.0

    seq = SequenceMatcher(None, q_str, c_str).ratio()
    if q_tok | c_tok:
        jaccard = len(q_tok & c_tok) / len(q_tok | c_tok)
    else:
        jaccard = 0.0

    score = 0.5 * seq + 0.5 * jaccard
    if q_tok and q_tok == c_tok:
        score = max(score, 0.95)
    elif q_tok and (q_tok <= c_tok or c_tok <= q_tok):
        score += 0.05
    return round(min(score, 1.0), 4)


def match_ingredient(
    query: str,
    candidates: Iterable[UsdaFood],
    *,
    config: MatchConfig | None = None,
) -> IngredientMatch:
    cfg = config or MatchConfig()
    pool = [
        c for c in candidates
        if (not cfg.require_all_macros) or c.has_all_macros
    ]
    if not pool:
        m = IngredientMatch(
            query=query, accepted=False, confidence=0.0,
            reason="no USDA candidates with complete macros",
        )
        logger.warning("ingredient_matcher: %r -> no usable candidates", query)
        return m

    scored = sorted(
        ((score_names(query, c.description, cfg), c) for c in pool),
        key=lambda t: t[0],
        reverse=True,
    )
    best_score, best = scored[0]

    if best_score >= cfg.min_confidence:
        match = IngredientMatch(
            query=query,
            accepted=True,
            confidence=best_score,
            food=best,
            candidate_description=best.description,
        )
        if best_score < cfg.log_accepted_below:
            logger.info(
                "ingredient_matcher: LOW-CONFIDENCE match %r -> %r "
                "(fdcId=%s, confidence=%.2f, threshold=%.2f)",
                query, best.description, best.fdc_id, best_score, cfg.min_confidence,
            )
        return match

    logger.warning(
        "ingredient_matcher: REJECTED %r — best candidate %r "
        "(fdcId=%s) scored %.2f < threshold %.2f; not used",
        query, best.description, best.fdc_id, best_score, cfg.min_confidence,
    )
    return IngredientMatch(
        query=query,
        accepted=False,
        confidence=best_score,
        candidate_description=best.description,
        reason=f"best confidence {best_score:.2f} < threshold {cfg.min_confidence:.2f}",
    )
