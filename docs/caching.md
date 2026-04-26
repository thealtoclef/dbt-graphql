# Caching & Burst Protection

A single result cache with singleflight, sitting between the GraphQL HTTP handler and the warehouse. Serves repeat queries without re-executing them, and coalesces concurrent identical queries into a single warehouse roundtrip.

**Entry points:** [`src/dbt_graphql/cache/`](../src/dbt_graphql/cache/) — wired into the API by [`api/app.py`](../src/dbt_graphql/api/app.py) and [`api/resolvers.py`](../src/dbt_graphql/api/resolvers.py).

See [architecture.md](architecture.md) for where this layer sits in the overall pipeline and [configuration.md § cache](configuration.md#cache-optional) for the operator-facing config surface.

---

## Table of contents

- [1. Why only the result cache](#1-why-only-the-result-cache)
- [2. The flow](#2-the-flow)
- [3. Cache-key derivation](#3-cache-key-derivation)
- [4. Multi-tenant correctness — the key claim](#4-multi-tenant-correctness--the-key-claim)
- [5. Per-table TTLs](#5-per-table-ttls)
- [6. Self-protection — what the cache does and does not defend against](#6-self-protection--what-the-cache-does-and-does-not-defend-against)
- [7. Observability](#7-observability)
- [8. Backends](#8-backends)
- [9. Things this layer deliberately doesn't do](#9-things-this-layer-deliberately-doesnt-do)

---

## 1. Why only the result cache

An earlier design proposed three layers — a parsed-document cache (skip GraphQL `parse`), a compiled-plan cache (skip policy eval + SQLAlchemy build), and the result cache (skip the warehouse). We measured: parse is ~µs and compile is ~ms per request, while the warehouse roundtrip is 10–1000 ms. Only the result cache earns its keep.

The compiled-plan cache also had a fragile correctness story — its key had to encode "which JWT claims could the policy possibly read?" to avoid cross-tenant leaks, and `compile_query` recursing into nested-table policies made that hard to bound. Removing it removed the only place the cache could plausibly leak across tenants.

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
async def execute_with_cache(stmt, *, dialect_name, table_names, runner, cfg):
    key = hash_sql(stmt, dialect_name)
    ttl = resolve_ttl(table_names, cfg)

    # Fast path — TTL hit.
    cached = await cache.get(key)
    if cached is not None:
        return cached

    # Slow path — coalesce concurrent misses through a lock.
    async with cache.lock(f"lock:{key}", expire=cfg.lock_safety_timeout_s):
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
| Lock-holder crashes mid-execution | `expire=lock_safety_timeout_s` (default `60`) auto-releases; next caller retries |

The lock's `expire=` is the **safety timeout** — auto-release after a lock-holder crash. It is **not** the result TTL. Both are configurable independently.

---

## 3. Cache-key derivation

[`src/dbt_graphql/cache/keys.py`](../src/dbt_graphql/cache/keys.py)

The key is `sql:` + sha256 of a canonical JSON of `{ "sql": ..., "params": ..., "dialect": ... }`, where:

- `sql` is `str(stmt.compile(dialect=…))` — the exact SQL string we will send to the warehouse, including bind-parameter placeholders.
- `params` is `dict(compiled.params)` — the **bound values** for those placeholders, including row-filter values supplied by the policy engine.
- `dialect` is the dialect name. Same query against Postgres vs MySQL produces different SQL syntax (functions, quoting, JSON aggregation), so they cannot share an entry on a shared Redis backend.

This means the key is determined by what gets sent over the wire, not by anything from above the SQL boundary (no JWT claims, no policy state, no GraphQL AST). Two requests share a cache entry if and only if they would send byte-identical SQL to the warehouse.

---

## 4. Multi-tenant correctness — the key claim

> **Two users with different effective permissions can never share a cache entry, by construction.**

The argument is short. The policy engine renders row-level filters into Jinja templates whose `{{ jwt.claims.x }}` placeholders are turned into SQLAlchemy bind parameters before the statement is compiled. Bound values land in `compiled.params`, which is part of the cache key. Therefore:

- **Tenant A (`cust_id=1`) vs Tenant B (`cust_id=2`)**: the row filter renders with different bind values → different `params` dict → different keys. Cache cannot leak.
- **Two admins with no row filter, totally different JWTs**: the rendered SQL is byte-identical and there are no row-filter binds → same key → they share. This is correct: they would receive the same data either way.
- **Same tenant, same query, repeated**: same SQL, same binds → same key → second request is served from cache. This is the intended hit case.
- **Same tenant, same query, fresh JWT (new `iat`/`exp`)**: as long as the *relevant* claims are unchanged, the rendered SQL is unchanged → same key → cache hits. Token refresh does not invalidate the cache.

What does **not** automatically share an entry, even when the user might intuitively expect it:

- **Different GraphQL field order** (`{ id name }` vs `{ name id }`). The SELECT column order differs → different SQL → different key. This is correct: response field order is part of the GraphQL contract.
- **Different `where` argument values, same shape**. Different binds → different keys. Correct.
- **Different `limit` / `offset`**. SA emits these into the SQL string → different keys. Correct.

There is no separate "tenant key" or claim-tracking machinery. Cross-tenant safety falls out of the SQL itself.

---

## 5. Per-table TTLs

Operators decide freshness per table:

```yaml
cache:
  result:
    default_ttl_s: 60
    per_table_ttl_s:
      orders: 30                # 30 s — rapidly changing
      daily_revenue: 3600       # 1 h — slow-moving aggregate
      user_sessions: 0          # realtime; see below
```

`resolve_ttl()` takes the **strictest** TTL across all tables touched by a query (this includes `default_ttl_s` as one of the candidates). The most-fresh-required table wins — correctness over hit rate.

Special value: **`0` = realtime + minimal coalescing window.** The cache still acquires the singleflight lock so a concurrent burst is coalesced into one warehouse call, but the result is persisted for only 1 second (just long enough for the lock-waiters to wake to a populated cache). After that, the entry expires and the next request re-fetches.

---

## 6. Self-protection — what the cache does and does not defend against

| Threat | Mitigation |
|---|---|
| Burst of identical queries → warehouse stampede | **Lock coalesces to 1 warehouse call.** |
| Burst of distinct queries → warehouse pool exhaustion | **Not mitigated.** SQLAlchemy pool sizing applies; the (N+1)th request blocks on pool checkout. Acceptable: clients see latency, server stays up. |
| Slow client + long warehouse query → connection held | Asyncio handles thousands of idle waiters cheaply. Client timeout is the client's responsibility. |
| Lock-holder crash mid-query | `lock_safety_timeout_s` (default `60`) auto-releases the lock. |
| Memory growth from queued waiters | Bounded by the HTTP server's max in-flight requests (Granian's worker concurrency). |
| Cache poisoning via crafted JWT | Key embeds bound row-filter values directly (§ 4). |

**Tuning the lock safety timeout.** Set it slightly above the slowest plausible warehouse query you expect. Too low → a legitimately slow query times out the lock and a second caller fires a duplicate execution. Too high → recovery from a crashed lock-holder is delayed. The default `60` works for typical analytics queries; bump it for heavy aggregations on TB tables.

What this layer is **not**:

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

## 8. Backends

Backends are configured by a list of cashews URIs with optional prefix routing:

```yaml
cache:
  backends:
    - url: "mem://?size=10000"        # default — single-replica
    # - url: "redis://localhost:6379/0"  # multi-replica: shares cache + locks across pods
```

For multi-replica deployments, a Redis backend lets all replicas share both the result cache and the singleflight lock — so coalescing crosses replicas, not just per-process.

The backend abstraction is provided by [cashews](https://github.com/Krukov/cashews) — see its docs for the URI grammar (TLS, sentinel, cluster, etc.).

---

## 9. Things this layer deliberately doesn't do

- **No parse cache, no compiled-plan cache.** Both savings are dwarfed by the warehouse roundtrip; see § 1.
- **No refresh-key invalidation (Cube-style).** Probes hit the warehouse on their own schedule and create coordination problems at scale. Wall-clock TTL is sufficient for a dbt-backed analytics API where data updates on dbt's schedule.
- **No mutation-driven invalidation.** Writes don't flow through dbt-graphql; they happen via dbt jobs out-of-band. Nothing to invalidate on.
- **No Automatic Persisted Queries (APQ).** Not in the GraphQL spec; an Apollo extension.
- **No per-query `@cached(ttl:)` directive override.** Per-table TTL covers 95% of cases. Add later if operators ask.
