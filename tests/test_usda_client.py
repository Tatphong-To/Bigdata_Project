"""usda_client: env-key handling (no DEMO_KEY fallback), per-100g macro
extraction by nutrientNumber + unitName, data-type ordering, error mapping."""

import json

import pytest

from food_pipeline.spoonacular import HttpResponse
from food_pipeline.usda_client import (
    UsdaClient,
    UsdaConfig,
    UsdaError,
    _extract_macros,
)


# --- config / key -----------------------------------------------------
def test_from_env_requires_key():
    with pytest.raises(RuntimeError, match="USDA_FDC_API_KEY is not set"):
        UsdaConfig.from_env(env={})


def test_from_env_rejects_demo_key():
    with pytest.raises(RuntimeError, match="DEMO_KEY"):
        UsdaConfig.from_env(env={"USDA_FDC_API_KEY": "DEMO_KEY"})


def test_from_env_accepts_real_key():
    cfg = UsdaConfig.from_env(env={"USDA_FDC_API_KEY": "realkey123"})
    assert cfg.api_key == "realkey123"


# --- macro extraction ------------------------------------------------
def _nutrients():
    return [
        {"nutrientNumber": "203", "unitName": "G", "value": 21.4, "nutrientName": "Protein"},
        {"nutrientNumber": "204", "unitName": "G", "value": 28.6, "nutrientName": "Total lipid (fat)"},
        {"nutrientNumber": "205", "unitName": "G", "value": 3.57, "nutrientName": "Carbohydrate, by difference"},
        {"nutrientNumber": "208", "unitName": "KCAL", "value": 393, "nutrientName": "Energy"},
        {"nutrientNumber": "268", "unitName": "kJ", "value": 1644, "nutrientName": "Energy"},
    ]


def test_extract_macros_by_number_and_unit():
    m = _extract_macros(_nutrients())
    assert m["calories_per_100g"] == pytest.approx(393)
    assert m["protein_g_per_100g"] == pytest.approx(21.4)
    assert m["carbs_g_per_100g"] == pytest.approx(3.57)
    assert m["fat_g_per_100g"] == pytest.approx(28.6)


def test_extract_macros_ignores_kj_energy():
    only_kj = [{"nutrientNumber": "268", "unitName": "KJ", "value": 1644}]
    assert _extract_macros(only_kj)["calories_per_100g"] is None


def test_extract_macros_missing_field_is_none():
    partial = [{"nutrientNumber": "208", "unitName": "KCAL", "value": 100}]
    m = _extract_macros(partial)
    assert m["calories_per_100g"] == 100
    assert m["protein_g_per_100g"] is None


def test_extract_macros_wrong_unit_is_none():
    wrong = [{"nutrientNumber": "203", "unitName": "MG", "value": 21000}]
    assert _extract_macros(wrong)["protein_g_per_100g"] is None


# --- client search --------------------------------------------------
def _search_payload():
    return {
        "foods": [
            {"fdcId": 111, "description": "CHEDDAR CHEESE", "dataType": "Branded",
             "foodNutrients": _nutrients()},
            {"fdcId": 222, "description": "Cheese, cheddar", "dataType": "Foundation",
             "foodNutrients": _nutrients()},
            {"fdcId": 333, "description": "Cheese, cheddar (Aussie)", "dataType": "SR Legacy",
             "foodNutrients": _nutrients()},
        ]
    }


class FakeTransport:
    def __init__(self, response):
        # a single HttpResponse, or a list of responses / callables (raise or return)
        self._seq = response if isinstance(response, list) else None
        self.response = None if self._seq is not None else response
        self.calls = []

    def get(self, url, headers, timeout):
        self.calls.append(url)
        if self._seq is not None:
            item = self._seq.pop(0)
            if callable(item):
                return item()
            return item
        return self.response


