"""The /recommend business logic — the four CLAUDE.md stages, in order.

    1. safety filter (Phase 4, rule-based)  — ALWAYS runs
    2. Layer A calculator (Mifflin-St Jeor) — daily target
    3. read precomputed cluster_id          — no training here
    4. Layer C deterministic ranking

Stages never reorder / merge / skip. K-Means is not imported anywhere in this
package.
"""

from __future__ import annotations

import logging
from typing import Protocol

from food_pipeline.safety_filter import (
    MenuItem,
    apply_safety_filter,
    parse_restrictions,
)

from .calculator import compute_daily_target
from .catalog import Candidate
from .ranking import rank_candidates
from .schemas import (
    DailyTargetModel,
    Nutrition,
    Recommendation,
    RecommendRequest,
    RecommendResponse,
)

logger = logging.getLogger("model_service.pipeline")


class CatalogPort(Protocol):
    def candidates(self) -> list[Candidate]: ...
    def cluster_centroids(self) -> dict[int, dict[str, float]]: ...
    def model_version(self) -> tuple[str, bool]: ...
    def log_prediction(self, record: dict) -> None: ...


def _menu_item(c: Candidate) -> MenuItem:
    # Feed the recipe NAME to the filter as extra scannable text alongside the
    # ingredient list. Names like "Beef Burrito" / "Peanut Satay" carry diet /
    # allergen signal that the ingredient text sometimes phrases unusually
    # (e.g. "stew meat", "sandwich steaks"). This does not change the Phase 4
    # filter logic — it only gives it more text to match against, which is the
    # safe direction for a stage-1 safety check.
    return MenuItem(
        menu_id=c.menu_id,
        name=c.name,
        ingredients=(*c.ingredients, c.name),
        diet_tags=c.diet_tags,
    )


def recommend(request: RecommendRequest, catalog: CatalogPort) -> RecommendResponse:
    candidates = catalog.candidates()

    # -- stage 1: safety filter (always runs, even with empty restrictions) --
    restrictions = parse_restrictions(
        allergies=request.restrictions.allergies,
        diet=request.restrictions.diet_type,
    )
    safety = apply_safety_filter([_menu_item(c) for c in candidates], restrictions)
    kept_ids = {i.menu_id for i in safety.kept}
    safe_candidates = [c for c in candidates if c.menu_id in kept_ids]
    logger.info(
        "recommend: %d candidates -> %d after safety filter (excluded %d)",
        len(candidates), len(safe_candidates), safety.excluded_count,
    )

    # -- stage 2: Layer A calculator --
    p = request.profile
    target = compute_daily_target(
        sex=p.sex,
        weight_kg=p.weight_kg,
        height_cm=p.height_cm,
        age_years=p.age,
        activity_level=p.activity_level,
        goal=p.goal,
    )

    # -- stage 3: read precomputed clusters (NO training) --
    centroids = catalog.cluster_centroids()
    model_version, provisional = catalog.model_version()

    # -- stage 4: Layer C deterministic ranking --
    ranking = rank_candidates(
        [c.as_ranking_dict() for c in safe_candidates],
        target.as_dict(),
        centroids,
        top_n=request.max_results,
    )
    if ranking.null_cluster_count:
        logger.info(
            "recommend: %d ranked candidate(s) had no cluster_id — "
            "scored by item distance + fixed penalty (not dropped, not errored)",
            ranking.null_cluster_count,
        )

    recommendations = [
        Recommendation(
            menu_id=s.menu_id,
            name=s.name,
            match_score=s.match_score,
            nutrition=Nutrition(**s.nutrition),
        )
        for s in ranking.ranked
    ]

    catalog.log_prediction(
        {
            "age": p.age, "sex": p.sex, "weight_kg": p.weight_kg,
            "height_cm": p.height_cm, "activity_level": p.activity_level,
            "goal": p.goal, "allergies": list(request.restrictions.allergies),
            "diet_type": request.restrictions.diet_type,
            "target_calories": target.calories,
            "target_protein_g": target.protein_g,
            "target_carbs_g": target.carbs_g,
            "target_fat_g": target.fat_g,
            "recommended_menu_ids": [r.menu_id for r in recommendations],
            "excluded_count": safety.excluded_count,
            "model_version": model_version,
        }
    )

    return RecommendResponse(
        daily_target=DailyTargetModel(**target.as_dict()),
        recommendations=recommendations,
        excluded_count=safety.excluded_count,
        model_version=model_version,
    )
