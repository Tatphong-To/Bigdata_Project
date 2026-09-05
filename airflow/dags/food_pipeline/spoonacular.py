"""Paced, quota-aware Spoonacular client.

Responsibilities:
  * never start a call that would blow the persisted daily point budget
    (checks ``QuotaTracker.can_afford`` using the verified cost model);
  * honour the 1 req/s free-plan rate limit;
  * send a browser User-Agent (the default python-urllib UA is Cloudflare-
    banned → 403 error 1010);
  * charge the persisted counter with the ACTUAL cost from the
    ``X-API-Quota-Request`` response header;
  * translate quota / ban responses into clear exceptions.

HTTP is behind the ``Transport`` protocol so tests never touch the network.
The real transport uses the stdlib (``urllib``) — a low-volume paced job does
not need an extra dependency, and urllib already handles the Cloudflare case
once the UA is set.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable, Protocol

from .config import ExtractConfig
from .cost import estimate_search_cost
from .quota import QuotaTracker


class SpoonacularError(RuntimeError):
    pass


class QuotaExhaustedError(SpoonacularError):
    """The API refused the call for quota reasons (HTTP 402), or we declined
    to make it because the persisted budget could not cover it."""


class UserAgentBannedError(SpoonacularError):
    """Cloudflare 403 / error 1010 — the User-Agent is banned. Not an auth or
    quota problem; set a browser-style UA."""


@dataclass
class HttpResponse:
    status: int
    headers: dict[str, str]
    body: str

    def header(self, name: str) -> str | None:
        low = name.lower()
        for k, v in self.headers.items():
            if k.lower() == low:
                return v
        return None


class Transport(Protocol):
    def get(
        self, url: str, headers: dict[str, str], timeout: float
    ) -> HttpResponse:
        ...


class UrllibTransport:
    def get(
        self, url: str, headers: dict[str, str], timeout: float
    ) -> HttpResponse:
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return HttpResponse(
                    status=resp.status,
                    headers={k: v for k, v in resp.headers.items()},
                    body=resp.read().decode("utf-8", "replace"),
                )
        except urllib.error.HTTPError as e:  # noqa: PERF203
            body = ""
            if hasattr(e, "read"):
                try:
                    body = e.read().decode("utf-8", "replace")
                except Exception:  # pragma: no cover - defensive
                    body = ""
            return HttpResponse(
                status=e.code,
                headers={k: v for k, v in (e.headers or {}).items()},
                body=body,
            )


class SpoonacularClient:
    def __init__(
        self,
        config: ExtractConfig,
        quota: QuotaTracker,
        *,
        transport: Transport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._cfg = config
        self._quota = quota
        self._transport = transport or UrllibTransport()
        self._sleep = sleep
        self._monotonic = monotonic
        self._last_call_at: float | None = None

    @property
    def quota(self) -> QuotaTracker:
        return self._quota

    # -- public API -----------------------------------------------------
    def complex_search(
        self,
        *,
        query: str | None = None,
        number: int | None = None,
        offset: int = 0,
        add_recipe_nutrition: bool = True,
        extra_params: dict[str, str] | None = None,
    ) -> dict:
        """One ``/recipes/complexSearch`` page.

        Raises :class:`QuotaExhaustedError` *before* any HTTP call if the
        persisted budget cannot cover the estimated cost.
        """
        number = self._cfg.number_per_query if number is None else number
        estimate = estimate_search_cost(number)
        if not self._quota.can_afford(estimate):
            raise QuotaExhaustedError(
                f"skipping call: needs ~{estimate:.2f} pts, only "
                f"{self._quota.remaining():.2f} left in today's budget"
            )

        params = {
            "number": str(number),
            "offset": str(offset),
            "addRecipeNutrition": "true" if add_recipe_nutrition else "false",
            "apiKey": self._cfg.api_key,
        }
        if query:
            params["query"] = query
        if extra_params:
            params.update(extra_params)

        url = f"{self._cfg.base_url}/recipes/complexSearch?" + urllib.parse.urlencode(
            params
        )
        resp = self._request(url)
        data = self._parse_json(resp)

        actual = self._charge_from_headers(resp, fallback=estimate)
        results = data.get("results", [])
        # sanity: cost should track the number actually returned
        return data

    # -- internals ----------------------------------------------------
    def _request(self, url: str) -> HttpResponse:
        self._respect_rate_limit()
        headers = {
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "User-Agent": self._cfg.user_agent,
        }
        try:
            resp = self._transport.get(url, headers, self._cfg.request_timeout_s)
        except Exception as e:  # network error: redact key from message
            raise SpoonacularError(self._cfg.redact(str(e))) from None
        finally:
            self._last_call_at = self._monotonic()

        if resp.status == 200:
            return resp
        if resp.status == 402:
            raise QuotaExhaustedError(
                "Spoonacular returned HTTP 402 — daily quota exhausted"
            )
        if resp.status == 403 and "1010" in resp.body:
            raise UserAgentBannedError(
                "Spoonacular/Cloudflare 403 error 1010: User-Agent banned. "
                "Set a browser-style SPOONACULAR_USER_AGENT."
            )
        raise SpoonacularError(
            self._cfg.redact(f"HTTP {resp.status}: {resp.body[:300]}")
        )

    def _respect_rate_limit(self) -> None:
        if self._last_call_at is None:
            return
        elapsed = self._monotonic() - self._last_call_at
        wait = self._cfg.min_request_interval_s - elapsed
        if wait > 0:
            self._sleep(wait)

    def _parse_json(self, resp: HttpResponse) -> dict:
        try:
            return json.loads(resp.body)
        except json.JSONDecodeError as e:
            raise SpoonacularError(f"non-JSON response: {e}") from None

    def _charge_from_headers(self, resp: HttpResponse, *, fallback: float) -> float:
        raw = resp.header("X-API-Quota-Request")
        try:
            actual = float(raw) if raw is not None else fallback
        except ValueError:
            actual = fallback
        self._quota.charge(actual, requests=1)
        return actual