def make_client(response, **cfg_kw):
    cfg = UsdaConfig(api_key="SECRET", min_request_interval_s=0.0, **cfg_kw)
    sleeps = []
    client = UsdaClient(cfg, transport=FakeTransport(response),
                        sleep=lambda s: sleeps.append(s), monotonic=lambda: 0.0)
    client._test_sleeps = sleeps  # type: ignore[attr-defined]
    return client


def test_search_orders_by_preferred_data_type():
    resp = HttpResponse(200, {"X-RateLimit-Remaining": "3598"}, json.dumps(_search_payload()))
    client = make_client(resp)
    foods = client.search_foods("cheddar")
    assert [f.data_type for f in foods] == ["Foundation", "SR Legacy", "Branded"]
    assert client.last_rate_limit_remaining == 3598


def test_search_all_macros_flag():
    payload = {"foods": [
        {"fdcId": 1, "description": "x", "dataType": "Foundation", "foodNutrients": _nutrients()},
        {"fdcId": 2, "description": "y", "dataType": "Foundation",
         "foodNutrients": [{"nutrientNumber": "208", "unitName": "KCAL", "value": 10}]},
    ]}
    client = make_client(HttpResponse(200, {}, json.dumps(payload)))
    foods = {f.fdc_id: f for f in client.search_foods("x")}
    assert foods[1].has_all_macros is True
    assert foods[2].has_all_macros is False


def test_429_raises_with_retry_after():
    client = make_client(HttpResponse(429, {"Retry-After": "60"}, "rate limited"))
    with pytest.raises(UsdaError, match="429"):
        client.search_foods("x")


def test_401_redacts_key():
    client = make_client(HttpResponse(401, {}, "bad key SECRET"))
    with pytest.raises(UsdaError) as ei:
        client.search_foods("x")
    assert "SECRET" not in str(ei.value)


def test_retries_transient_400_then_succeeds():
    ok = HttpResponse(200, {}, json.dumps(_search_payload()))
    bad = HttpResponse(400, {}, "<html>400 Bad Request nginx</html>")
    client = make_client([bad, bad, ok], max_retries=3, retry_backoff_s=1.0)
    foods = client.search_foods("cheddar")
    assert len(foods) == 3
    assert client._transport.calls  # 3 calls made
    assert len(client._transport.calls) == 3
    assert client._test_sleeps == [pytest.approx(1.0), pytest.approx(2.0)]


def test_retries_timeout_then_succeeds():
    ok = HttpResponse(200, {}, json.dumps({"foods": []}))
    def boom():
        raise TimeoutError("The read operation timed out")
    client = make_client([boom, ok], max_retries=2)
    assert client.search_foods("x") == []
    assert len(client._transport.calls) == 2


def test_gives_up_after_max_retries():
    bad = HttpResponse(503, {}, "service unavailable")
    client = make_client([bad, bad, bad], max_retries=2, retry_backoff_s=0.1)
    with pytest.raises(UsdaError, match="after 3 attempts"):
        client.search_foods("x")
    assert len(client._transport.calls) == 3


def test_429_is_not_retried():
    client = make_client([HttpResponse(429, {"Retry-After": "30"}, "slow down")],
                         max_retries=3)
    with pytest.raises(UsdaError, match="429"):
        client.search_foods("x")
    assert len(client._transport.calls) == 1


def test_401_is_not_retried():
    client = make_client([HttpResponse(401, {}, "bad key")], max_retries=3)
    with pytest.raises(UsdaError):
        client.search_foods("x")
    assert len(client._transport.calls) == 1


def test_api_key_goes_on_the_wire():
    resp = HttpResponse(200, {}, json.dumps({"foods": []}))
    t = FakeTransport(resp)
    cfg = UsdaConfig(api_key="SECRET", min_request_interval_s=0.0)
    UsdaClient(cfg, transport=t, sleep=lambda s: None, monotonic=lambda: 0.0).search_foods("x")
    assert "api_key=SECRET" in t.calls[0]
    assert "dataType=" in t.calls[0]
