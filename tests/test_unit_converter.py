"""unit_converter: supported units convert exactly / by density; everything
else is rejected with a reason, never estimated."""

import pytest

from food_pipeline.unit_converter import (
    SUPPORTED_MASS_UNITS,
    SUPPORTED_VOLUME_UNITS,
    UnitConverterConfig,
    convert_to_grams,
)


# --- mass: exact ---------------------------------------------------------
@pytest.mark.parametrize(
    ("text", "grams"),
    [
        ("200 g", 200.0),
        ("200g", 200.0),
        ("1 kg", 1000.0),
        ("1.5 kg", 1500.0),
        ("2 oz", 56.69904625),
        ("1 lb", 453.59237),
        ("500 mg", 0.5),
        ("100 grams", 100.0),
        ("2 ounces", 56.69904625),
    ],
)
def test_mass_units_exact(text, grams):
    r = convert_to_grams(text)
    assert r.ok
    assert r.kind == "mass"
    assert r.grams == pytest.approx(grams)


# --- volume: needs density (default water-equivalent) -------------------
def test_volume_uses_default_g_per_ml():
    r = convert_to_grams("1 cup")
    assert r.ok and r.kind == "volume"
    assert r.grams == pytest.approx(236.5882365)  # 1.0 g/ml


def test_volume_scales_with_configured_density():
    cfg = UnitConverterConfig(g_per_ml=0.92)
    r = convert_to_grams("1 cup", config=cfg)
    assert r.grams == pytest.approx(236.5882365 * 0.92)


def test_density_override_by_ingredient_name():
    cfg = UnitConverterConfig(density_overrides={"oil": 0.92})
    r = convert_to_grams("1 cup", ingredient_name="olive oil", config=cfg)
    assert r.grams == pytest.approx(236.5882365 * 0.92)
    # non-matching name falls back to default density
    r2 = convert_to_grams("1 cup", ingredient_name="whole milk", config=cfg)
    assert r2.grams == pytest.approx(236.5882365)


@pytest.mark.parametrize(
    ("text", "grams"),
    [
        ("1 tbsp", 14.78676478125),
        ("2 tsp", 9.8578431875),
        ("1 tablespoon", 14.78676478125),
        ("1.5 l", 1500.0),
        ("250 ml", 250.0),
        ("1 fl oz", 29.5735295625),
    ],
)
def test_volume_units(text, grams):
    r = convert_to_grams(text)
    assert r.ok and r.grams == pytest.approx(grams)


def test_allow_volume_false_rejects_volume():
    cfg = UnitConverterConfig(allow_volume=False)
    r = convert_to_grams("1 cup", config=cfg)
    assert not r.ok
    assert "allow_volume" in r.reason


# --- fractions ---------------------------------------------------------
@pytest.mark.parametrize(
    ("text", "grams"),
    [
        ("1/2 cup", 236.5882365 / 2),
        ("1 1/2 cups", 236.5882365 * 1.5),
        ("½ cup", 236.5882365 / 2),      # ½
        ("1½ cups", 236.5882365 * 1.5),  # 1½
        (".5 cup", 236.5882365 / 2),
        ("3/4 cup", 236.5882365 * 0.75),
    ],
)
def test_fraction_quantities(text, grams):
    r = convert_to_grams(text)
    assert r.ok and r.grams == pytest.approx(grams)


# --- rejections: never estimated ------------------------------------
@pytest.mark.parametrize(
    ("text", "needle"),
    [
        ("", "empty"),
        ("   ", "empty"),
        ("to taste", "non-quantitative"),
        ("a pinch", "non-quantitative"),
        ("1 pinch", "non-quantitative"),
        ("dash", "non-quantitative"),
        ("handful", "non-quantitative"),
        ("as needed", "non-quantitative"),
        ("2", "count-based"),
        ("3 ", "count-based"),
        ("1 onion", "unsupported unit"),
        ("1 can", "unsupported unit"),
        ("2 cloves", "unsupported unit"),
        ("1 sprig", "unsupported unit"),
        ("3 stalks", "unsupported unit"),
        ("some rice", "non-quantitative"),
        ("chopped", "could not parse"),
    ],
)
def test_rejections(text, needle):
    r = convert_to_grams(text)
    assert not r.ok
    assert r.grams is None
    assert needle in r.reason


def test_zero_or_negative_quantity_rejected():
    assert not convert_to_grams("0 g").ok
    assert not convert_to_grams("0 cup").ok


def test_supported_unit_lists_are_explicit():
    assert set(SUPPORTED_MASS_UNITS) == {"g", "kg", "mg", "oz", "lb"}
    assert "cup" in SUPPORTED_VOLUME_UNITS and "tbsp" in SUPPORTED_VOLUME_UNITS


def test_trailing_words_after_unit_are_ignored():
    r = convert_to_grams("1 cup chopped")
    assert r.ok and r.unit == "cup"
