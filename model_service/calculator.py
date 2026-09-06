"""Layer A — daily energy + macro target (CLAUDE.md stage 2).

Pure deterministic business logic. Mifflin-St Jeor equation + an activity
multiplier + a goal adjustment. Constants are taken verbatim from the
`food-rec-domain` skill section 4 — not re-derived, not approximated.

    BMR (male)   = 10*weight_kg + 6.25*height_cm - 5*age + 5
    BMR (female) = 10*weight_kg + 6.25*height_cm - 5*age - 161
    TDEE = BMR * activity_multiplier

    goal "lose"     : TDEE - 500, floored at TDEE*0.80 (the skill caps the
                      deficit at ~20-25% of TDEE; we use the conservative 20%)
    goal "maintain" : TDEE
    goal "gain"     : TDEE + 400 (midpoint of the skill's +300..+500 range)

Macro split (skill: general default, protein 25-30% / carbs 40-50% /
fat 25-30% of calories — NOT a clinical prescription):
    protein 30%, carbs 40%, fat 30%   (4 / 4 / 9 kcal per gram)
"""

from __future__ import annotations

from dataclasses import dataclass

# --- constants, from the food-rec-domain skill section 4 -------------------
BMR_MALE_CONSTANT = 5.0
BMR_FEMALE_CONSTANT = -161.0

ACTIVITY_MULTIPLIERS: dict[str, float] = {
    "sedentary": 1.2,
    "light": 1.375,
    "moderate": 1.55,
    "active": 1.725,
    "very_active": 1.9,
}

GOAL_WEIGHT_LOSS_DELTA = -500.0
GOAL_WEIGHT_LOSS_FLOOR_FRACTION = 0.80  # deficit never exceeds 20% of TDEE
GOAL_WEIGHT_GAIN_DELTA = 400.0

# macro % of total calories + Atwater kcal/gram
MACRO_SPLIT = {"protein": 0.30, "carbs": 0.40, "fat": 0.30}
KCAL_PER_GRAM = {"protein": 4.0, "carbs": 4.0, "fat": 9.0}

VALID_SEXES = ("male", "female")
VALID_GOALS = ("lose", "maintain", "gain")


@dataclass(frozen=True)
class DailyTarget:
    calories: int
    protein_g: float
    carbs_g: float
    fat_g: float

    def as_dict(self) -> dict[str, float]:
        return {
            "calories": self.calories,
            "protein_g": self.protein_g,
            "carbs_g": self.carbs_g,
            "fat_g": self.fat_g,
        }


def mifflin_st_jeor_bmr(
    *, sex: str, weight_kg: float, height_cm: float, age_years: float
) -> float:
    if sex not in VALID_SEXES:
        raise ValueError(f"sex must be one of {VALID_SEXES}, got {sex!r}")
    base = 10.0 * weight_kg + 6.25 * height_cm - 5.0 * age_years
    return base + (BMR_MALE_CONSTANT if sex == "male" else BMR_FEMALE_CONSTANT)


def tdee(bmr: float, activity_level: str) -> float:
    try:
        return bmr * ACTIVITY_MULTIPLIERS[activity_level]
    except KeyError:
        raise ValueError(
            f"activity_level must be one of {tuple(ACTIVITY_MULTIPLIERS)}, "
            f"got {activity_level!r}"
        ) from None


def apply_goal(tdee_value: float, goal: str) -> float:
    if goal == "maintain":
        return tdee_value
    if goal == "gain":
        return tdee_value + GOAL_WEIGHT_GAIN_DELTA
    if goal == "lose":
        return max(
            tdee_value + GOAL_WEIGHT_LOSS_DELTA,
            tdee_value * GOAL_WEIGHT_LOSS_FLOOR_FRACTION,
        )
    raise ValueError(f"goal must be one of {VALID_GOALS}, got {goal!r}")


def macro_targets_g(calories: float) -> dict[str, float]:
    return {
        macro: calories * MACRO_SPLIT[macro] / KCAL_PER_GRAM[macro]
        for macro in ("protein", "carbs", "fat")
    }


def compute_daily_target(
    *,
    sex: str,
    weight_kg: float,
    height_cm: float,
    age_years: float,
    activity_level: str,
    goal: str,
) -> DailyTarget:
    """The full Layer A chain: BMR -> TDEE -> goal adjustment -> macros."""
    bmr = mifflin_st_jeor_bmr(
        sex=sex, weight_kg=weight_kg, height_cm=height_cm, age_years=age_years
    )
    target_calories = apply_goal(tdee(bmr, activity_level), goal)
    macros = macro_targets_g(target_calories)
    return DailyTarget(
        calories=round(target_calories),
        protein_g=round(macros["protein"], 1),
        carbs_g=round(macros["carbs"], 1),
        fat_g=round(macros["fat"], 1),
    )
