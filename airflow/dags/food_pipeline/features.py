"""Layer B clustering features (the Airflow ``compute_nutrition_ratios`` step).

Computed per recipe from Spoonacular's own computed nutrition fields ONLY,
using the exact formulas in the ``food-rec-domain`` skill, section 5:

    pct_calories_from_protein = (protein_g * 4) / total_calories
    pct_calories_from_carbs   = (carbs_g   * 4) / total_calories
    pct_calories_from_fat     = (fat_g     * 9) / total_calories
    calories_per_serving      = total_calories        # already per serving

4 kcal/g for protein and carbs, 9 kcal/g for fat (standard Atwater factors).

These four numbers are the ENTIRE feature set that goes to K-Means in Phase 3.
No ``diet`` / ``intolerances`` / diet-boolean tag from Spoonacular may appear
in a feature row — using an externally-assigned label would defeat the point
of unsupervised discovery (CLAUDE.md). This module enforces that with a
whitelist check (:func:`assert_feature_row_clean`) and a test asserts it too.

Divide-by-zero / missing-value policy (per PLAN.md): a row with no usable
``calories`` (missing, non-finite, or <= 0) or any missing/negative macro is
**dropped**, with a logged reason. Values are never imputed.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)

PROTEIN_KCAL_PER_G = 4.0
CARB_KCAL_PER_G = 4.0
FAT_KCAL_PER_G = 9.0

# The four features fed to K-Means, in a fixed order.
FEATURE_COLUMNS: tuple[str, ...] = (
    "pct_calories_from_protein",
    "pct_calories_from_carbs",
    "pct_calories_from_fat",
    "calories_per_serving",
)

# A feature row may contain exactly these keys: the join key + the features.
_ALLOWED_KEYS = frozenset({"menu_id", *FEATURE_COLUMNS})

# Keys that must NEVER leak into a feature row (externally-assigned labels).
_BANNED_SUBSTRINGS = ("diet", "intoler", "vegan", "vegetarian", "gluten", "dairy", "tag")


@dataclass(frozen=True)
class DroppedRow:
    menu_id: str | None
    reason: str


def _finite_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def compute_feature_row(row: dict[str, Any]) -> dict[str, Any] | str:
    """Return a feature row for ``row``, or a string reason it was dropped.

    ``row`` is a Phase 1 staging row (``menu_id``, ``calories``, ``protein_g``,
    ``carbs_g``, ``fat_g``, ...).
    """
    calories = row.get("calories")
    if calories is None:
        return "calories is missing"
    if not _finite_number(calories):
        return f"calories is not a finite number ({calories!r})"
    if calories <= 0:
        return f"calories <= 0 ({calories}) — cannot divide"

    macros: dict[str, float] = {}
    for field in ("protein_g", "carbs_g", "fat_g"):
        value = row.get(field)
        if value is None:
            return f"{field} is missing"
        if not _finite_number(value):
            return f"{field} is not a finite number ({value!r})"
        if value < 0:
            return f"{field} is negative ({value})"
        macros[field] = float(value)

    total_calories = float(calories)
    feature_row = {
        "menu_id": row.get("menu_id"),
        "pct_calories_from_protein": macros["protein_g"] * PROTEIN_KCAL_PER_G / total_calories,
        "pct_calories_from_carbs": macros["carbs_g"] * CARB_KCAL_PER_G / total_calories,
        "pct_calories_from_fat": macros["fat_g"] * FAT_KCAL_PER_G / total_calories,
        "calories_per_serving": total_calories,
    }
    assert_feature_row_clean(feature_row)
    return feature_row


def assert_feature_row_clean(feature_row: dict[str, Any]) -> None:
    """Guard: a feature row carries only the join key and the four features —
    no diet/intolerance/label field from the source API."""
    keys = set(feature_row)
    extra = keys - _ALLOWED_KEYS
    if extra:
        raise ValueError(
            f"feature row has disallowed keys {sorted(extra)}; only "
            f"{sorted(_ALLOWED_KEYS)} are permitted (no source diet/allergy tags)"
        )
    missing = _ALLOWED_KEYS - keys
    if missing:
        raise ValueError(f"feature row is missing keys {sorted(missing)}")
    for key in keys:
        low = key.lower()
        if any(bad in low for bad in _BANNED_SUBSTRINGS):
            raise ValueError(f"feature row key {key!r} looks like a source label")


def build_feature_rows(
    rows: Iterable[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[DroppedRow]]:
    """Split staging rows into (feature_rows, dropped). Logs each drop."""
    features: list[dict[str, Any]] = []
    dropped: list[DroppedRow] = []
    total = 0
    for row in rows:
        total += 1
        result = compute_feature_row(row)
        if isinstance(result, str):
            d = DroppedRow(menu_id=row.get("menu_id"), reason=result)
            dropped.append(d)
            logger.warning(
                "features: dropped menu_id=%s reason=%s", d.menu_id, d.reason
            )
        else:
            features.append(result)
    logger.info(
        "features: %d computed, %d dropped (of %d)", len(features), len(dropped), total
    )
    return features, dropped


def build_features_from_staging_file(
    path: str | Path,
) -> tuple[list[dict[str, Any]], list[DroppedRow]]:
    """Load a Phase 1 staging file (``{"accepted": [...], ...}``) and compute
    features from its accepted rows."""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    accepted = payload.get("accepted", [])
    return build_feature_rows(accepted)


def feature_matrix(feature_rows: Iterable[dict[str, Any]]) -> list[list[float]]:
    """The numeric matrix K-Means consumes: one row per recipe, columns in
    ``FEATURE_COLUMNS`` order. ``menu_id`` is intentionally excluded."""
    matrix: list[list[float]] = []
    for fr in feature_rows:
        assert_feature_row_clean(fr)
        matrix.append([float(fr[col]) for col in FEATURE_COLUMNS])
    return matrix
