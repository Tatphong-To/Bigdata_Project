"""Convert a free-text measure ("1 cup", "200g", "2 tbsp") to grams (Phase 2b).

Explicit about what it supports. A measure it cannot convert is **rejected**
(``ConversionResult.grams is None`` with a ``reason``) — never estimated:

  * non-quantitative measures ("to taste", "a pinch", "handful", "") -> reject
  * count-based measures with no mass/volume unit ("2", "1 onion", "3 eggs")
    -> reject (no per-item weight table in scope)
  * unknown units ("1 can", "1 sprig") -> reject

Mass units convert exactly. **Volume units need a density**: the only
assumption this module makes is a configurable ``g_per_ml`` (default ``1.0``,
i.e. water-equivalent). That is a documented modelling choice, not a silent
guess — callers can pass a real value, or per-ingredient ``density_overrides``
(substring -> g/ml), or set ``allow_volume=False`` to reject every volume
measure instead. Whatever the setting, a recipe built from these numbers is
tagged ``nutrition_source = 'usda_estimated'`` downstream.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Mapping

# --- supported units -------------------------------------------------------
# grams per 1 unit, for MASS units (exact).
_MASS_G: dict[str, float] = {
    "g": 1.0,
    "kg": 1000.0,
    "mg": 0.001,
    "oz": 28.349523125,
    "lb": 453.59237,
}
# millilitres per 1 unit, for VOLUME units. Converted to grams via g_per_ml.
_VOLUME_ML: dict[str, float] = {
    "ml": 1.0,
    "l": 1000.0,
    "tsp": 4.92892159375,
    "tbsp": 14.78676478125,
    "cup": 236.5882365,
    "floz": 29.5735295625,
    "pint": 473.176473,
    "quart": 946.352946,
    "gallon": 3785.411784,
}

SUPPORTED_MASS_UNITS: tuple[str, ...] = tuple(_MASS_G)
SUPPORTED_VOLUME_UNITS: tuple[str, ...] = tuple(_VOLUME_ML)

# alias -> canonical key
_UNIT_ALIASES: dict[str, str] = {
    "gram": "g", "grams": "g", "gr": "g", "gm": "g", "grammes": "g", "gramme": "g",
    "kilogram": "kg", "kilograms": "kg", "kilo": "kg", "kilos": "kg", "kgs": "kg",
    "milligram": "mg", "milligrams": "mg",
    "ounce": "oz", "ounces": "oz", "ozs": "oz",
    "pound": "lb", "pounds": "lb", "lbs": "lb",
    "milliliter": "ml", "millilitre": "ml", "milliliters": "ml", "millilitres": "ml",
    "cc": "ml", "mls": "ml",
    "liter": "l", "litre": "l", "liters": "l", "litres": "l", "ltr": "l",
    "teaspoon": "tsp", "teaspoons": "tsp", "tsps": "tsp", "tspn": "tsp",
    "tablespoon": "tbsp", "tablespoons": "tbsp", "tbsps": "tbsp", "tbs": "tbsp",
    "tbl": "tbsp", "tblsp": "tbsp",
    "cups": "cup",
    "fl oz": "floz", "fluid ounce": "floz", "fluid ounces": "floz",
    "fl. oz": "floz", "fl.oz": "floz",
    "pints": "pint", "pt": "pint",
    "quarts": "quart", "qt": "quart",
    "gallons": "gallon", "gal": "gallon",
}

# unicode fraction -> value
_UNICODE_FRACTIONS: dict[str, str] = {
    "¼": "1/4", "½": "1/2", "¾": "3/4",
    "⅐": "1/7", "⅑": "1/9", "⅒": "1/10",
    "⅓": "1/3", "⅔": "2/3",
    "⅕": "1/5", "⅖": "2/5", "⅗": "3/5", "⅘": "4/5",
    "⅙": "1/6", "⅚": "5/6",
    "⅛": "1/8", "⅜": "3/8", "⅝": "5/8", "⅞": "7/8",
}

# measures that are explicitly non-quantitative
_NON_QUANTITATIVE = re.compile(
    r"\b(to taste|as needed|as required|pinch|dash|handful|drizzle|splash|"
    r"sprinkle|garnish|for serving|for frying|to serve|some|few|q\.?s\.?)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ConversionResult:
    grams: float | None
    reason: str | None = None  # set iff grams is None
    quantity: float | None = None
    unit: str | None = None  # canonical unit key
    kind: str | None = None  # "mass" | "volume"

    @property
    def ok(self) -> bool:
        return self.grams is not None


@dataclass(frozen=True)
class UnitConverterConfig:
    g_per_ml: float = 1.0
    allow_volume: bool = True
    # ingredient-name substring (lower) -> density g/ml, applied when the
    # ingredient name contains the substring. Optional precision.
    density_overrides: Mapping[str, float] = field(default_factory=dict)


def _to_ascii_fractions(text: str) -> str:
    out = []
    for ch in text:
        if ch in _UNICODE_FRACTIONS:
            # "1½" -> "1 1/2"; "½" -> " 1/2"
            if out and out[-1].isdigit():
                out.append(" ")
            out.append(_UNICODE_FRACTIONS[ch])
        else:
            out.append(ch)
    return "".join(out)


_QTY_RE = re.compile(
    r"""^\s*
        (?P<qty>
            \d+\s+\d+\s*/\s*\d+      # mixed  "1 1/2"
          | \d+\s*/\s*\d+            # fraction "3/4"
          | \d+(?:\.\d+)?            # int / decimal "2" "1.5"
          | \.\d+                    # ".5"
        )
        \s*
        (?P<unit>[a-zA-Z][a-zA-Z. ]*?)?      # optional unit words
        \s*$
    """,
    re.VERBOSE,
)


def _parse_quantity(raw: str) -> float | None:
    raw = raw.strip()
    try:
        if " " in raw and "/" in raw:  # mixed number
            whole, frac = raw.split(None, 1)
            num, den = frac.split("/")
            return int(whole) + int(num) / int(den)
        if "/" in raw:
            num, den = raw.split("/")
            return int(num) / int(den)
        return float(raw)
    except (ValueError, ZeroDivisionError):
        return None


def _canonical_unit(unit_text: str) -> str | None:
    u = unicodedata.normalize("NFKD", unit_text).strip().lower()
    u = u.strip(". ").replace(".", "")
    u = re.sub(r"\s+", " ", u)
    if not u:
        return None
    for candidate in (u, u.rstrip("s"), u.split(" ")[0], u.split(" ")[0].rstrip("s")):
        if candidate in _MASS_G or candidate in _VOLUME_ML:
            return candidate
        if candidate in _UNIT_ALIASES:
            return _UNIT_ALIASES[candidate]
    # two-word aliases like "fl oz"
    if u in _UNIT_ALIASES:
        return _UNIT_ALIASES[u]
    return None


def convert_to_grams(
    quantity_text: str,
    *,
    ingredient_name: str = "",
    config: UnitConverterConfig | None = None,
) -> ConversionResult:
    cfg = config or UnitConverterConfig()
    text = (quantity_text or "").strip()
    if not text:
        return ConversionResult(None, reason="empty measure")
    if _NON_QUANTITATIVE.search(text):
        return ConversionResult(None, reason=f"non-quantitative measure: {text!r}")

    text = _to_ascii_fractions(text)
    m = _QTY_RE.match(text)
    if not m:
        return ConversionResult(None, reason=f"could not parse quantity/unit from {quantity_text!r}")

    qty = _parse_quantity(m.group("qty"))
    if qty is None:
        return ConversionResult(None, reason=f"bad numeric quantity in {quantity_text!r}")
    if qty <= 0:
        return ConversionResult(None, reason=f"non-positive quantity ({qty}) in {quantity_text!r}")

    unit_text = (m.group("unit") or "").strip()
    if not unit_text:
        return ConversionResult(
            None, reason=f"count-based measure with no mass/volume unit: {quantity_text!r}",
            quantity=qty,
        )

    unit = _canonical_unit(unit_text)
    if unit is None:
        return ConversionResult(None, reason=f"unsupported unit {unit_text!r}", quantity=qty)

    if unit in _MASS_G:
        return ConversionResult(
            grams=qty * _MASS_G[unit], quantity=qty, unit=unit, kind="mass"
        )

    # volume
    if not cfg.allow_volume:
        return ConversionResult(
            None, reason=f"volume unit {unit!r} rejected (allow_volume=False)",
            quantity=qty, unit=unit, kind="volume",
        )
    density = cfg.g_per_ml
    name_l = (ingredient_name or "").lower()
    for substr, dens in cfg.density_overrides.items():
        if substr in name_l:
            density = dens
            break
    return ConversionResult(
        grams=qty * _VOLUME_ML[unit] * density,
        quantity=qty,
        unit=unit,
        kind="volume",
    )
