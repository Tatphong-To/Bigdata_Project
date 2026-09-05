"""Persisted daily point-quota tracker for Spoonacular.

The quota state lives in Postgres (``food_db.extraction_quota``, one row per
day), NOT in process memory. A restarted DAG / worker / container reads the
same row and continues counting from where it left off — it never resets the
budget by restarting. See CLAUDE.md ("persisted counter ... survives DAG
restarts") and the food-rec-domain pitfalls checklist.

The tracker counts **points** (cost model in ``cost.py``), not request count.

Storage is behind the ``QuotaStore`` protocol so the tracker can be unit
tested without a database; ``PostgresQuotaStore`` in ``db.py`` is the real
implementation.
"""

from __future__ import annotations

import datetime as _dt
from typing import Callable, Protocol


class QuotaStore(Protocol):
    """Minimal persistence contract for per-day usage."""

    def get_usage(self, day: _dt.date) -> tuple[float, int]:
        """Return ``(points_used, request_count)`` for ``day`` (0, 0 if none)."""
        ...

    def add_usage(
        self, day: _dt.date, points: float, requests: int
    ) -> tuple[float, int]:
        """Atomically add usage to ``day``'s row and return the NEW totals.

        Must be a single atomic upsert (INSERT ... ON CONFLICT DO UPDATE
        ... RETURNING) so concurrent tasks cannot lose an increment.
        """
        ...


class QuotaExceeded(RuntimeError):
    """Raised when an operation would push past the daily point budget."""


class QuotaTracker:
    def __init__(
        self,
        store: QuotaStore,
        daily_point_quota: float,
        *,
        today: Callable[[], _dt.date] = _dt.date.today,
        safety_margin_points: float = 0.0,
    ) -> None:
        if daily_point_quota <= 0:
            raise ValueError("daily_point_quota must be > 0")
        self._store = store
        self._quota = float(daily_point_quota)
        self._today = today
        self._margin = float(safety_margin_points)

    # -- reads -------------------------------------------------------------
    def points_used(self) -> float:
        used, _ = self._store.get_usage(self._today())
        return used

    def remaining(self) -> float:
        """Points left today, after the safety margin. Never negative."""
        return max(0.0, self._quota - self._margin - self.points_used())

    def can_afford(self, estimated_points: float) -> bool:
        if estimated_points < 0:
            raise ValueError("estimated_points must be >= 0")
        return estimated_points <= self.remaining()

    # -- writes ----------------------------------------------------------
    def charge(self, points: float, requests: int = 1) -> float:
        """Persist ``points`` of usage for today; return the new running total.

        Charge with the *actual* cost from ``X-API-Quota-Request`` after a call
        succeeds. Charging is allowed to cross the limit (the real API already
        served the request) — the guard is ``can_afford`` *before* the call.
        """
        if points < 0:
            raise ValueError("points must be >= 0")
        new_used, _ = self._store.add_usage(self._today(), points, requests)
        return new_used

    def guard(self, estimated_points: float) -> None:
        """Raise :class:`QuotaExceeded` if ``estimated_points`` will not fit."""
        if not self.can_afford(estimated_points):
            raise QuotaExceeded(
                f"call needs ~{estimated_points:.2f} pts but only "
                f"{self.remaining():.2f} of {self._quota:.0f} remain today"
            )


class InMemoryQuotaStore:
    """Non-persistent store. For tests and dry runs only — NEVER wire this into
    the real pipeline (it defeats the whole point of a persisted counter)."""

    def __init__(self) -> None:
        self._rows: dict[_dt.date, list[float]] = {}

    def get_usage(self, day: _dt.date) -> tuple[float, int]:
        pts, reqs = self._rows.get(day, [0.0, 0])
        return float(pts), int(reqs)

    def add_usage(
        self, day: _dt.date, points: float, requests: int
    ) -> tuple[float, int]:
        pts, reqs = self._rows.get(day, [0.0, 0])
        pts += float(points)
        reqs += int(requests)
        self._rows[day] = [pts, reqs]
        return pts, reqs
