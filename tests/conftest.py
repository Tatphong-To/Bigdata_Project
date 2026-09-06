"""Make the ``food_pipeline`` package importable in tests.

The package lives under ``airflow/dags/`` because that is where Airflow puts
it on ``sys.path`` inside the image. Mirror that here.
"""

import json
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DAGS_DIR = _REPO_ROOT / "airflow" / "dags"
for _p in (_DAGS_DIR, _REPO_ROOT):  # food_pipeline.* and model_service.*
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

_FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture
def spoonacular_search_payload() -> dict:
    """A real (trimmed) ``complexSearch?addRecipeNutrition=true`` response,
    captured 2026-09-05 (recipe id 634476 "Bbq Chicken")."""
    return json.loads(
        (_FIXTURES / "spoonacular_complexsearch_sample.json").read_text("utf-8")
    )
