# Spoonacular free-tier quota — verification notes

**Verified on:** 2026-09-05 — against **real API responses** (3 calls).
**Status:** ✅ Verified. Free-tier allowance is **50 points/day**.

## Confirmed quota

A real `complexSearch?addRecipeNutrition=true&number=1` call cost **1.06
points** and returned `X-API-Quota-Left: 48.94` → the daily allowance is
**50.00 points/day**. Confirmed again across the follow-up calls
(used 2.66 → left 47.34; used 5.46 → left 44.54).

The `food-rec-domain` skill previously said "~150 requests/day" — that was
stale and has been corrected in `SKILL.md`. The live pricing page (50) is
right. **Re-check the account console** if quota behaviour looks off.

| Source | Value | Verdict |
|---|---|---|
| Real response headers, 2026-09-05 | 50 points/day | ✅ authoritative |
| Live pricing page | 50 points/day | ✅ agrees |
| `food-rec-domain` skill (old) / third-party blog | ~150 | ❌ stale / wrong |

## Quota response headers (exact names, verified)

Every response includes:

- `X-API-Quota-Request` — points **this call** cost
- `X-API-Quota-Used` — cumulative points used today
- `X-API-Quota-Left` — points remaining today

They are also listed in `Access-Control-Expose-Headers`. Over quota → HTTP
**402** (per docs). Reset is daily.

## Measured point cost (replaces the earlier estimate)

Decomposed from 3 real calls (`number` = 1, 10, 30), `addRecipeNutrition=true`,
**no** `fillIngredients`:

| number requested | results returned | `X-API-Quota-Request` |
|---|---|---|
| 1  | 1  | **1.06** |
| 10 | 10 | **1.60** |
| 30 | 30 | **2.80** |

Solving `cost = base + per_recipe * n`:

```
base       = 1.000 points
per_recipe = 0.060 points
cost(n)    = 1.000 + 0.060 * n     (exact fit on all 3 points)
```

**This is the number to budget with.** It is higher than the additive figure
implied by the public docs (`1 + 0.01/recipe + 0.025/recipe` ≈ `1 + 0.035n`,
which predicts 2.05 for n=30 vs the measured 2.80). Trust the measurement, not
the doc arithmetic. `fillIngredients=true` adds cost on top — we do not use it
(ingredient names already come back under `nutrition.ingredients[]`).

### Budgeting at 50 points/day

- `number=100` search ≈ **7.0 points** → ~7 such calls/day max.
- Realistically the DAG pulls a batch per run, stops when
  `points_used + next_call_estimate > SPOONACULAR_DAILY_POINT_QUOTA`.
- Quota number lives in config (`SPOONACULAR_DAILY_POINT_QUOTA`, default 50),
  not a hardcoded constant.

## Pitfall found during verification

Python's default `User-Agent` (`Python-urllib/3.x`) is **banned by
Spoonacular's Cloudflare** — you get HTTP 403 with body
`error_code: 1010, error_name: "browser_signature_banned"` and the request
never reaches the API (no quota spent, but also no data). Send a normal
browser-style `User-Agent` header. Recorded in `SKILL.md` pitfalls too.

## Rate limit

Pricing page states **1 request/second** on the free plan. The extractor
sleeps ≥1s between calls.
