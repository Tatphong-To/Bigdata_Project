"""Estimate whole-recipe nutrition for a TheMealDB meal (Phase 2b).

For each ``IngredientLine``:
  1. ``unit_converter.convert_to_grams`` — free-text measure -> grams. A
     measure that cannot be converted skips the ingredient (recorded, logged);
  2. ``UsdaClient.search_foods`` — candidate foods for the ingredient name;
  3. ``ingredient_matcher.match_ingredient`` — best candidate + confidence.
     Below the configured threshold the ingredient is skipped (recorded,
     logged) — never guessed;
  4. contribution = ``grams / 100 * per-100g macro``.

Totals are summed over the ingredients that made it through all three steps.
``pct_calories_from_*`` reuse :func:`food_pipeline.features.compute_feature_row`
so the ratio formula is byte-for-byte the same as the Spoonacular path.

Every recipe produced here carries ``nutrition_source = 'usda_estimated'``
(vs. ``'spoonacular_computed'`` on the primary path) — see CLAUDE.md.

Serving basis: TheMealDB has no servings count. ``pct_calories_from_*`` are
scale-invariant so they are always produced. ``calories_per_serving`` needs a
servings number; if the caller does not supply one it is left ``None`` and the
row is marked ``complete = False`` (it cannot fully join Phase 3 clustering).
No servings count is invented.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable

from .features import compute_feature_row
from .ingredient_matcher import MatchConfig, match_ingredient
from .themealdb import IngredientLine, ParsedMeal
from .unit_converter import UnitConverterConfig, convert_to_grams
from .usda_client import UsdaFood

logger = logging.getLogger(__name__)

NUTRITION_SOURCE_USDA = "usda_estimated"
NUTRITION_SOURCE_SPOONACULAR = "spoonacular_computed"

SearchFn = Callable[[str], list[UsdaFood]]


@dataclass(frozen=True)
class IngredientContribution:
    slot: int
    name: str
    grams: float
    fdc_id: int
    matched_description: str
    confidence: float
    calories: float
    protein_g: float
    carbs_g: float
    fat_g: float


@dataclass(frozen=True)
class SkippedIngredient:
    slot: int
    name: str
    quantity_text: str
    stage: str  # "unit_conversion" | "usda_search" | "ingredient_match"
    reason: str


@dataclass(frozen=True)
class RecipeNutrition:
    meal_id: str
    name: str
    nutrition_source: str
    total_calories: float | None
    total_protein_g: float | None
    total_carbs_g: float | None
    total_fat_g: float | None
    servings: float | None
    calories_per_serving: float | None
    pct_calories_from_protein: float | None
    pct_calories_from_carbs: float | None
    pct_calories_from_fat: float | None
    used: tuple[IngredientContribution, ...] = ()
    skipped: tuple[SkippedIngredient, ...] = ()
    complete: bool = False
    notes: tuple[str, ...] = ()

    @property
    def n_used(self) -> int:
        return len(self.used)

    @property
    def n_skipped(self) -> int:
        return len(self.skipped)


def _contribution(
    line: IngredientLine, grams: float, food: UsdaFood, confidence: float
) -> IngredientContribution:
    factor = grams / 100.0
    return IngredientContribution(
        slot=line.slot,
        name=line.name,
        grams=grams,
        fdc_id=food.fdc_id,
        matched_description=food.description,
        confidence=confidence,
        calories=(food.calories_per_100g or 0.0) * factor,
        protein_g=(food.protein_g_per_100g or 0.0) * factor,
        carbs_g=(food.carbs_g_per_100g or 0.0) * factor,
        fat_g=(food.fat_g_per_100g or 0.0) * factor,
    )


def compute_recipe_nutrition(
    meal: ParsedMeal,
    search_fn: SearchFn,
    *,
    servings: float | None = None,
    match_config: MatchConfig | None = None,
    unit_config: UnitConverterConfig | None = None,
) -> RecipeNutrition:
    used: list[IngredientContribution] = []
    skipped: list[SkippedIngredient] = []

    for line in meal.ingredients:
        conv = convert_to_grams(
            line.quantity_text, ingredient_name=line.name, config=unit_config
        )
        if not conv.ok:
            skipped.append(SkippedIngredient(
                line.slot, line.name, line.quantity_text, "unit_conversion", conv.reason or "unconvertible",
            ))
            continue

        try:
            candidates = search_fn(line.name)
        except Exception as exc:  # a lookup failure skips one ingredient, not the recipe
            skipped.append(SkippedIngredient(
                line.slot, line.name, line.quantity_text, "usda_search", f"search failed: {exc}",
            ))
            logger.warning("compute_recipe_nutrition: USDA search failed for %r: %s", line.name, exc)
            continue

        if not candidates:
            skipped.append(SkippedIngredient(
                line.slot, line.name, line.quantity_text, "usda_search", "no USDA search results",
            ))
            continue

        m = match_ingredient(line.name, candidates, config=match_config)
        if not m.accepted or m.food is None:
            skipped.append(SkippedIngredient(
                line.slot, line.name, line.quantity_text, "ingredient_match",
                m.reason or "below confidence threshold",
            ))
            continue

        used.append(_contribution(line, conv.grams, m.food, m.confidence))

    notes: list[str] = []
    if not used:
        notes.append("no ingredients resolved to USDA nutrition")
        return RecipeNutrition(
            meal_id=meal.meal_id, name=meal.name, nutrition_source=NUTRITION_SOURCE_USDA,
            total_calories=None, total_protein_g=None, total_carbs_g=None, total_fat_g=None,
            servings=servings, calories_per_serving=None,
            pct_calories_from_protein=None, pct_calories_from_carbs=None,
            pct_calories_from_fat=None,
            used=tuple(used), skipped=tuple(skipped), complete=False, notes=tuple(notes),
        )

    total_cal = sum(c.calories for c in used)
    total_pro = sum(c.protein_g for c in used)
    total_carb = sum(c.carbs_g for c in used)
    total_fat = sum(c.fat_g for c in used)

    if skipped:
        notes.append(f"{len(skipped)} of {len(meal.ingredients)} ingredients skipped")

    # ratios: reuse features.compute_feature_row for identical formula.
    # basis is consistent (all per-recipe, or all per-serving) so pct_* are the
    # same either way; calories_per_serving needs a real servings number.
    if servings and servings > 0:
        basis = {
            "menu_id": meal.meal_id,
            "calories": total_cal / servings,
            "protein_g": total_pro / servings,
            "carbs_g": total_carb / servings,
            "fat_g": total_fat / servings,
        }
        cps = total_cal / servings
        complete = True
    else:
        basis = {
            "menu_id": meal.meal_id,
            "calories": total_cal,
            "protein_g": total_pro,
            "carbs_g": total_carb,
            "fat_g": total_fat,
        }
        cps = None
        complete = False
        notes.append("no servings count — calories_per_serving unavailable, row incomplete for clustering")

    feats = compute_feature_row(basis)
    if isinstance(feats, str):  # e.g. total calories summed to 0
        notes.append(f"ratio computation dropped: {feats}")
        return RecipeNutrition(
            meal_id=meal.meal_id, name=meal.name, nutrition_source=NUTRITION_SOURCE_USDA,
            total_calories=total_cal, total_protein_g=total_pro,
            total_carbs_g=total_carb, total_fat_g=total_fat,
            servings=servings, calories_per_serving=cps,
            pct_calories_from_protein=None, pct_calories_from_carbs=None,
            pct_calories_from_fat=None,
            used=tuple(used), skipped=tuple(skipped), complete=False, notes=tuple(notes),
        )

    return RecipeNutrition(
        meal_id=meal.meal_id,
        name=meal.name,
        nutrition_source=NUTRITION_SOURCE_USDA,
        total_calories=total_cal,
        total_protein_g=total_pro,
        total_carbs_g=total_carb,
        total_fat_g=total_fat,
        servings=servings,
        calories_per_serving=cps,
        pct_calories_from_protein=feats["pct_calories_from_protein"],
        pct_calories_from_carbs=feats["pct_calories_from_carbs"],
        pct_calories_from_fat=feats["pct_calories_from_fat"],
        used=tuple(used),
        skipped=tuple(skipped),
        complete=complete,
        notes=tuple(notes),
    )
