"""USDA FoodData Central client (Phase 2b).

Searches FDC for a food name and returns candidates with macro nutrition
**per 100 g**. Fuzzy name matching + confidence scoring is a separate concern
(:mod:`food_pipeline.ingredient_matcher`); this module only talks to the API
and normalises the nutrient rows.

The API key is read from ``os.environ["USDA_FDC_API_KEY"]``. There is **no
``DEMO_KEY`` fallback** — if the env var is missing (or is literally
``DEMO_KEY``), construction raises. DEMO_KEY is 30 req/hour / 50 req/day and
must never be substituted silently.

Verified against a real response (food-rec-domain SKILL.md section 3b):
``/foods/search`` returns ``foods[]`` where each food has ``fdcId``,
``dataType``, ``description``, ``foodNutrients[]`` with flat
``{nutrientNumber, unitName, value}`` rows, values per 100 g. Rate limit
header ``X-Ratelimit-Limit: 3600`` (per hour, per key).
"""

from __future__ import annotations

import json
import os
import time
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Callable

from .spoonacular import HttpResponse, Transport, UrllibTransport

_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# macro -> (nutrientNumber, required unitName)
_MACRO_NUTRIENTS: dict[str, tuple[str, str]] = {
    "calories_per_100g": ("208", "KCAL"),
    "protein_g_per_100g": ("203", "G"),
    "carbs_g_per_100g": ("205", "G"),
    "fat_g_per_100g": ("204", "G"),
}


class UsdaError(RuntimeError):
    pass


@dataclass(frozen=True)
class UsdaConfig:
    api_key: str
    base_url: str = "https://api.nal.usda.gov/fdc/v1"
    user_agent: str = _BROWSER_UA
    page_size: int = 5
    # best first; used to bias candidate ordering toward generic references
    preferred_data_types: tuple[str, ...] = (
        "Foundation",
        "SR Legacy",
        "Survey (FNDDS)",
        "Branded",
    )
    min_request_interval_s: float = 1.0
    timeout_s: float = 45.0
    rate_limit_per_hour: int = 3600  # verified 2026-09-05, informational
    # api.data.gov's edge intermittently returns 400/5xx or times out under
    # load even for valid requests (the same call succeeds seconds later), so
    # transient failures are retried with linear backoff. 429 is NOT retried
    # here — it is surfaced so the caller can honour Retry-After.
    max_retries: int = 3
    retry_backoff_s: float = 1.5

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None, **overrides: Any) -> "UsdaConfig":
        env = os.environ if env is None else env
        key = (env.get("USDA_FDC_API_KEY") or "").strip()
        if not key:
            raise RuntimeError(
                "USDA_FDC_API_KEY is not set. Put a real FoodData Central key "
                "(https://api.data.gov/signup/) in the environment. There is no "
                "DEMO_KEY fallback."
            )
        if key == "DEMO_KEY":
            raise RuntimeError(
                "USDA_FDC_API_KEY is 'DEMO_KEY' — that is the shared demo key "
                "(30 req/hour). Set a real signed-up key."
            )
        return cls(api_key=key, **overrides)

    def redact(self, text: str) -> str:
        return text.replace(self.api_key, "***REDACTED***") if self.api_key else text


@dataclass(frozen=True)
class UsdaFood:
    fdc_id: int
    description: str
    data_type: str
    calories_per_100g: float | None
    protein_g_per_100g: float | None
    carbs_g_per_100g: float | None
    fat_g_per_100g: float | None

    @property
    def has_all_macros(self) -> bool:
        return all(
            v is not None
            for v in (
                self.calories_per_100g,
                self.protein_g_per_100g,
                self.carbs_g_per_100g,
                self.fat_g_per_100g,
            )
        )


def _extract_macros(food_nutrients: list[dict[str, Any]]) -> dict[str, float | None]:
    by_number: dict[str, dict[str, Any]] = {}
    for n in food_nutrients:
        num = str(n.get("nutrientNumber") or n.get("number") or "").strip()
        if num and num not in by_number:
            by_number[num] = n
    out: dict[str, float | None] = {}
    for field_name, (number, unit) in _MACRO_NUTRIENTS.items():
        row = by_number.get(number)
        value = None
        if row is not None:
            row_unit = str(row.get("unitName") or "").strip().upper()
            if row_unit == unit:
                try:
                    value = float(row["value"])
                except (KeyError, TypeError, ValueError):
                    value = None
        out[field_name] = value
    return out


