"""Run one Spoonacular extraction batch.

Pulls a handful of ``complexSearch`` pages (stopping the moment the persisted
daily point budget can't cover the next call), writes the raw payloads under
``data/raw/``, then parses + validates the recipes into a staging file.

This is a plain function, called by the Airflow ``extract_menus`` /
``validate_nutrition_data`` tasks in Phase 3 — it is not itself a DAG.

The staging file is an interim hand-off artifact (JSON under ``data/raw/``,
git-ignored). It is transient pipeline plumbing, not a data store — Phase 3
decides how tasks pass data (XCom / table). It is NOT a return to a
file-based data-lake approach (CLAUDE.md: Postgres-only for real data).
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .parse import parse_search_results
from .spoonacular import QuotaExhaustedError, SpoonacularClient
from .validate import Rejection, validate_batch

logger = logging.getLogger(__name__)


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


@dataclass
class ExtractionRun:
    raw_path: Path
    staging_path: Path
    accepted: list[dict[str, Any]]
    rejected: list[Rejection]
    queries_completed: list[str] = field(default_factory=list)
    stopped_early: bool = False
    points_used_before: float = 0.0
    points_used_after: float = 0.0

    @property
    def n_accepted(self) -> int:
        return len(self.accepted)

    @property
    def n_rejected(self) -> int:
        return len(self.rejected)


def run_extraction(
    client: SpoonacularClient,
    *,
    queries: list[str],
    out_dir: str | Path,
    number: int | None = None,
    now: Callable[[], _dt.datetime] = _utcnow,
) -> ExtractionRun:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    points_before = client.quota.points_used()

    payloads: list[dict[str, Any]] = []
    completed: list[str] = []
    stopped_early = False
    for query in queries:
        try:
            payload = client.complex_search(query=query, number=number)
        except QuotaExhaustedError as exc:
            stopped_early = True
            logger.warning(
                "extract: stopping early before query %r — %s", query, exc
            )
            break
        payloads.append(payload)
        completed.append(query)
        logger.info(
            "extract: query %r returned %d recipes",
            query,
            len(payload.get("results", [])),
        )

    stamp = now().strftime("%Y%m%dT%H%M%SZ")
    raw_path = out_dir / f"{stamp}_spoonacular_raw.json"
    raw_path.write_text(
        json.dumps({"fetched_at": stamp, "queries": completed, "payloads": payloads}),
        encoding="utf-8",
    )

    parsed: list[dict[str, Any]] = []
    seen: set[str] = set()
    for payload in payloads:
        for row in parse_search_results(payload):
            mid = row["menu_id"]
            if mid is not None:
                if mid in seen:
                    continue
                seen.add(mid)
            parsed.append(row)

    accepted, rejected = validate_batch(parsed)

    staging_path = out_dir / f"{stamp}_staging.json"
    staging_path.write_text(
        json.dumps(
            {
                "fetched_at": stamp,
                "accepted": accepted,
                "rejected": [
                    {"menu_id": r.menu_id, "name": r.name, "reasons": list(r.reasons)}
                    for r in rejected
                ],
            }
        ),
        encoding="utf-8",
    )

    points_after = client.quota.points_used()
    logger.info(
        "extract: %d accepted, %d rejected; %.2f points spent this run "
        "(%.2f -> %.2f); stopped_early=%s",
        len(accepted),
        len(rejected),
        points_after - points_before,
        points_before,
        points_after,
        stopped_early,
    )

    return ExtractionRun(
        raw_path=raw_path,
        staging_path=staging_path,
        accepted=accepted,
        rejected=rejected,
        queries_completed=completed,
        stopped_early=stopped_early,
        points_used_before=points_before,
        points_used_after=points_after,
    )
