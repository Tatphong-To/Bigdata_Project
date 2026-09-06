"""Layer C — deterministic matching / ranking (CLAUDE.md stage 4).

Given the safety-filtered candidates (stage 1) and the daily target (stage 2),
rank by distance to target, using the **precomputed** cluster assignments
(stage 3, read from `menu_catalog`). No learned model, no K-Means — plain
arithmetic, fully deterministic (stable order, ties broken by `menu_id`).

A recommended dish is treated as roughly one of `MEALS_PER_DAY` meals, so
candidates are scored against `daily_target / MEALS_PER_DAY`.

score(candidate) = W_ITEM   * normalized_distance(candidate_per_serving, meal_target)
                 + W_CLUSTER * cluster_term

  * cluster_term, when the candidate has a `cluster_id`, is the normalized
    distance between that cluster's mean per-serving macros (a GROUP BY AVG
    over the catalog — not ML) and the meal target: "does this dish's
    nutrition *group* fit the target".
  * a candidate with `cluster_id = None` (e.g. written before the catalog
    reached the K-Means gate) is NOT dropped and does NOT error — its
    cluster_term falls back to its own item distance plus a small fixed
    penalty, so it can still be recommended but ranks below an equally good
    clustered dish. The count of such fallbacks is returned for logging.

lower score = better fit.  match_score = 1 / (1 + score), rounded.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

MEALS_PER_DAY = 3

W_ITEM = 0.6
W_CLUSTER = 0.4
NULL_CLUSTER_PENALTY = 0.15  # applied to the cluster term only

_MACROS = ("calories", "protein_g", "carbs_g", "fat_g")


@dataclass(frozen=True)
class ScoredCandidate:
    menu_id: str
    name: str
    match_score: float
    nutrition: dict[str, float]
    score: float
    cluster_id: int | None
    used_cluster_fallback: bool


@dataclass(frozen=True)
class RankingResult:
    ranked: list[ScoredCandidate]
    null_cluster_count: int  # candidates ranked without a real cluster term


def _normalized_distance(item: dict[str, float], target: dict[str, float]) -> float:
    """Root-sum-square of per-macro relative errors. Each denominator is
    floored at 1.0 so a near-zero target macro can't explode the term."""
    acc = 0.0
    for m in _MACROS:
        denom = max(abs(target.get(m, 0.0)), 1.0)
        acc += ((item.get(m, 0.0) - target.get(m, 0.0)) / denom) ** 2
    return math.sqrt(acc)


def per_meal_target(daily_target: dict[str, float]) -> dict[str, float]:
    return {m: daily_target.get(m, 0.0) / MEALS_PER_DAY for m in _MACROS}


def rank_candidates(
    candidates: list[dict],
    daily_target: dict[str, float],
    cluster_centroids: dict[int, dict[str, float]],
    *,
    top_n: int = 10,
) -> RankingResult:
    """`candidates`: dicts with menu_id, name, calories, protein_g, carbs_g,
    fat_g, cluster_id. `cluster_centroids`: cluster_id -> mean per-serving
    macros. Returns the top `top_n`, best first."""
    meal_target = per_meal_target(daily_target)
    scored: list[ScoredCandidate] = []
    null_cluster = 0

    for c in candidates:
        nutrition = {m: float(c.get(m, 0.0) or 0.0) for m in _MACROS}
        d_item = _normalized_distance(nutrition, meal_target)

        cid = c.get("cluster_id")
        centroid = cluster_centroids.get(cid) if cid is not None else None
        if centroid is not None:
            d_cluster = _normalized_distance(centroid, meal_target)
            fallback = False
        else:
            d_cluster = d_item + NULL_CLUSTER_PENALTY
            fallback = True
            null_cluster += 1

        score = W_ITEM * d_item + W_CLUSTER * d_cluster
        scored.append(
            ScoredCandidate(
                menu_id=str(c["menu_id"]),
                name=str(c.get("name") or ""),
                match_score=round(1.0 / (1.0 + score), 4),
                nutrition=nutrition,
                score=score,
                cluster_id=cid if cid is None else int(cid),
                used_cluster_fallback=fallback,
            )
        )

    # deterministic: best (lowest) score first, ties broken by menu_id
    scored.sort(key=lambda s: (s.score, s.menu_id))
    return RankingResult(ranked=scored[:top_n], null_cluster_count=null_cluster)
