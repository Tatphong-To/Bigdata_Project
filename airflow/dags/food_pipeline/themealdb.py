"""TheMealDB client + recipe parser (Phase 2b).

TheMealDB has recipes and ingredient lists but NO nutrition. This module only
fetches and structures the data; nutrition is estimated downstream
(``usda_client`` + ``ingredient_matcher`` + ``unit_converter`` +
``compute_recipe_nutrition``).

Free access uses the shared test key ``1`` — no registration. The HTTP
plumbing (``Transport`` / ``HttpResponse`` / ``UrllibTransport``) is reused
from ``spoonacular`` as-is; that module is not modified.

Verified response shape (food-rec-domain SKILL.md section 2): top level is
``{"meals": [...]}`` or ``{"meals": null}``; each meal has ``idMeal``,
``strMeal``, ``strCategory``, ``strArea``, ``strInstructions``, ``strTags``,
and ``strIngredient1..20`` / ``strMeasure1..20`` where unused slots are ``""``
or ``null``.
"""

from __future__ import annotations

import json
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any, Callable

from .spoonacular import HttpResponse, Transport, UrllibTransport

_MAX_INGREDIENT_SLOTS = 20

_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


class TheMealDbError(RuntimeError):
    pass


@dataclass(frozen=True)
class IngredientLine:
    """One ``strIngredientN`` / ``strMeasureN`` pair from a meal."""

    name: str  # normalized: stripped + lower-cased
    quantity_text: str  # raw measure text, e.g. "1 cup", "200g", "" if none
    slot: int  # 1-based position in the meal


@dataclass(frozen=True)
class ParsedMeal:
    meal_id: str
    name: str
    category: str | None
    area: str | None
    instructions: str | None
    tags: tuple[str, ...] = ()
    ingredients: tuple[IngredientLine, ...] = ()


def _clean(value: Any) -> str:
    """None / non-str / whitespace -> ''. Otherwise stripped string."""
    if not isinstance(value, str):
        return ""
    return value.strip()


def parse_meal(raw: dict[str, Any]) -> ParsedMeal:
    ingredients: list[IngredientLine] = []
    for slot in range(1, _MAX_INGREDIENT_SLOTS + 1):
        name = _clean(raw.get(f"strIngredient{slot}"))
        if not name:
            continue  # empty slot (or None) -> skip
        measure = _clean(raw.get(f"strMeasure{slot}"))
        ingredients.append(
            IngredientLine(
                name=name.lower(),
                quantity_text=measure,
                slot=slot,
            )
        )

    tags_raw = _clean(raw.get("strTags"))
    tags = tuple(t.strip().lower() for t in tags_raw.split(",") if t.strip()) if tags_raw else ()

    return ParsedMeal(
        meal_id=str(raw["idMeal"]) if raw.get("idMeal") is not None else "",
        name=_clean(raw.get("strMeal")),
        category=_clean(raw.get("strCategory")) or None,
        area=_clean(raw.get("strArea")) or None,
        instructions=_clean(raw.get("strInstructions")) or None,
        tags=tags,
        ingredients=tuple(ingredients),
    )


class TheMealDbClient:
    BASE = "https://www.themealdb.com/api/json/v1"

    def __init__(
        self,
        *,
        test_key: str = "1",
        transport: Transport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        min_request_interval_s: float = 0.5,
        timeout_s: float = 30.0,
        max_retries: int = 2,
        retry_backoff_s: float = 1.5,
    ) -> None:
        self._key = test_key
        self._transport = transport or UrllibTransport()
        self._sleep = sleep
        self._monotonic = monotonic
        self._min_interval = min_request_interval_s
        self._timeout = timeout_s
        self._max_retries = max_retries
        self._retry_backoff = retry_backoff_s
        self._last_call_at: float | None = None

    # -- public --------------------------------------------------------
    def search(self, name: str) -> list[dict[str, Any]]:
        """`search.php?s=` — full meal objects whose name contains ``name``."""
        return self._meals(self._get("search.php", {"s": name}))

    def lookup(self, meal_id: str | int) -> dict[str, Any] | None:
        """`lookup.php?i=` — one full meal object, or None if not found."""
        meals = self._meals(self._get("lookup.php", {"i": str(meal_id)}))
        return meals[0] if meals else None

    def list_by_category(self, category: str) -> list[dict[str, Any]]:
        """`filter.php?c=` — PARTIAL meal objects (idMeal/strMeal/thumb only).
        Follow up with :meth:`lookup` for ingredients."""
        return self._meals(self._get("filter.php", {"c": category}))

    def parsed_search(self, name: str) -> list[ParsedMeal]:
        return [parse_meal(m) for m in self.search(name)]

    # -- internals --------------------------------------------------
    def _get(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        url = f"{self.BASE}/{self._key}/{path}?" + urllib.parse.urlencode(params)
        headers = {
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "User-Agent": _BROWSER_UA,
        }
        attempts = self._max_retries + 1
        last_err = "unknown error"
        for attempt in range(attempts):
            self._respect_rate_limit()
            try:
                resp: HttpResponse = self._transport.get(url, headers, self._timeout)
                self._last_call_at = self._monotonic()
            except Exception as e:  # timeout / connection error — TheMealDB is flaky
                self._last_call_at = self._monotonic()
                last_err = str(e) or e.__class__.__name__
                if attempt + 1 < attempts:
                    self._sleep(self._retry_backoff * (attempt + 1))
                    continue
                raise TheMealDbError(
                    f"{path} failed after {attempts} attempts: {last_err}"
                ) from None

            if resp.status == 200:
                try:
                    return json.loads(resp.body)
                except json.JSONDecodeError as e:
                    raise TheMealDbError(f"non-JSON response from {path}: {e}") from None
            last_err = f"HTTP {resp.status}: {resp.body[:200]}"
            if resp.status >= 500 and attempt + 1 < attempts:
                self._sleep(self._retry_backoff * (attempt + 1))
                continue
            raise TheMealDbError(f"HTTP {resp.status} from {path}: {resp.body[:200]}")
        raise TheMealDbError(f"{path} failed after {attempts} attempts: {last_err}")

    @staticmethod
    def _meals(payload: dict[str, Any]) -> list[dict[str, Any]]:
        meals = payload.get("meals")
        return list(meals) if meals else []  # None -> []

    def _respect_rate_limit(self) -> None:
        if self._last_call_at is None:
            return
        wait = self._min_interval - (self._monotonic() - self._last_call_at)
        if wait > 0:
            self._sleep(wait)
