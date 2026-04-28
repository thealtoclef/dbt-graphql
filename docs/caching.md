# Caching & Burst Protection

A result cache with singleflight, sitting between the GraphQL HTTP handler and the warehouse. Serves repeat queries without re-executing them, and coalesces concurrent identical queries into a single warehouse roundtrip.

**Entry points:** [`src/dbt_graphql/cache/`](../src/dbt_graphql/cache/) — wired into the GraphQL sub-app by [`graphql/app.py`](../src/dbt_graphql/graphql/app.py) and [`graphql/resolvers.py`](../src/dbt_graphql/graphql/resolvers.py).

See [architecture.md](architecture.md) for where the cache sits in the overall pipeline and [configuration.md § cache](configuration.md#cache-optional) for the operator-facing config surface.

---

## Table of contents

- [1. Why only the result cache](#1-why-only-the-result-cache)
- [2. The flow](#2-the-flow)
- [3. Cache-key derivation](#3-cache-key-derivation)
- [4. Multi-tenant correctness — the key claim](#4-multi-tenant-correctness--the-key-claim)
- [5. TTL](#5-ttl)
- [6. Self-protection — what the cache does and does not defend against](#6-self-protection--what-the-cache-does-and-does-not-defend-against)
- [7. Observability](#7-observability)
- [8. Backend](#8-backend)
- [9. Things the cache deliberately doesn't do](#9-things-the-cache-deliberately-doesnt-do)

---

## 1. Why only the result cache

An earlier design proposed two more caches in front of this one — a parsed-document cache (skip GraphQL `parse`) and a compiled-plan cache (skip policy eval + SQLAlchemy build). We sized them at the design target of **1000 RPS** and dropped both:

| What | Cost saved per hit | At 1000 RPS, ~100% hit rate | Verdict |
|---|---|---|---|
| Parse cache | ~30–100 µs | ~0.05 cores of CPU | Noise — lost in measurement error. |
| Compiled-plan cache | ~0.5–3 ms | ~1.5 cores of CPU | Real — but not on the bottleneck path. |
| **Result cache** | **10–1000 ms (warehouse roundtrip)** | dominates | The only one that matters. |

At 1000 RPS to an OLAP warehouse, you exhaust connection-pool / warehouse concurrency long before the app is CPU-bound. The result cache saves the warehouse roundtrip — that's where 1000 RPS becomes sustainable. The compiled-plan cache would save CPU you weren't out of, and adding a replica costs less than maintaining the cache code.

The compiled-plan cache also had a fragile correctness story — its key had to encode "which JWT claims could the policy possibly read?" to avoid cross-tenant leaks, and `compile_query` recursing into nested-table policies made that hard to bound. Dropping it removed the only place the cache could plausibly leak across tenants.

```
HTTP request (POST /graphql)
   │
   ▼
[Auth middleware → JWT payload]
   │
   ▼
[Ariadne parse + validate]            (~µs, no cache)
   │
   ▼
[Resolver: compile_query → SQL]       (~ms, no cache)
   │
   ▼
┌─────────────────────────────────────────────────────────────┐
│  Result cache + singleflight   cache/result.py              │
│  - GET cache by (rendered SQL + bound params + dialect)     │
│  - On miss: acquire lock, re-check, run warehouse, SET      │
│    with TTL, release lock                                   │
│  - Concurrent misses on the same key wait on the lock,      │
│    wake up to a populated cache, return without firing      │
│    the warehouse a second time.                             │
└─────────────────────────────────────────────────────────────┘
   │
   ▼
Warehouse (only on miss while holding the lock)
```

---

## 2. The flow

The execution path is twelve lines:

```python
async def execute_with_cache(stmt, *, dialect_name, runner, cfg):
    key = hash_sql(stmt, dialect_name)
    ttl = cfg.ttl

    # Fast path — TTL hit.
    cached = await cache.get(key)
    if cached is not None:
        return cached

    # Slow path — coalesce concurrent misses through a lock.
    async with cache.lock(f"{key}:lock", expire=cfg.lock_safety_timeout):
        cached = await cache.get(key)         # re-check inside the lock
        if cached is not None:
            return cached
        result = await runner(stmt)
        await cache.set(key, result, expire=(1 if ttl == 0 else ttl))
        return result
```

Behavior matrix:

| Scenario | Outcome |
|---|---|
| Steady state, query within TTL | TTL hit — sub-ms, no warehouse |
| Cold start, 100 concurrent identical | Lock coalesces — 1 warehouse hit, 99 wake to populated cache |
| TTL boundary, 100 concurrent identical | Lock coalesces — 1 warehouse hit |
| Distinct queries, different keys | Independent paths, no contention |
| Lock-holder crashes mid-execution | `expire=lock_safety_timeout` (default `10`) auto-releases; next caller retries |

The lock's `expire=` is the **safety timeout** — auto-release after a lock-holder crash. It is **not** the result TTL. Both are configurable independently.

---

## 3. Cache-key derivation

[`src/dbt_graphql/cache/keys.py`](../src/dbt_graphql/cache/keys.py)

The key is `sql:` + sha256 of a canonical JSON of `{ "sql": ..., "params": ..., "dialect": ... }`, where:

- `sql` is `str(stmt.compile(dialect=…))` — the exact SQL string we will send to the warehouse, including bind-parameter placeholders.
- `params` is `dict(compiled.params)` — the **bound values** for those placeholders, including row-filter values supplied by the policy engine.
- `dialect` is the dialect name. Same query against Postgres vs MySQL produces different SQL syntax (functions, quoting, JSON aggregation), so they cannot share an entry on a shared Redis backend.

This means the key is determined by what gets sent over the wire, not by anything from above the SQL boundary (no JWT claims, no policy state, no GraphQL AST). Two requests share a cache entry if and only if they would send byte-identical SQL to the warehouse.

If `dialect_name` is one SQLAlchemy cannot load, `hash_sql` raises `ValueError` rather than falling back. An earlier version fell back to `str(stmt)` for unknown dialects — which omits bound parameter values from the key, silently letting two queries with different binds collide. Refusing to emit the key was the safer fix; today's two adapters (`postgresql`, `mysql`) both load cleanly, and any future adapter SA can register works automatically.

---

## 4. Multi-tenant correctness — the key claim

> **Two users with different effective permissions can never share a cache entry, by construction.**

The argument is short. The policy engine compiles `row_filter` DSL trees to SQLAlchemy `ColumnElement` clauses; JWT claim values bind as named parameters before the statement is compiled. Bound values land in `compiled.params`, which is part of the cache key. Therefore:

- **Tenant A (`cust_id=1`) vs Tenant B (`cust_id=2`)**: the row filter compiles with different bind values → different `params` dict → different keys. Cache cannot leak.
- **Two admins with no row filter, totally different JWTs**: the compiled SQL is byte-identical and there are no row-filter binds → same key → they share. This is correct: they would receive the same data either way.
- **Same tenant, same query, repeated**: same SQL, same binds → same key → second request is served from cache. This is the intended hit case.
- **Same tenant, same query, fresh JWT (new `iat`/`exp`)**: as long as the *relevant* claims are unchanged, the compiled SQL is unchanged → same key → cache hits. Token refresh does not invalidate the cache.

What does **not** automatically share an entry, even when the user might intuitively expect it:

- **Different GraphQL field order** (`{ id name }` vs `{ name id }`). The SELECT column order differs → different SQL → different key. This is correct: response field order is part of the GraphQL contract.
- **Different `where` argument values, same shape**. Different binds → different keys. Correct.
- **Different `limit` / `offset`**. SA emits these into the SQL string → different keys. Correct.

There is no separate "tenant key" or claim-tracking machinery. Cross-tenant safety falls out of the SQL itself.

---

## 5. TTL

A single global freshness window:

```yaml
cache:
  ttl: 60   # seconds
```

Per-table TTLs were considered and dropped: scattering freshness rules across the operator config is fragile and disconnects the freshness decision from the dbt model that defines the table. If per-table freshness becomes necessary, it should be expressed alongside the model definition — not in a parallel YAML block in the API config.

Special value: **`ttl: 0` = realtime + minimal coalescing window.** The cache still acquires the singleflight lock so a concurrent burst is coalesced into one warehouse call, but the result is persisted for only 1 second (just long enough for the lock-waiters to wake to a populated cache). After that, the entry expires and the next request re-fetches.

---

## 6. Self-protection — what the cache does and does not defend against

| Threat | Mitigation |
|---|---|
| Burst of identical queries → warehouse stampede | **Lock coalesces to 1 warehouse call.** |
| Burst of distinct queries → warehouse pool exhaustion | **Not mitigated.** SQLAlchemy pool sizing applies; the (N+1)th request blocks on pool checkout. Acceptable: clients see latency, server stays up. |
| Slow client + long warehouse query → connection held | Asyncio handles thousands of idle waiters cheaply. Client timeout is the client's responsibility. |
| Lock-holder crash mid-query | `lock_safety_timeout` (default `10`) auto-releases the lock. |
| Memory growth from queued waiters | Bounded by the HTTP server's max in-flight requests (uvicorn's worker / event-loop concurrency). |
| Cache poisoning via crafted JWT | Key embeds bound row-filter values directly (§ 4). |

**Tuning the lock safety timeout.** Two constraints:

- **Above your p99 warehouse query time.** When a *legitimately slow* query exceeds the timeout, Redis silently deletes the lock and a waiting caller becomes the new holder — running the same query a second time against the warehouse. Both responses still return 200; the cost is one wasted warehouse roundtrip per slow query. So set the timeout above your slowest expected legitimate query.
- **Below your upstream LB idle timeout, with a few seconds of headroom.** A *crashed* holder (SIGKILL, OOM) leaves a stuck lock until the TTL fires; after that, a waiter takes over and runs the query. If the TTL is ≥ LB timeout, the LB resets every waiter before recovery completes.

Default `10` targets fast warehouses (ClickHouse, Doris, Postgres on dbt-modeled tables, p99 ≤ 5s) behind a typical 30s LB. Bump to 25–60 for slow OLAP aggregations; bump the LB too if you go above its idle timeout.

What the cache is **not**:

- Not a query-cost limiter — it does not reject expensive queries up-front.
- Not a per-IP rate limiter — that belongs at the load balancer.
- Not a connection-pool guard — wrap the SQLAlchemy engine pool yourself if needed.

---

## 7. Observability

Process-local counters live at [`cache/stats.py`](../src/dbt_graphql/cache/stats.py):

```python
from dbt_graphql.cache import stats

stats.result.hit          # TTL hit (steady state)
stats.result.coalesced    # woke from lock to populated cache (singleflight win)
stats.result.miss         # ran the warehouse
```

The split between `hit` and `coalesced` matters operationally: a high `coalesced:miss` ratio means singleflight is doing real work; a high `hit:miss` ratio means TTLs are well-tuned.

`stats.reset()` clears all counters — used by the test suite between runs.

---

## 8. Backend

A single cashews URI:

```yaml
cache:
  url: "mem://?size=10000"             # default — single-replica
  # url: "redis://localhost:6379/0"    # multi-replica: shares cache + locks across pods
```

The lock key uses the same prefix as the cache value (`{key}:lock`, not `lock:{key}`), so a Redis URI moves both the cache entries and the singleflight locks to Redis — coalescing crosses replicas, not just per-process. See [cashews](https://github.com/Krukov/cashews) for the URI grammar (TLS, sentinel, cluster, etc.).

---

## 9. Things the cache deliberately doesn't do

- **No parse cache, no compiled-plan cache.** Both savings are dwarfed by the warehouse roundtrip; see § 1.
- **No refresh-key invalidation (Cube-style).** Probes hit the warehouse on their own schedule and create coordination problems at scale. Wall-clock TTL is sufficient for a dbt-backed analytics API where data updates on dbt's schedule.
- **No mutation-driven invalidation.** Writes don't flow through dbt-graphql; they happen via dbt jobs out-of-band. Nothing to invalidate on.
- **No Automatic Persisted Queries (APQ).** Not in the GraphQL spec; an Apollo extension.
- **No per-query `@cached(ttl:)` directive override.** A single global TTL covers the common case. Per-table freshness, if ever needed, belongs alongside the dbt model definition — not as a parallel block in the operator config.
