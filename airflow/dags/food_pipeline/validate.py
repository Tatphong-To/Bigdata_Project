"""Reject staging rows with missing or physically-impossible nutrition values.

This is the Airflow ``validate_nutrition_data`` step's logic (CLAUDE.md DAG
order). Every rejection is returned WITH A REASON and logged, so it is visible
why a recipe did not make it into the catalog.

It is deliberately conservative: a row only has to be *plausible*, not
correct. The goal is to catch gross errors (negative values, missing macros,
a per-recipe vs per-serving mix-up that makes macros ~4x the calories), not to
second-guess Spoonacular's numbers.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

_MACROS = ("protein_g", "carbs_g", "fat_g")
_REQUIRED_NUMERIC = ("calories", *_MACROS)

# Atwater factors (kcal per gram) — same constants as the clustering formula.
_KCAL_PER_G = {"protein_g": 4.0, "carbs_g": 4.0, "fat_g": 9.0}

# Plausibility bounds for a single serving.
_MAX_CALORIES_PER_SERVING = 5000.0
_MAX_GRAMS_PER_MACRO = 1500.0
# Allow macro-derived calories to sit a bit above stated calories (rounding,
# fibre, sugar alcohols, alcohol) before we treat it as an error.
_MACRO_CALORIE_TOLERANCE_RATIO = 1.25
_MACRO_CALORIE_TOLERANCE_ABS = 25.0


@dataclass(frozen=True)
class Rejection:
    menu_id: str | None
    name: str | None
    reasons: tuple[str, ...]


def validate_row(row: dict[str, Any]) -> list[str]:
    """Return a list of failure reasons for ``row``. Empty list == valid."""
    reasons: list[str] = []

    if not row.get("menu_id"):
        reasons.append("missing menu_id")
    name = row.get("name")
    if not isinstance(name, str) or not name.strip():
        reasons.append("missing name")

    for field in _REQUIRED_NUMERIC:
        value = row.get(field)
        if value is None:
            reasons.append(f"missing {field}")
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            reasons.append(f"{field} is not numeric ({value!r})")
            continue
        if math.isnan(value) or math.isinf(value):
            reasons.append(f"{field} is not finite ({value})")
            continue
        if value < 0:
            reasons.append(f"{field} is negative ({value})")

    servings = row.get("servings")
    if servings is not None:
        if isinstance(servings, bool) or not isinstance(servings, (int, float)):
            reasons.append(f"servings is not numeric ({servings!r})")
        elif servings <= 0:
            reasons.append(f"servings is not positive ({servings})")

    # Everything below needs the numeric fields to be present and sane.
    if reasons:
        return reasons

    calories = float(row["calories"])
    if calories == 0:
        reasons.append("calories is zero")
    if calories > _MAX_CALORIES_PER_SERVING:
        reasons.append(
            f"calories per serving implausibly high ({calories:.0f} > "
            f"{_MAX_CALORIES_PER_SERVING:.0f})"
        )
    for field in _MACROS:
        grams = float(row[field])
        if grams > _MAX_GRAMS_PER_MACRO:
            reasons.append(
                f"{field} implausibly high ({grams:.0f} g > "
                f"{_MAX_GRAMS_PER_MACRO:.0f} g)"
            )

    if calories > 0:
        macro_calories = sum(_KCAL_PER_G[f] * float(row[f]) for f in _MACROS)
        ceiling = calories * _MACRO_CALORIE_TOLERANCE_RATIO + _MACRO_CALORIE_TOLERANCE_ABS
        if macro_calories > ceiling:
            reasons.append(
                f"macro-derived calories ({macro_calories:.0f}) exceed stated "
                f"calories ({calories:.0f}) beyond tolerance — likely a "
                f"per-serving/per-recipe mismatch"
            )

    return reasons


def validate_batch(
    rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[Rejection]]:
    """Split ``rows`` into (accepted, rejected). Logs one line per rejection."""
    accepted: list[dict[str, Any]] = []
    rejected: list[Rejection] = []
    for row in rows:
        problems = validate_row(row)
        if problems:
            rej = Rejection(
                menu_id=row.get("menu_id"),
                name=row.get("name"),
                reasons=tuple(problems),
            )
            rejected.append(rej)
            logger.warning(
                "validate: rejected menu_id=%s name=%r reasons=%s",
                rej.menu_id,
                rej.name,
                "; ".join(rej.reasons),
            )
        else:
            accepted.append(row)
    logger.info(
        "validate: %d accepted, %d rejected (of %d)",
        len(accepted),
        len(rejected),
        len(rows),
    )
    return accepted, rejected
