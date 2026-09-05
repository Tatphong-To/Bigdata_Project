"""Environment-driven configuration for the extractor.

Kept Spoonacular-specific on purpose. Other sources (e.g. USDA FoodData
Central) are NOT in scope yet and no structure is reserved for them — that
will be designed together when it is agreed (Phase 2b).
"""

from __future__ import annotations

import dataclasses
import os
from collections.abc import Mapping

# Spoonacular's Cloudflare bans the default python-urllib user-agent (403,
# error 1010). A normal browser UA works — see docs/spoonacular-quota.md.
_DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


@dataclasses.dataclass(frozen=True)
class ExtractConfig:
    api_key: str
    base_url: str = "https://api.spoonacular.com"
    user_agent: str = _DEFAULT_UA
    # Verified allowance is 50.00 points/day (2026-09-05). Overridable because
    # providers change limits; confirm against the account console.
    daily_point_quota: float = 50.0
    # Leave a little unspent so a mis-estimate near the edge doesn't 402.
    safety_margin_points: float = 1.0
    # Free plan documents 1 request/second.
    min_request_interval_s: float = 1.1
    # Recipes per complexSearch page. cost = 1 + 0.06 * number.
    number_per_query: int = 20
    request_timeout_s: float = 30.0

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "ExtractConfig":
        env = os.environ if env is None else env
        api_key = env.get("SPOONACULAR_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError(
                "SPOONACULAR_API_KEY is not set (put it in .env / the Airflow "
                "environment). It is never committed."
            )
        kwargs: dict[str, object] = {"api_key": api_key}
        if v := env.get("SPOONACULAR_BASE_URL", "").strip():
            kwargs["base_url"] = v
        if v := env.get("SPOONACULAR_USER_AGENT", "").strip():
            kwargs["user_agent"] = v
        if v := env.get("SPOONACULAR_DAILY_POINT_QUOTA", "").strip():
            kwargs["daily_point_quota"] = float(v)
        if v := env.get("SPOONACULAR_SAFETY_MARGIN_POINTS", "").strip():
            kwargs["safety_margin_points"] = float(v)
        if v := env.get("SPOONACULAR_MIN_REQUEST_INTERVAL_S", "").strip():
            kwargs["min_request_interval_s"] = float(v)
        if v := env.get("SPOONACULAR_NUMBER_PER_QUERY", "").strip():
            kwargs["number_per_query"] = int(v)
        return cls(**kwargs)  # type: ignore[arg-type]

    def redact(self, text: str) -> str:
        """Replace the API key wherever it appears (log/exception hygiene)."""
        return text.replace(self.api_key, "***REDACTED***") if self.api_key else text
