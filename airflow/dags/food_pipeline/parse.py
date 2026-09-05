"""Turn a raw Spoonacular ``complexSearch`` recipe object into a staging row
for ``food_db.menu_catalog``.

Field mapping is VERIFIED against a real response (2026-09-05), see
docs/api-schemas.md:

  * top-level array is ``results`` (not ``recipes``);
  * per-serving nutrition is in ``recipe.nutrition.nutrients[]`` as
    ``{name, amount, unit, percentOfDailyNeeds}`` objects — matched by EXACT
    name. Note a separate ``"Net Carbohydrates"`` row exists, so matching
    ``"Carbohydrates"`` must be exact, not a prefix;
  * ingredient names are in ``recipe.nutrition.ingredients[].name`` and are
    present WITHOUT ``fillIngredients``;
  * diet/allergen tags: ``recipe.diets[]`` plus the recipe-level booleans.

The ``diet_tags`` we keep are for the rule-based SAFETY FILTER only. They must
never be fed to the Layer B clustering step (CLAUDE.md).
"""

from __future__ import annotations

from typing import Any

# exact nutrient names -> staging column
_MACRO_FIELDS: dict[str, str] = {
    "calories": "Calories",
    "protein_g": "Protein",
    "carbs_g": "Carbohydrates",
    "fat_g": "Fat",
}

_DIET_BOOLEANS = (
    "vegetarian",
    "vegan",
    "glutenFree",
    "dairyFree",
    "veryHealthy",
    "lowFodmap",
)


def _nutrient_amount(nutrients: list[dict[str, Any]], exact_name: str) -> float | None:
    for n in nutrients:
        if n.get("name") == exact_name:  # exact match on purpose
            try:
                return float(n["amount"])
            except (KeyError, TypeError, ValueError):
                return None
    return None


def parse_recipe(raw: dict[str, Any]) -> dict[str, Any]:
    """Map one raw recipe object to a staging row. Missing values become
    ``None`` here — rejecting them is the validator's job, not the parser's."""
    nutrition = raw.get("nutrition") or {}
    nutrients = nutrition.get("nutrients") or []

    row: dict[str, Any] = {
        "menu_id": str(raw["id"]) if raw.get("id") is not None else None,
        "source": "spoonacular",
        "name": raw.get("title"),
        "servings": raw.get("servings"),
    }
    for column, nutrient_name in _MACRO_FIELDS.items():
        row[column] = _nutrient_amount(nutrients, nutrient_name)

    ingredients = [
        str(i["name"]).strip().lower()
        for i in (nutrition.get("ingredients") or [])
        if i.get("name")
    ]
    row["ingredients"] = ingredients

    diet_tags: list[str] = []
    for tag in raw.get("diets") or []:
        if isinstance(tag, str) and tag.strip():
            diet_tags.append(tag.strip().lower())
    for key in _DIET_BOOLEANS:
        if raw.get(key) is True:
            normalized = _camel_to_words(key)
            if normalized not in diet_tags:
                diet_tags.append(normalized)
    row["diet_tags"] = diet_tags

    row["raw_payload"] = raw
    return row


def _camel_to_words(s: str) -> str:
    out: list[str] = []
    for ch in s:
        if ch.isupper():
            out.append(" ")
            out.append(ch.lower())
        else:
            out.append(ch)
    return "".join(out)


def parse_search_results(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Parse a full ``complexSearch`` page, de-duplicating by ``menu_id``
    (later pages/queries can repeat recipes). Rows with no ``menu_id`` are
    kept as-is for the validator to reject."""
    seen: set[str] = set()
    rows: list[dict[str, Any]] = []
    for raw in payload.get("results", []):
        row = parse_recipe(raw)
        mid = row["menu_id"]
        if mid is not None:
            if mid in seen:
                continue
            seen.add(mid)
        rows.append(row)
    return rows