def _food_from_raw(raw: dict[str, Any]) -> UsdaFood:
    macros = _extract_macros(raw.get("foodNutrients") or [])
    return UsdaFood(
        fdc_id=int(raw["fdcId"]),
        description=str(raw.get("description") or ""),
        data_type=str(raw.get("dataType") or ""),
        **macros,
    )


class UsdaClient:
    def __init__(
        self,
        config: UsdaConfig,
        *,
        transport: Transport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._cfg = config
        self._transport = transport or UrllibTransport()
        self._sleep = sleep
        self._monotonic = monotonic
        self._last_call_at: float | None = None
        self._last_rate_limit_remaining: int | None = None

    @property
    def last_rate_limit_remaining(self) -> int | None:
        return self._last_rate_limit_remaining

    def search_foods(
        self,
        query: str,
        *,
        page_size: int | None = None,
        data_types: tuple[str, ...] | None = None,
    ) -> list[UsdaFood]:
        """Search FDC for ``query``; return candidate foods with per-100 g
        macros, ordered by the API's relevance then by
        ``preferred_data_types``."""
        params = {
            "query": query,
            "pageSize": str(page_size or self._cfg.page_size),
            "api_key": self._cfg.api_key,
        }
        dts = data_types if data_types is not None else self._cfg.preferred_data_types
        if dts:
            params["dataType"] = ",".join(dts)

        url = f"{self._cfg.base_url}/foods/search?" + urllib.parse.urlencode(params)
        payload = self._get(url)
        foods = [
            _food_from_raw(f)
            for f in payload.get("foods", [])
            if f.get("fdcId") is not None
        ]
        rank = {dt: i for i, dt in enumerate(self._cfg.preferred_data_types)}
        # stable sort: keep API relevance order within the same data type
        foods.sort(key=lambda f: rank.get(f.data_type, len(rank)))
        return foods

    # -- internals --------------------------------------------------
    # api.data.gov's edge flakes on valid requests under load — retry these.
    _RETRYABLE_STATUS = frozenset({400, 408, 500, 502, 503, 504})

    def _get(self, url: str) -> dict[str, Any]:
        headers = {
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "User-Agent": self._cfg.user_agent,
        }
        attempts = self._cfg.max_retries + 1
        last_err: str = "unknown error"
        for attempt in range(attempts):
            self._respect_rate_limit()
            try:
                resp: HttpResponse = self._transport.get(
                    url, headers, self._cfg.timeout_s
                )
            except Exception as e:  # network error / timeout
                last_err = self._cfg.redact(str(e)) or e.__class__.__name__
                self._last_call_at = self._monotonic()
                if attempt + 1 < attempts:
                    self._sleep(self._cfg.retry_backoff_s * (attempt + 1))
                    continue
                raise UsdaError(f"request failed after {attempts} attempts: {last_err}") from None
            self._last_call_at = self._monotonic()

            remaining = resp.header("X-RateLimit-Remaining")
            if remaining is not None:
                try:
                    self._last_rate_limit_remaining = int(remaining)
                except ValueError:
                    pass

            if resp.status == 200:
                try:
                    return json.loads(resp.body)
                except json.JSONDecodeError as e:
                    raise UsdaError(f"non-JSON response: {e}") from None
            if resp.status == 429:  # not retried here — surface Retry-After
                raise UsdaError(
                    f"HTTP 429 rate limited (Retry-After={resp.header('Retry-After')})"
                )
            if resp.status in (401, 403):
                raise UsdaError(
                    self._cfg.redact(f"HTTP {resp.status} — check USDA_FDC_API_KEY")
                )
            last_err = self._cfg.redact(f"HTTP {resp.status}: {resp.body[:200]}")
            if resp.status in self._RETRYABLE_STATUS:
                if attempt + 1 < attempts:
                    self._sleep(self._cfg.retry_backoff_s * (attempt + 1))
                    continue
                raise UsdaError(f"request failed after {attempts} attempts: {last_err}")
            raise UsdaError(last_err)
        raise UsdaError(f"request failed after {attempts} attempts: {last_err}")

    def _respect_rate_limit(self) -> None:
        if self._last_call_at is None:
            return
        wait = self._cfg.min_request_interval_s - (self._monotonic() - self._last_call_at)
        if wait > 0:
            self._sleep(wait)
