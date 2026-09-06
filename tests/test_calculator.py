"""Layer A — Mifflin-St Jeor daily target, checked against hand computation.

Constants (food-rec-domain skill s4): BMR male +5 / female -161; activity
1.2 / 1.375 / 1.55 / 1.725 / 1.9; lose = TDEE-500 floored at 0.80*TDEE;
gain = TDEE+400; macro split 30/40/30 at 4/4/9 kcal/g.
"""

import pytest

from model_service.calculator import (
    ACTIVITY_MULTIPLIERS,
    apply_goal,
    compute_daily_target,
    mifflin_st_jeor_bmr,
    tdee,
)


def _target(**kw):
    return compute_daily_target(**kw)


def test_male_moderate_maintain_hand_computed():
    # BMR = 10*80 + 6.25*180 - 5*30 + 5 = 800+1125-150+5 = 1780
    # TDEE = 1780 * 1.55 = 2759 ; maintain -> 2759
    t = _target(sex="male", weight_kg=80, height_cm=180, age_years=30,
                activity_level="moderate", goal="maintain")
    assert t.calories == 2759
    assert t.protein_g == pytest.approx(round(2759 * 0.30 / 4, 1))   # 206.9
    assert t.carbs_g == pytest.approx(round(2759 * 0.40 / 4, 1))     # 275.9
    assert t.fat_g == pytest.approx(round(2759 * 0.30 / 9, 1))       # 92.0
    assert (t.protein_g, t.carbs_g, t.fat_g) == (206.9, 275.9, 92.0)


def test_female_sedentary_lose_hand_computed():
    # BMR = 600 + 1031.25 - 125 - 161 = 1345.25
    # TDEE = 1345.25 * 1.2 = 1614.30 ; lose -> max(1114.30, 1291.44) = 1291.44
    t = _target(sex="female", weight_kg=60, height_cm=165, age_years=25,
                activity_level="sedentary", goal="lose")
    assert t.calories == 1291                       # round(1291.44)
    assert t.protein_g == pytest.approx(96.9)       # 1291.44*0.30/4
    assert t.carbs_g == pytest.approx(129.1)        # 1291.44*0.40/4
    assert t.fat_g == pytest.approx(43.0)           # 1291.44*0.30/9


def test_male_very_active_gain_hand_computed():
    # BMR = 950 + 1112.5 - 200 + 5 = 1867.5 ; TDEE = *1.9 = 3548.25
    # gain -> 3548.25 + 400 = 3948.25
    t = _target(sex="male", weight_kg=95, height_cm=178, age_years=40,
                activity_level="very_active", goal="gain")
    assert t.calories == 3948
    assert t.protein_g == pytest.approx(296.1)
    assert t.carbs_g == pytest.approx(394.8)
    assert t.fat_g == pytest.approx(131.6)


def test_deficit_is_capped_at_20_percent_of_tdee():
    # BMR = 500 + 1000 - 100 - 161 = 1239 ; TDEE = *1.2 = 1486.8
    # TDEE-500 = 986.8 but floor 0.80*TDEE = 1189.44 wins
    t = _target(sex="female", weight_kg=50, height_cm=160, age_years=20,
                activity_level="sedentary", goal="lose")
    assert t.calories == 1189
    # the applied deficit is exactly 20% of TDEE, not 500
    assert 1486.8 - 1189.44 == pytest.approx(1486.8 * 0.20)


@pytest.mark.parametrize(
    ("sex", "const"),
    [("male", 5.0), ("female", -161.0)],
)
def test_bmr_sex_constant(sex, const):
    bmr = mifflin_st_jeor_bmr(sex=sex, weight_kg=70, height_cm=170, age_years=35)
    assert bmr == pytest.approx(10 * 70 + 6.25 * 170 - 5 * 35 + const)


@pytest.mark.parametrize("level,mult", list(ACTIVITY_MULTIPLIERS.items()))
def test_every_activity_multiplier(level, mult):
    bmr = mifflin_st_jeor_bmr(sex="male", weight_kg=80, height_cm=180, age_years=30)
    assert bmr == 1780
    assert tdee(bmr, level) == pytest.approx(1780 * mult)


def test_goal_adjustments():
    assert apply_goal(2000, "maintain") == 2000
    assert apply_goal(2000, "gain") == 2400
    assert apply_goal(2000, "lose") == 1600           # 2000-500 > 0.8*2000
    assert apply_goal(2200, "lose") == pytest.approx(1760)  # 0.8*2200 wins vs 1700


def test_macro_split_sums_to_total_calories():
    t = _target(sex="male", weight_kg=75, height_cm=175, age_years=28,
                activity_level="active", goal="maintain")
    # 30/40/30 split -> reconstructed kcal ~= target (rounding aside)
    kcal = t.protein_g * 4 + t.carbs_g * 4 + t.fat_g * 9
    assert kcal == pytest.approx(t.calories, rel=0.01)


def test_rejects_bad_inputs():
    with pytest.raises(ValueError):
        mifflin_st_jeor_bmr(sex="other", weight_kg=70, height_cm=170, age_years=30)
    with pytest.raises(ValueError):
        tdee(1500, "extreme")
    with pytest.raises(ValueError):
        apply_goal(2000, "bulk")
