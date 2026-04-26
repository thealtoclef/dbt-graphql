# Caching & Burst Protection

Three-layer cache that sits between the GraphQL HTTP handler and the warehouse. Serves repeat queries without re-parsing, re-compiling, or re-executing them, and coalesces concurrent identical queries into a single warehouse roundtrip.

**Entry points:** [`src/dbt_graphql/cache/`](../src/dbt_graphql/cache/) — wired into the API by [`api/app.py`](../src/dbt_graphql/api/app.py) and [`api/resolvers.py`](../src/dbt_graphql/api/resolvers.py).

See [architecture.md](architecture.md) for where this layer sits in the overall pipeline, [configuration.md § cache](configuration.md#cache-optional) for the operator-facing config surface, and [`docs/plans/sec-j-caching.md`](plans/sec-j-caching.md) for the original design rationale (incl. alternatives that were rejected).

---

## Table of contents

- [1. Why three layers](#1-why-three-layers)
- [2. The L3 flow (singleflight)](#2-the-l3-flow-singleflight)
- [3. Cache-key derivation](#3-cache-key-derivation)
- [4. Multi-tenant correctness](#4-multi-tenant-correctness)
- [5. Per-table TTLs](#5-per-table-ttls)
- [6. Self-protection — what the cache does and does not defend against](#6-self-protection--what-the-cache-does-and-does-not-defend-against)
- [7. Observability](#7-observability)
- [8. Backends](#8-backends)
- [9. Things this layer deliberately doesn't do](#9-things-this-layer-deliberately-doesnt-do)

---

## 1. Why three layers

```
HTTP request (POST /graphql)
   │
   ▼
[Auth middleware → JWT payload]
   │
   ▼
┌─────────────────────────────────────────────────────────────┐
│ L1  Parsed-doc cache         cache/parsed_doc.py            │
│     parse(query_string) → DocumentNode                      │
│     Skips redundant CPU when same query string repeats.     │
└─────────────────────────────────────────────────────────────┘
   │
   ▼
┌─────────────────────────────────────────────────────────────┐
│ L2  Compiled-plan cache      cache/compiled_plan.py         │
│     GraphQL AST → SQLAlchemy Select + policy-resolved masks │
│     and row-filter bind-params.                             │
│     Key includes the JWT signature for tenant isolation.    │
└─────────────────────────────────────────────────────────────┘
   │
   ▼
┌─────────────────────────────────────────────────────────────┐
│ L3  Result cache + singleflight  cache/result.py            │
│     - GET cache by (rendered SQL + bound params)            │
│     - On miss: acquire lock, re-check, run warehouse, SET   │
│       with TTL, release lock                                │
│     - Concurrent misses on the same key wait on the lock,   │
│       wake up to a populated cache, return without firing   │
│       the warehouse a second time.                          │
└─────────────────────────────────────────────────────────────┘
   │
   ▼
Warehouse (only if all three layers missed AND we hold the lock)
```

Each layer skips a different cost:

| Layer | What it skips on hit | Storage |
|---|---|---|
| L1 | parse | in-process LRU (sync; called from Ariadne's `query_parser` hook) |
| L2 | policy eval + SQL build | cashews in-memory backend (cached `Select` objects don't pickle reliably) |
| L3 | warehouse roundtrip | cashews — defaults to in-memory; a Redis-prefixed backend is opt-in for multi-replica deployments |

Singleflight is **not** a separate layer — it's combined with L3 in a single GET → lock → re-check → SET flow. They share the same key namespace and the same backend, so coalescing automatically works across replicas when L3 is on Redis.

---

## 2. The L3 flow (singleflight)

L3 is the only layer with a TTL and the only one that defends against bursts. Its execution path is twelve lines:

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

The lock's `expire=` is the **safety timeout** — auto-release after a lock-holder crash. It is **not** the result TTL. Both are configurable independently. See [§ 6](#6-self-protection--what-the-cache-does-and-does-not-defend-against) for what each tunable defends against.

---

## 3. Cache-key derivation

[`src/dbt_graphql/cache/keys.py`](../src/dbt_graphql/cache/keys.py)

| Layer | Key composition | Why |
|---|---|---|
| L1 | `parse:` + sha256(canonicalized query string) | `canonicalize_doc()` parses + `print_ast`s, so two queries that differ only in whitespace land on the same key. Field-order differences do **not** collapse — GraphQL field order affects response shape. |
| L2 | `plan:{table}:{doc_subtree_hash}:{args_sig}:{jwt_sig}` | `args_sig` covers `where` / `limit` / `offset` / `dialect`. `jwt_sig` is sha256 of the canonicalized JWT payload (sorted-keys JSON), recursively. |
| L3 | `sql:` + sha256(rendered SQL + bound params + dialect) | The compiled SQL string already incorporates everything from L2 (column allow-list, mask expressions, row-filter values), so policy isolation is structurally automatic at L3 without re-deriving the JWT signature. |

Dialect is in every key. The same query against Postgres and MySQL produces different SQL (functions, quoting, JSON aggregation), so a Postgres replica must never serve a MySQL replica's L3 entry from a shared Redis.

---

## 4. Multi-tenant correctness

Two users with different JWTs must never see each other's cached data — even when their queries have the same shape.

The L2 cache key includes the **full JWT signature** (sha256 of the canonicalized payload). This is stricter than the design plan's original "claim-paths-referenced" idea: the recursion in `compile_query` into nested-table policies means we cannot enumerate all the claim paths the policy will read up-front, so we conservatively key on the entire JWT. Two structurally-identical JWT payloads still share an L2 entry — that's the cross-session sharing benefit.

The L3 cache key is structurally tenant-isolated by construction: row-filter bind values appear in the rendered SQL's bind-params, so two users with different `cust_id` claims produce different L3 keys without us having to do anything explicit.

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

Special value: **`0` = realtime + minimal coalescing window.** L3 still acquires the singleflight lock so a concurrent burst is coalesced into one warehouse call, but the result is persisted for only 1 second (just long enough for the lock-waiters to wake to a populated cache). After that, the entry expires and the next request re-fetches.

---

## 6. Self-protection — what the cache does and does not defend against

| Threat | Mitigation |
|---|---|
| Burst of identical queries → warehouse stampede | **L3 lock coalesces to 1 warehouse call.** |
| Burst of distinct queries → warehouse pool exhaustion | **Not mitigated.** SQLAlchemy pool sizing applies; the (N+1)th request blocks on pool checkout. Acceptable: clients see latency, server stays up. |
| Slow client + long warehouse query → connection held | Asyncio handles thousands of idle waiters cheaply. Client timeout is the client's responsibility; Uvicorn / Granian have keepalive limits as backstop. |
| Lock-holder crash mid-query | `lock_safety_timeout_s` (default `60`) auto-releases the lock. |
| Memory growth from queued waiters | Bounded by the HTTP server's max in-flight requests (Granian's worker concurrency). |
| Cache poisoning via crafted JWT | L2 key uses the full JWT signature (§ 4); L3 key embeds bound row-filter values directly. |

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

stats.parsed_doc.hit
stats.parsed_doc.miss

stats.compiled_plan.hit
stats.compiled_plan.miss

stats.result.hit          # TTL hit (steady state)
stats.result.coalesced    # woke from lock to populated cache (singleflight win)
stats.result.miss         # ran the warehouse
```

Hit / miss / coalesce are tracked per-layer. The L3 split between `hit` and `coalesced` matters operationally: a high `coalesced:miss` ratio means singleflight is doing real work; a high `hit:miss` ratio means TTLs are well-tuned.

`stats.reset()` clears all counters — used by the test suite between runs.

---

## 8. Backends

Backends are configured by a list of cashews URIs with optional prefix routing:

```yaml
cache:
  backends:
    - url: "mem://?size=10000"        # default catch-all
    - url: "redis://localhost:6379/0" # opt-in for multi-replica
      prefix: "sql:"                  # only L3 entries go to Redis
```

Prefix routing maps any cache key starting with `prefix` to that backend. Common patterns:

- **Single-replica deployments:** one `mem://` backend covers everything.
- **Multi-replica deployments:** `mem://` for L1/L2 (parse + compile, always per-process), `redis://` with `prefix: "sql:"` for L3 (so coalescing crosses replicas via Redis's BLPOP-based locks).

L1 and L2 are intentionally **always in-memory**. L1 is wired into Ariadne's sync `query_parser` hook (no `await` available); L2 stores SQLAlchemy `Select` objects which don't pickle reliably across cashews backends. L3 is the only layer that benefits from a shared backend.

The backend abstraction is provided by [cashews](https://github.com/Krukov/cashews) — see its docs for the URI grammar (TLS, sentinel, cluster, etc.).

---

## 9. Things this layer deliberately doesn't do

- **No refresh-key invalidation (Cube-style).** Probes hit the warehouse on their own schedule and create coordination problems at scale. Wall-clock TTL is sufficient for a dbt-backed analytics API where data updates on dbt's schedule.
- **No Continue Wait long-polling (Cube-style).** Singleflight already coalesces bursts. Holding HTTP connections for the duration is fine in asyncio. The `203 Non-Authoritative Information` escape hatch is documented in the design plan but not implemented.
- **No mutation-driven invalidation.** Writes don't flow through dbt-graphql; they happen via dbt jobs out-of-band. Nothing to invalidate on.
- **No Automatic Persisted Queries (APQ).** Not in the GraphQL spec; an Apollo extension. The design plan defers to a query allow-list (Sec-E, planned) for the security half.
- **No L1/L2 in Redis.** See § 8.
- **No per-query `@cached(ttl:)` directive override.** Per-table TTL covers 95% of cases. Add later if operators ask.
