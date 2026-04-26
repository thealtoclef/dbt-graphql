# Sec-J — Caching: remaining work

**Status:** Result cache + singleflight shipped. Remaining work is
observability and multi-replica validation. Detailed reference for the
shipped design lives in [`docs/caching.md`](../caching.md).

---

## 1. What's already shipped

For context only — do not redo:

- Result cache + singleflight via cashews (`src/dbt_graphql/cache/`).
- Single-backend cashews URI (`cache.url`); same backend stores entries
  and singleflight locks (`{key}:lock`).
- Flat config: `enabled`, `url`, `ttl`, `lock_safety_timeout`.
- Process-local hit/miss/coalesced counters (`cache.stats`).
- `hash_sql` keying that includes bound parameter values and the dialect
  name — refuses to emit a key for unknown dialects.

Anything not in this list and not in §2 below is **out of scope** — see
§3 for things that were deliberately rejected post-design.

---

## 2. Remaining work

### 2.1 OTel cache metrics

Process-local `CacheStats` is enough for tests but invisible to operators
running a metrics pipeline. Emit OTel counters mirroring the existing
counter fields — same instrument shape as `auth.jwt`:

```
cache.result   counter   {outcome=hit|coalesced|miss}
```

**Where:** add to `src/dbt_graphql/cache/result.py` — increment on the
same code paths that update `stats.result.*`. Keep the process-local
counters; they exist for tests and don't conflict.

**Acceptance:** counters appear on the OTel pipeline; an integration
test asserts the three outcomes increment for the three scenarios
(steady-state hit / cold-burst coalesce / first miss).

### 2.2 Redis multi-replica integration test

The current burst test in `tests/integration/test_result_cache.py` runs
in-process. A multi-replica deployment with a Redis-backed cashews URI
should also coalesce across processes — the lock-key-prefix fix that
ships today (`{key}:lock` not `lock:{key}`) is what makes that work.
Prove it under test.

**Where:** new `tests/integration/test_cache_redis.py`. Use
`pytest-docker` (already a dev dep) to spin up Redis. Spawn two cashews
client instances against the same Redis URL, fire 100 concurrent
identical queries split across both, assert the mock runner ran exactly
once.

**Acceptance:** test passes; reverting the lock-key-prefix in
`result.py` makes it fail (regression guard).

### 2.3 (Optional, deferred) Escape hatches under load

These are **not** to be built speculatively. Implement only after
production telemetry shows the need.

- **203 short-circuit on lock wait.** If a request waits longer than a
  configurable threshold for the singleflight lock, return HTTP 203 with
  `X-Continue-Wait: true` and an empty body. Background work continues
  and populates the cache for subsequent requests. 203 (Non-Authoritative
  Information) is the standard status closest to "placeholder, real
  data pending". 200-with-JSON-error and 202 were both rejected.
- **Connection-pool guard.** A semaphore in front of the SQLAlchemy
  engine pool. Only if singleflight alone proves insufficient to keep
  the pool from exhausting under burst-of-distinct-queries.
- **Per-key concurrency cap.** Bound the number of waiters on a single
  lock so a slow warehouse query can't pile up unbounded.

If/when one of these gets built, split it into its own plan file rather
than reviving this one.

---

## 3. Explicitly rejected (do not revive)

These appeared in earlier design rounds and were dropped. Keeping the
list so future readers don't re-propose them.

- **L1 parsed-doc cache** — parse is ~µs per request, dwarfed by the
  warehouse roundtrip.
- **L2 compiled-plan cache** — same reason; also the cross-tenant
  correctness story (claim-path tracking) was fragile.
- **Multi-backend prefix routing** — collapsed to a single cashews URI.
- **Per-table TTL.** Removed from the config (`1a920d5`); a single
  global TTL is enough.
- **Per-query `@cached(ttl:)` directive.** Belongs alongside the dbt
  model, not in the operator config.
- **Refresh-key invalidation (Cube-style).** Probes create cron-storm
  coordination problems at multi-replica scale.
- **Automatic Persisted Queries (APQ).** Apollo-only extension; the
  Sec-E allow-list covers the security half.
- **Mutation-driven invalidation.** Writes don't flow through this API.
