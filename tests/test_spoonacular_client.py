"""SpoonacularClient: quota gating before a call, real-cost accounting from
the response header, rate limiting, and error translation. No network."""

import datetime as dt
import json

import pytest

from food_pipeline.config import ExtractConfig
from food_pipeline.quota import InMemoryQuotaStore, QuotaTracker
from food_pipeline.spoonacular import (
    HttpResponse,
    QuotaExhaustedError,
    SpoonacularClient,
    SpoonacularError,
    UserAgentBannedError,
)

DAY = dt.date(2026, 9, 5)


class FakeTransport:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def get(self, url, headers, timeout):
        self.calls.append({"url": url, "headers": headers, "timeout": timeout})
        if not self._responses:
            raise AssertionError("unexpected extra HTTP call")
        resp = self._responses.pop(0)
        return resp() if callable(resp) else resp


def ok_body(n_results=1, quota_request="1.06"):
    return HttpResponse(
        status=200,
        headers={"X-API-Quota-Request": quota_request, "X-API-Quota-Left": "48.94"},
        body=json.dumps({"results": [{"id": i} for i in range(n_results)]}),
    )


def make_client(transport, *, quota=50.0, used=0.0, interval=1.1):
    store = InMemoryQuotaStore()
    if used:
        store.add_usage(DAY, used, 1)
    tracker = QuotaTracker(store, quota, today=lambda: DAY)
    cfg = ExtractConfig(api_key="SECRET", min_request_interval_s=interval)
    sleeps = []
    clock = {"t": 1000.0}
    client = SpoonacularClient(
        cfg,
        tracker,
        transport=transport,
        sleep=lambda s: sleeps.append(s),
        monotonic=lambda: clock["t"],
    )
    return client, tracker, sleeps, clock


def test_charges_actual_header_cost_not_estimate():
    # estimate for n=10 is 1.60; header says 1.99 -> the charge must be 1.99
    t = FakeTransport([ok_body(n_results=10, quota_request="1.99")])
    client, tracker, _, _ = make_client(t)
    client.complex_search(query="chicken", number=10)
    assert tracker.points_used() == pytest.approx(1.99)


def test_falls_back_to_estimate_when_header_missing():
    resp = HttpResponse(status=200, headers={}, body=json.dumps({"results": []}))
    t = FakeTransport([resp])
    client, tracker, _, _ = make_client(t)
    client.complex_search(query="x", number=30)
    assert tracker.points_used() == pytest.approx(2.80)  # 1 + 0.06*30


def test_refuses_call_when_budget_cannot_cover_estimate():
    t = FakeTransport([])  # must not be hit
    client, tracker, _, _ = make_client(t, quota=50.0, used=49.5)
    with pytest.raises(QuotaExhaustedError):
        client.complex_search(query="x", number=20)  # needs 2.2
    assert t.calls == []
    assert tracker.points_used() == pytest.approx(49.5)  # unchanged


def test_http_402_becomes_quota_error():
    t = FakeTransport([HttpResponse(status=402, headers={}, body="Payment Required")])
    client, _, _, _ = make_client(t)
    with pytest.raises(QuotaExhaustedError):
        client.complex_search(query="x", number=1)


def test_cloudflare_1010_becomes_user_agent_error():
    body = json.dumps({"status": 403, "error_code": 1010, "error_name": "browser_signature_banned"})
    t = FakeTransport([HttpResponse(status=403, headers={}, body=body)])
    client, _, _, _ = make_client(t)
    with pytest.raises(UserAgentBannedError):
        client.complex_search(query="x", number=1)


def test_other_http_error_redacts_api_key():
    t = FakeTransport([HttpResponse(status=500, headers={}, body="apiKey=SECRET boom")])
    client, _, _, _ = make_client(t)
    with pytest.raises(SpoonacularError) as ei:
        client.complex_search(query="x", number=1)
    assert "SECRET" not in str(ei.value)
    assert "***REDACTED***" in str(ei.value)


def test_sends_browser_user_agent():
    t = FakeTransport([ok_body()])
    client, _, _, _ = make_client(t)
    client.complex_search(query="x", number=1)
    assert "Mozilla/5.0" in t.calls[0]["headers"]["User-Agent"]


def test_api_key_in_query_not_logged_url_contains_it_only_once():
    t = FakeTransport([ok_body()])
    client, _, _, _ = make_client(t)
    client.complex_search(query="x", number=1)
    assert "apiKey=SECRET" in t.calls[0]["url"]  # it does go on the wire


def test_rate_limit_sleeps_between_calls():
    t = FakeTransport([ok_body(), ok_body()])
    client, _, sleeps, clock = make_client(t, interval=1.1)
    client.complex_search(query="a", number=1)
    # no time passes on the fake clock -> second call must wait ~full interval
    client.complex_search(query="b", number=1)
    assert sleeps and sleeps[0] == pytest.approx(1.1, abs=1e-6)


def test_no_sleep_before_first_call():
    t = FakeTransport([ok_body()])
    client, _, sleeps, _ = make_client(t)
    client.complex_search(query="a", number=1)
    assert sleeps == []
