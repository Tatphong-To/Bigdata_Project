"""FastAPI app — the Model Service (Phase 5).

Serves POST /recommend. Every request runs the fixed pipeline in
`model_service.pipeline` (safety filter -> Layer A -> read cluster_id ->
Layer C ranking) and is logged to `prediction_log`.

Run:  fastapi dev model_service/main.py      (dev, reload)
      fastapi run model_service/main.py       (prod)
Needs FOOD_DB_DSN or AIRFLOW_CONN_FOOD_DB in the environment.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import Depends, FastAPI

from . import catalog as catalog_module
from .pipeline import recommend as run_recommend
from .schemas import MEDICAL_DISCLAIMER, RecommendRequest, RecommendResponse

logging.basicConfig(level=logging.INFO)

app = FastAPI(
    title="Food & Nutrition Recommendation — Model Service",
    version="1.0.0",
    description=(
        "Personalised meal recommendations. Each request runs, in this fixed "
        "order: (1) a rule-based safety filter for allergies / diet, "
        "(2) a Mifflin-St Jeor daily energy + macro target, (3) a read of the "
        "precomputed K-Means cluster_id from the catalog, (4) deterministic "
        "distance ranking. The service never trains a model.\n\n"
        "**This is not medical or clinical dietary advice.** " + MEDICAL_DISCLAIMER
    ),
)


class DbCatalog:
    """CatalogPort backed by Postgres. One instance per request; candidates
    are read once and reused across the pipeline stages."""

    def __init__(self) -> None:
        self._candidates = None

    def candidates(self):
        if self._candidates is None:
            self._candidates = catalog_module.fetch_candidates()
        return self._candidates

    def cluster_centroids(self):
        return catalog_module.fetch_cluster_centroids()

    def model_version(self):
        return catalog_module.fetch_model_version()

    def log_prediction(self, record: dict) -> None:
        try:
            catalog_module.write_prediction_log(record)
        except Exception:  # logging a request must not fail the response
            logging.getLogger("model_service").exception("prediction_log write failed")


def get_catalog() -> DbCatalog:
    return DbCatalog()


CatalogDep = Annotated[DbCatalog, Depends(get_catalog)]


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/recommend")
def recommend(request: RecommendRequest, catalog: CatalogDep) -> RecommendResponse:
    """Run the fixed safety-filter -> Layer A -> cluster-read -> Layer C
    pipeline. `excluded_count` is always present (the safety filter always
    runs, even with no restrictions). `model_version` ends in `-provisional`
    while the catalog is in the 150-499 row band. Not medical advice — see
    the `disclaimer` field on the response."""
    return run_recommend(request, catalog)
