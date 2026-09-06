"""Pydantic request / response models for POST /recommend.

Response shape follows the CLAUDE.md contract exactly, plus a `disclaimer`
string on every response (CLAUDE.md: the not-medical-advice note must be
plain and prominent, not a footnote).
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field

Sex = Literal["male", "female"]
ActivityLevel = Literal["sedentary", "light", "moderate", "active", "very_active"]
Goal = Literal["lose", "maintain", "gain"]

MEDICAL_DISCLAIMER = (
    "This is not medical or clinical dietary advice. The daily targets come "
    "from the Mifflin-St Jeor equation and are a general wellness estimate, "
    "not a substitute for a doctor or registered dietitian, especially if "
    "you have a medical condition, take medication that interacts with diet, "
    "are pregnant, or are under 18. The safety filter matches ingredient text "
    "and known allergen tags only and can miss allergens hidden in compound "
    "ingredients or phrased unusually; anyone with a serious allergy must "
    "still check ingredients themselves and not rely on this as a sole "
    "safeguard. Cluster labels are exploratory groupings, not clinically "
    "validated categories."
)


class Profile(BaseModel):
    age: Annotated[int, Field(ge=1, le=120, description="years")]
    sex: Annotated[Sex, Field(description="Mifflin-St Jeor has two equation forms")]
    weight_kg: Annotated[float, Field(gt=0, le=500)]
    height_cm: Annotated[float, Field(gt=0, le=280)]
    activity_level: ActivityLevel
    goal: Goal


class Restrictions(BaseModel):
    allergies: Annotated[list[str], Field(default_factory=list)]
    diet_type: Annotated[str | None, Field(default=None)]


class RecommendRequest(BaseModel):
    profile: Profile
    restrictions: Annotated[Restrictions, Field(default_factory=Restrictions)]
    max_results: Annotated[int, Field(default=10, ge=1, le=50)]


class Nutrition(BaseModel):
    calories: float
    protein_g: float
    carbs_g: float
    fat_g: float


class DailyTargetModel(Nutrition):
    calories: int  # target calories are reported as a whole number


class Recommendation(BaseModel):
    menu_id: str
    name: str
    match_score: float
    nutrition: Nutrition


class RecommendResponse(BaseModel):
    daily_target: DailyTargetModel
    recommendations: list[Recommendation]
    excluded_count: int
    model_version: str
    disclaimer: str = MEDICAL_DISCLAIMER
