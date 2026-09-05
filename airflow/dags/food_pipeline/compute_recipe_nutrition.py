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
so the ratio formula is byte-for-byte the same as the Spoonacular path
(unchanged — CLAUDE.md).

Every recipe produced here carries ``nutrition_source = 'usda_estimated'``
(vs. ``'spoonacular_computed'`` on the primary path) — see CLAUDE.md.

Recipe-level completeness guard
------------------------------
Skipped ingredients bias the ratios *systematically*, not as noise: the
percentage base excludes the macros of whatever was dropped. If a recipe's
main energy/protein source is skipped (e.g. Teriyaki Chicken Casserole where
``chicken [2]`` and ``vegetables [1 (12 oz.)]`` can't be quantified), the
remaining rice+sauce still sum to ~100% and *look* fine while being wrong.

So each recipe gets a **completeness** score and a configurable threshold
(:class:`CompletenessConfig.min_completeness`, same pattern as
``MatchConfig.min_confidence`` — never hard-coded at a call site). Below the
threshold the row is **dropped**: the four clustering features
(``pct_calories_from_*`` and ``calories_per_serving``) are set to ``None`` and
``complete = False``. Nothing partial is emitted for clustering — "dropped,
not guessed" (CLAUDE.md).

Serving basis: TheMealDB has no servings count. ``pct_calories_from_*`` are
scale-invariant so they are computed regardless; ``calories_per_serving``
needs a servings number and is ``None`` (``complete = False``) without one.
No servings count is invented.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import Callable, Iterable

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
class CompletenessConfig:
    """Recipe-level completeness gate. ``min_completeness`` is the only
    acceptance threshold and is passed in — never hard-coded at a call site
    (same rule as ``MatchConfig.min_confidence``).

    NOTE: ``min_completeness`` default here is PROVISIONAL — confirm the real
    value against live skip-rate numbers before relying on it.
    """

    min_completeness: float = 0.70  # PROVISIONAL — see note above

    # How to combine the two sub-metrics into the gate value:
    #   "count"   -> matched_ingredient_count / total_ingredient_count
    #   "calorie" -> matched_calories / (matched + estimated missing)
    #   "min"     -> min(count, calorie)  [default: strict on both axes]
    basis: str = "min"

    # GUARD-ONLY heuristics for weighting a skipped ingredient by energy.
    # These never become nutrition — they only estimate how much a dropped
    # ingredient *would* have contributed, to decide whether to keep the row.
    #   * a skipped ingredient that DID convert to grams and had USDA
    #     candidates is weighted by grams x best-candidate kcal/100 g;
    #   * one that converted but had no candidates -> grams x fallback kcal/g;
    #   * one that never converted -> fallback grams x fallback kcal/g;
    #   * a non-quantitative measure ("pinch", "to taste") -> treated as 0.
    guard_fallback_kcal_per_g: float = 2.0
    guard_fallback_grams_per_unquantified: float = 100.0


@dataclass(frozen=True)
class Completeness:
    count_fraction: float
    calorie_fraction: float
    value: float  # the gate value, per CompletenessConfig.basis
    basis: str
    estimated_missing_calories: float


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
    grams: float | None = None  # set if the measure did convert
    est_missing_calories: float = 0.0  # guard-only energy estimate


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
    # completeness
    total_ingredient_count: int = 0
    matched_ingredient_count: int = 0
    count_completeness: float = 0.0
    calorie_completeness: float | None = None
    completeness: float = 0.0
    completeness_basis: str = "min"
    dropped_for_completeness: bool = False

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


def _best_candidate_kcal_per_100g(candidates: list[UsdaFood]) -> float | None:
    for c in candidates:
        if c.calories_per_100g is not None:
            return c.calories_per_100g
    return None


def _estimate_missing_calories(
    *, grams: float | None, reason: str, candidates: list[UsdaFood], cfg: CompletenessConfig
) -> float:
    if grams is None:
        if "non-quantitative" in reason:
            return 0.0  # a pinch / to taste — negligible by design
        return cfg.guard_fallback_grams_per_unquantified * cfg.guard_fallback_kcal_per_g
    kcal_per_100g = _best_candidate_kcal_per_100g(candidates)
    if kcal_per_100g is not None:
        return grams / 100.0 * kcal_per_100g
    return grams * cfg.guard_fallback_kcal_per_g


def assess_completeness(
    *,
    matched_calories: float,
    skipped: list[SkippedIngredient],
    total_ingredient_count: int,
    cfg: CompletenessConfig,
) -> Completeness:
    matched_count = total_ingredient_count - len(skipped)
    count_fraction = (
        matched_count / total_ingredient_count if total_ingredient_count else 0.0
    )
    missing_kcal = sum(s.est_missing_calories for s in skipped)
    denom = matched_calories + missing_kcal
    calorie_fraction = (matched_calories / denom) if denom > 0 else 0.0

    if cfg.basis == "calorie":
        value = calorie_fraction
    elif cfg.basis == "count":
        value = count_fraction
    else:  # "min"
        value = min(count_fraction, calorie_fraction)

    return Completeness(
        count_fraction=count_fraction,
        calorie_fraction=calorie_fraction,
        value=value,
        basis=cfg.basis,
        estimated_missing_calories=missing_kcal,
    )


def compute_recipe_nutrition(
    meal: ParsedMeal,
    search_fn: SearchFn,
    *,
    servings: float | None = None,
    match_config: MatchConfig | None = None,
    unit_config: UnitConverterConfig | None = None,
    completeness_config: CompletenessConfig | None = None,
) -> RecipeNutrition:
    comp_cfg = completeness_config or CompletenessConfig()
    used: list[IngredientContribution] = []
    skipped: list[SkippedIngredient] = []
    total_count = len(meal.ingredients)

    for line in meal.ingredients:
        conv = convert_to_grams(
            line.quantity_text, ingredient_name=line.name, config=unit_config
        )
        if not conv.ok:
            reason = conv.reason or "unconvertible"
            skipped.append(SkippedIngredient(
                line.slot, line.name, line.quantity_text, "unit_conversion", reason,
                grams=None,
                est_missing_calories=_estimate_missing_calories(
                    grams=None, reason=reason, candidates=[], cfg=comp_cfg
                ),
            ))
            continue

        try:
            candidates = search_fn(line.name)
        except Exception as exc:  # a lookup failure skips one ingredient, not the recipe
            skipped.append(SkippedIngredient(
                line.slot, line.name, line.quantity_text, "usda_search",
                f"search failed: {exc}", grams=conv.grams,
                est_missing_calories=_estimate_missing_calories(
                    grams=conv.grams, reason="search failed", candidates=[], cfg=comp_cfg
                ),
            ))
            logger.warning("compute_recipe_nutrition: USDA search failed for %r: %s", line.name, exc)
            continue

        if not candidates:
            skipped.append(SkippedIngredient(
                line.slot, line.name, line.quantity_text, "usda_search",
                "no USDA search results", grams=conv.grams,
                est_missing_calories=_estimate_missing_calories(
                    grams=conv.grams, reason="no results", candidates=[], cfg=comp_cfg
                ),
            ))
            continue

        m = match_ingredient(line.name, candidates, config=match_config)
        if not m.accepted or m.food is None:
            skipped.append(SkippedIngredient(
                line.slot, line.name, line.quantity_text, "ingredient_match",
                m.reason or "below confidence threshold", grams=conv.grams,
                est_missing_calories=_estimate_missing_calories(
                    grams=conv.grams, reason=m.reason or "", candidates=candidates,
                    cfg=comp_cfg,
                ),
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
            total_ingredient_count=total_count, matched_ingredient_count=0,
            count_completeness=0.0, calorie_completeness=0.0, completeness=0.0,
            completeness_basis=comp_cfg.basis,
        )

    total_cal = sum(c.calories for c in used)
    total_pro = sum(c.protein_g for c in used)
    total_carb = sum(c.carbs_g for c in used)
    total_fat = sum(c.fat_g for c in used)

    if skipped:
        notes.append(f"{len(skipped)} of {total_count} ingredients skipped")

    comp = assess_completeness(
        matched_calories=total_cal,
        skipped=skipped,
        total_ingredient_count=total_count,
        cfg=comp_cfg,
    )

    # ratios: reuse features.compute_feature_row for the identical formula.
    if servings and servings > 0:
        basis = {
            "menu_id": meal.meal_id,
            "calories": total_cal / servings,
            "protein_g": total_pro / servings,
            "carbs_g": total_carb / servings,
            "fat_g": total_fat / servings,
        }
        cps: float | None = total_cal / servings
    else:
        basis = {
            "menu_id": meal.meal_id,
            "calories": total_cal,
            "protein_g": total_pro,
            "carbs_g": total_carb,
            "fat_g": total_fat,
        }
        cps = None
        notes.append("no servings count — calories_per_serving unavailable")

    feats = compute_feature_row(basis)
    if isinstance(feats, str):  # e.g. total calories summed to 0
        notes.append(f"ratio computation dropped: {feats}")
        pct_p = pct_c = pct_f = None
        complete = False
    else:
        pct_p = feats["pct_calories_from_protein"]
        pct_c = feats["pct_calories_from_carbs"]
        pct_f = feats["pct_calories_from_fat"]
        complete = cps is not None  # needs servings to fully join clustering

    result = RecipeNutrition(
        meal_id=meal.meal_id,
        name=meal.name,
        nutrition_source=NUTRITION_SOURCE_USDA,
        total_calories=total_cal,
        total_protein_g=total_pro,
        total_carbs_g=total_carb,
        total_fat_g=total_fat,
        servings=servings,
        calories_per_serving=cps,
        pct_calories_from_protein=pct_p,
        pct_calories_from_carbs=pct_c,
        pct_calories_from_fat=pct_f,
        used=tuple(used),
        skipped=tuple(skipped),
        complete=complete,
        notes=tuple(notes),
        total_ingredient_count=total_count,
        matched_ingredient_count=len(used),
        count_completeness=comp.count_fraction,
        calorie_completeness=comp.calorie_fraction,
        completeness=comp.value,
        completeness_basis=comp.basis,
    )

    if comp.value < comp_cfg.min_completeness:
        return _drop_for_completeness(result, comp, comp_cfg)
    return result


def _drop_for_completeness(
    result: RecipeNutrition, comp: Completeness, cfg: CompletenessConfig
) -> RecipeNutrition:
    rejected = [
        f"{s.name!r} [{s.quantity_text}] ({s.stage}: {s.reason})"
        for s in result.skipped
    ]
    logger.warning(
        "compute_recipe_nutrition: DROPPED recipe id=%s name=%r for low "
        "completeness %.2f (%s basis: count=%.2f, calorie=%.2f) < threshold "
        "%.2f — %d/%d ingredients matched; rejected: %s",
        result.meal_id, result.name, comp.value, comp.basis,
        comp.count_fraction, comp.calorie_fraction, cfg.min_completeness,
        result.matched_ingredient_count, result.total_ingredient_count,
        "; ".join(rejected) or "(none)",
    )
    return replace(
        result,
        pct_calories_from_protein=None,
        pct_calories_from_carbs=None,
        pct_calories_from_fat=None,
        calories_per_serving=None,
        complete=False,
        dropped_for_completeness=True,
        notes=result.notes
        + (
            f"DROPPED for low completeness {comp.value:.2f} < "
            f"{cfg.min_completeness:.2f} ({comp.basis}: count "
            f"{comp.count_fraction:.2f}, calorie {comp.calorie_fraction:.2f}) — "
            f"clustering features withheld",
        ),
    )


# --------------------------------------------------------------------------
# batch summary
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class CompletenessSummary:
    total_recipes: int
    passed: int
    dropped_for_completeness: int
    dropped_no_ingredients: int
    incomplete_no_servings: int
    total_ingredients: int
    matched_ingredients: int
    skipped_by_stage: dict[str, int]

    @property
    def recipe_drop_rate(self) -> float:
        return (
            self.dropped_for_completeness / self.total_recipes
            if self.total_recipes
            else 0.0
        )

    @property
    def ingredient_skip_rate(self) -> float:
        return (
            1.0 - self.matched_ingredients / self.total_ingredients
            if self.total_ingredients
            else 0.0
        )


def summarize_completeness(results: Iterable[RecipeNutrition]) -> CompletenessSummary:
    results = list(results)
    by_stage: dict[str, int] = {}
    total_ing = matched_ing = 0
    passed = dropped_comp = dropped_none = incomplete_serv = 0
    for r in results:
        total_ing += r.total_ingredient_count
        matched_ing += r.matched_ingredient_count
        for s in r.skipped:
            by_stage[s.stage] = by_stage.get(s.stage, 0) + 1
        if r.dropped_for_completeness:
            dropped_comp += 1
        elif r.total_calories is None:
            dropped_none += 1
        elif not r.complete:
            incomplete_serv += 1
            passed += 1  # ratios still valid, just missing calories_per_serving
        else:
            passed += 1
    return CompletenessSummary(
        total_recipes=len(results),
        passed=passed,
        dropped_for_completeness=dropped_comp,
        dropped_no_ingredients=dropped_none,
        incomplete_no_servings=incomplete_serv,
        total_ingredients=total_ing,
        matched_ingredients=matched_ing,
        skipped_by_stage=by_stage,
    )


def log_completeness_summary(results: Iterable[RecipeNutrition]) -> CompletenessSummary:
    s = summarize_completeness(results)
    logger.info(
        "compute_recipe_nutrition batch: %d recipes — %d passed, %d dropped for "
        "completeness (%.0f%%), %d with no usable ingredients, %d ratio-ok but "
        "missing servings. Ingredients: %d/%d matched (%.0f%% skipped); "
        "skips by stage: %s",
        s.total_recipes, s.passed, s.dropped_for_completeness,
        s.recipe_drop_rate * 100, s.dropped_no_ingredients,
        s.incomplete_no_servings, s.matched_ingredients, s.total_ingredients,
        s.ingredient_skip_rate * 100, s.skipped_by_stage or "{}",
    )
    return s
