# Sec-J — Caching & Burst Protection

**Status:** Planned
**Owner:** TBD
**Depends on:** none (independent of Sec-A through Sec-L)
**Blocks:** none

---

## 1. Goals

1. **Self-protect the server.** A burst of identical concurrent requests must
   not multiply into a burst of warehouse queries, connection-pool waits, or
   redundant CPU spent on parse/validate/policy-eval.
2. **Cut response latency for repeat queries.** Same query within a TTL window
   returns the cached answer without touching the warehouse.
3. **Keep correctness under multi-tenancy.** Two users with different JWTs
   never see each other's cached data; cache hit rate stays high when only
   policy-irrelevant claims differ between them.
4. **Stay simple operationally.** No probes, no tickers, no cron, no
   refresh-key SQL fired at the warehouse. TTL is wall-clock; operator picks
   the number; behavior is deterministic.
5. **Pluggable backend.** In-memory default; Redis for multi-replica deploys.
   Same code path, swap via config.

## 2. Non-goals

- **Refresh-key invalidation (Cube-style).** Probes hit the warehouse on
  their own schedule and create coordination problems at scale (cron storms
  / tick drift between replicas). Wall-clock TTL is sufficient for a
  dbt-backed analytics API where data updates on dbt's schedule.
- **Automatic Persisted Queries (APQ).** Not in the GraphQL spec; an Apollo
  extension. The Sec-E allow-list (planned separately) covers the
  security half of APQ without requiring client cooperation.
- **Continue Wait long-polling (Cube-style).** With singleflight in place,
  bursts coalesce into a single in-flight request that all callers `await`.
  Holding HTTP connections for the duration is fine in asyncio. We do **not**
  ship Continue Wait in MVP. If we ever observe real harm (connection
  exhaustion at scale), the opt-in escape hatch is documented in §10 below.
- **Mutation-driven invalidation.** Writes do not flow through dbt-graphql;
  they happen via dbt jobs out-of-band. There is nothing to invalidate on.
- **Distributed cache consistency guarantees.** Best-effort: a Redis hiccup
  may briefly admit duplicate warehouse queries. That is acceptable.

## 3. Architecture

### 3.1 Layers at a glance

```
HTTP request (POST /graphql)
   │
   ▼
[Auth/policy middleware — out of scope for this plan]
   │
   ▼
┌─────────────────────────────────────────────────────────────┐
│ L1  Parsed-doc cache                                        │
│     parse(query_string) + validate(ast, schema)             │
│     Skips redundant CPU when same query string repeats.     │
└─────────────────────────────────────────────────────────────┘
   │
   ▼
┌─────────────────────────────────────────────────────────────┐
│ L2  Compiled-plan cache                                     │
│     GraphQL AST → SQLAlchemy stmt + policy-resolved masks   │
│     and row-filter bind-params.                             │
│     Key includes role + the JWT claims the policy actually  │
│     touched (not all claims).                               │
└─────────────────────────────────────────────────────────────┘
   │
   ▼
┌─────────────────────────────────────────────────────────────┐
│ L3  Result cache + singleflight (one mechanism)             │
│     - GET cache by (rendered SQL + bound params)            │
│     - On miss: acquire lock, re-check, run warehouse, SET   │
│       with TTL, release lock                                │
│     - Concurrent misses on the same key wait on the lock,   │
│       wake up to a populated cache, return without firing   │
│       the warehouse a second time.                          │
└─────────────────────────────────────────────────────────────┘
   │
   ▼
Warehouse (only if all three layers missed and we hold the lock)
```

### 3.2 Why this exact shape

Five-layer designs (parsed-doc, compiled-plan, result, APQ, singleflight)
exist in the literature and in mature systems (Hasura, Cube, Apollo Router).
We collapse to **three** for the following reasons:

- **APQ dropped** — not standard, requires client cooperation, and its
  security half is covered by Sec-E.
- **Singleflight not a separate layer** — by itself it would be a dict of
  in-flight futures with no persistence; combined with the result cache
  it becomes a "GET → lock → re-check → SET" flow that uses the same
  storage and the same key. Treating them as one layer eliminates a
  redundant key namespace and a separate config block.

The split between L1, L2, L3 is real and worth keeping:

| Layer | What it skips on hit | Storage characteristics |
|---|---|---|
| L1 | parse + validate | tiny entries (~few KB), bounded set, in-process is fine |
| L2 | policy eval + SQL build | medium entries, key cardinality grows with `roles × claim-tuples`, in-process is fine |
| L3 | warehouse roundtrip | rows can be large (KB–MB), benefits from Redis in multi-replica |

L1 and L2 are pure-functional and never need TTL — they're invalidated by
process restart (schema change → redeploy) and by policy hot-reload (Sec-K
generation counter, when that ships). They are intentionally **always
in-memory**, never Redis: serializing/deserializing an AST or a SQLAlchemy
stmt across the wire would cost more than the cache miss it prevents.

L3 is where the warehouse-saving and burst-protection happen. It is the
only layer that benefits from a shared backend across replicas, and the only
layer with a TTL.

### 3.3 The L3 flow in detail

Pseudocode (final shape; ~12 lines):

```python
async def execute_with_cache(sql: str, params: dict, runner) -> Result:
    key = f"sql:{hash_sql(sql, params)}"

    # Fast path — TTL hit. Steady state.
    if (cached := await cache.get(key)) is not None:
        return cached

    # Slow path — coalesce concurrent misses through a lock.
    async with cache.lock(key, expire=LOCK_SAFETY_TIMEOUT):
        # Re-check inside the lock: another caller may have populated
        # while we were waiting.
        if (cached := await cache.get(key)) is not None:
            return cached
        result = await runner(sql, params)
        await cache.set(key, result, expire=ttl_for(sql))
        return result
```

Behavior matrix:

| Scenario | Outcome |
|---|---|
| Steady state, query within TTL | TTL hit — sub-ms, no warehouse |
| Cold start, 100 concurrent identical | Lock coalesces — 1 warehouse hit, 99 wake to populated cache |
| TTL boundary, 100 concurrent identical | Lock coalesces — 1 warehouse hit |
| Distinct queries, different keys | Independent paths, no contention |
| Lock-holder crashes mid-execution | `expire=LOCK_SAFETY_TIMEOUT` (e.g., 60s) auto-releases; next caller retries |

The lock's `expire=` is the **safety timeout** — if a lock-holder crashes
mid-query, the lock auto-releases after this many seconds and the next
caller retries. It is **not** the result TTL. Operator-configurable via
`cache.result.lock_safety_timeout_s` (default `60` in `defaults.py`).
The default sits slightly above the slowest plausible warehouse query;
operators with longer-running queries (heavy aggregations, full-scans on
TB tables) bump this up. Setting it too low risks duplicate execution if
the warehouse query exceeds the timeout while still legitimately running;
setting it too high delays recovery after a crash. Both are tunable
operational concerns — hence configurable.

### 3.4 Cache key composition (the multi-tenant correctness piece)

For L2 and L3 the cache key must isolate tenants without exploding cardinality:

```python
def plan_cache_key(doc_hash: str, resolved_policy, jwt_payload) -> str:
    # Only claims the matching policies actually referenced go into the key.
    claim_sig = tuple(
        (path, _read_path(jwt_payload, path))
        for path in sorted(resolved_policy.claim_paths_referenced)
    )
    return f"plan:{doc_hash}:{resolved_policy.role_signature}:{stable_hash(claim_sig)}"
```

This requires `PolicyEngine.resolve()` to track which JWT claim paths it
read during evaluation. Implementation: wrap the `JWTPayload` dot-access
object with a tracker that records every attribute access, return the set
along with the `ResolvedPolicy`.

For L3, the key is the rendered SQL + bound parameter values, which already
incorporates everything from L2 (column allow-list, mask expressions, row
filter values) — so policy isolation is structurally automatic at L3
without re-deriving the claim signature.

## 4. Library choice

**[cashews](https://github.com/Krukov/cashews)** — single dependency for
all three layers and the singleflight lock.

| Requirement | cashews provides |
|---|---|
| In-memory + Redis backends, swap by config | `cache.setup("mem://...")`, `cache.setup("redis://...")` — URI-driven |
| Per-prefix backend routing | `cache.setup(url, prefix="plan:")` — different prefixes route to different backends |
| Singleflight / coalescing primitive | `async with cache.lock(key, expire=...)` — works on whichever backend the prefix routes to (so multi-replica via Redis works automatically) |
| Sane decorator API for L1/L2 | `@cache(ttl=..., prefix="parse:")` |
| TTL on `set` | `cache.set(key, value, expire=ttl)` |
| Active maintenance, MIT, ~3k stars | Yes |

### Alternatives considered

- **aiocache** — older, no built-in stampede protection, would require us
  to write the lock primitive ourselves.
- **cacheme** — heavier, more opinionated, chain-storage and
  multi-serializer features we don't need.
- **`singleflight` (PyPI port of Go groupcache)** — last release ~2020,
  in-process only (no Redis), would force a separate dep just for L5.
  Cashews `cache.lock` covers this with the same backend story.

## 5. Configuration

### 5.1 Pydantic shape (lives under `AppConfig.cache`)

```python
class CacheBackendConfig(BaseModel):
    url: str                           # cashews URI, e.g. "mem://?size=10000"
    prefix: str = ""                   # routes keys with this prefix to this backend
    enabled: bool = True

class L1Config(BaseModel):             # parsed-doc
    enabled: bool = True
    max_size: int = 1000               # LRU capacity in entries

class L2Config(BaseModel):             # compiled-plan
    enabled: bool = True
    max_size: int = 1000

class L3Config(BaseModel):             # result + singleflight
    enabled: bool = True
    default_ttl_s: int = 60            # global default; per-table override below
    per_table_ttl_s: dict[str, int] = {}   # 0 = disabled (always fetch fresh)
    lock_safety_timeout_s: int = 60    # internal; rarely tuned

class CacheConfig(BaseModel):
    backends: list[CacheBackendConfig] = [
        CacheBackendConfig(url="mem://?size=10000"),
    ]
    parsed_doc: L1Config = L1Config()
    compiled_plan: L2Config = L2Config()
    result: L3Config = L3Config()
```

### 5.2 Example `config.yml`

```yaml
cache:
  backends:
    - url: "mem://?size=10000"        # default catch-all
    # - url: "redis://localhost:6379/0"
    #   prefix: "sql:"                # only result cache to Redis in prod
    #   enabled: true

  parsed_doc:    { enabled: true,  max_size: 1000 }
  compiled_plan: { enabled: true,  max_size: 1000 }
  result:
    enabled: true
    default_ttl_s: 60
    per_table_ttl_s:
      orders: 30
      daily_revenue: 3600
      user_sessions: 0                # never cache
    lock_safety_timeout_s: 60
```

### 5.3 Configuration philosophy

- **L1 and L2** expose `max_size` only. No TTL, no backend choice. Always
  in-memory using cashews' `mem://?size=N` backend, which is a bounded
  LRU under the hood (cashews uses an OrderedDict-based LRU implementation;
  evicts least-recently-used on overflow — same eviction semantics as
  `functools.lru_cache`, just async-aware). The size limit is **mandatory**;
  there is no "unbounded" mode — that would be a memory-leak footgun in a
  long-running server. Defaults (1000 each) handle every real-world schema;
  the knob exists for the rare massive multi-tenant case where compiled-plan
  cardinality (`docs × roles × claim-tuples`) exceeds 1000.
- **Singleflight** lock backend is L3's backend (no separate config). The
  one tunable — the lock safety timeout — lives under
  `cache.result.lock_safety_timeout_s` because it's tightly coupled to
  warehouse-query duration, not to cache mechanics.
- **L3** exposes `default_ttl_s`, `per_table_ttl_s`, and
  `lock_safety_timeout_s`. Operators decide freshness per table and
  warehouse-timeout headroom; no probing, no auto-detection.

### 5.4 Why cashews' built-in LRU instead of `functools.lru_cache` or a custom LRU

We considered three options for L1/L2:

| Option | Verdict |
|---|---|
| `functools.lru_cache` | Sync-only; would require wrapping `await` calls in odd ways. Rejected. |
| `async-lru` (third-party) | Adds a second cache dependency for the same job cashews already does. Rejected. |
| `cachetools.LRUCache` | Sync, would need our own async wrapper + locking. Rejected. |
| **cashews `mem://?size=N`** | Same library as L3, async-native, LRU eviction, prefix-routed config. **Selected.** |

Single dependency, single mental model. Eviction policy is LRU.

### 5.5 Centralized defaults

Per project convention, all default values live in `src/dbt_graphql/defaults.py`
and are referenced by `cache/config.py`. New entries:

```python
# defaults.py additions
CACHE_BACKEND_DEFAULT_URL: Final[str] = "mem://?size=10000"
CACHE_PARSED_DOC_MAX_SIZE: Final[int] = 1000
CACHE_COMPILED_PLAN_MAX_SIZE: Final[int] = 1000
CACHE_RESULT_DEFAULT_TTL_S: Final[int] = 60
CACHE_RESULT_LOCK_SAFETY_TIMEOUT_S: Final[int] = 60
```

`config.example.yml` documents all of them with the same values commented
out, matching the existing pattern from Phase 5.

## 6. File layout

```
src/dbt_graphql/cache/
  __init__.py           # public: setup_cache(), close_cache(), get_cache()
  config.py             # CacheConfig + sub-models (§5.1)
  setup.py              # reads config, calls cashews.cache.setup() per backend
  keys.py               # canonicalize_doc(), hash_sql(), plan_cache_key()
  parsed_doc.py         # L1: cached parse+validate wrapper around graphql-core
  compiled_plan.py      # L2: cached compile wrapper; needs claim-tracker hook
  result.py             # L3: execute_with_cache() — the GET/lock/SET flow
  metrics.py            # OTel hit/miss/coalesced counters per layer
```

Integration points (no new files, only edits):

- `src/dbt_graphql/config.py` — add `cache: CacheConfig` to `AppConfig`
- `src/dbt_graphql/api/app.py` — call `setup_cache(config.cache)` in lifespan
- `src/dbt_graphql/api/resolvers.py` — wrap parse/compile/execute calls with
  the layer functions
- `src/dbt_graphql/security/policy.py` — add claim-path tracking to
  `PolicyEngine.resolve()` so L2 keys are correct
- `tests/integration/` — new `test_cache_layers.py`,
  `test_cache_burst_protection.py`, `test_cache_redis.py`

## 7. Implementation plan

Each step is a self-contained PR. Order is mandatory — later steps depend
on earlier ones.

### Step 1 — Cache config & setup scaffolding

- Add `cashews` to `pyproject.toml` deps.
- Create `cache/config.py` with the pydantic models from §5.1.
- Create `cache/setup.py`:
  - `setup_cache(cfg: CacheConfig)` iterates `cfg.backends`, calls
    `cashews.cache.setup(b.url, prefix=b.prefix)` for each enabled.
  - `close_cache()` for lifespan teardown.
- Wire into `AppConfig` and the API lifespan.
- **Tests:** unit test that mem and Redis URIs parse, that disabled
  backends are skipped, that default config produces a working in-mem
  cache.

**Acceptance:** `dbt-graphql serve` boots with cache enabled, can `GET`
and `SET` via cashews, no behavior change to query path yet.

### Step 2 — L1 parsed-doc cache

- Create `cache/keys.py::canonicalize_doc(query_str)` → uses `graphql.parse`
  + `graphql.print_ast` to normalize whitespace and field order.
- Create `cache/parsed_doc.py::parse_and_validate(query_str, schema)`:
  - Compute `key = sha256(canonicalize_doc(query_str))`.
  - LRU lookup in `cashews` with `prefix="parse:"`.
  - On miss: parse + validate, store the resulting `DocumentNode` and the
    list of validation errors (or `None`). Cache *both* sides — invalid
    queries should not re-validate.
- Replace direct `parse()` calls in `api/resolvers.py` with this helper.
- Emit OTel counter `cache.parsed_doc.{hit,miss}`.
- **Tests:** same query string twice → second call cache-hit; whitespace
  variations land on same key after canonicalization; invalid query
  cached and returns same errors on second call.

**Acceptance:** repeated identical queries skip parse + validate (verified
via spans / mock counter).

### Step 3 — Policy claim-path tracker

- In `security/policy.py`, wrap `JWTPayload` access with a
  `TrackingJWTPayload` that records every attribute path read during
  policy evaluation (including dotted paths like `claims.org_id`).
- `PolicyEngine.resolve()` returns
  `ResolvedPolicy(..., claim_paths_referenced: frozenset[str])`.
- This is L2's prerequisite — no caching changes yet.
- **Tests:** policy with `when: "'analysts' in jwt.groups"` records
  `{"groups"}` only; nested `row_level: "{{ jwt.claims.org_id }} = ..."`
  records `{"claims.org_id"}`.

**Acceptance:** every existing policy test still passes; new tests
confirm tracker accuracy for `when:`, `row_level:`, and mask expressions.

### Step 4 — L2 compiled-plan cache

- Create `cache/keys.py::plan_cache_key(doc_hash, resolved_policy, jwt)`
  per §3.4.
- Create `cache/compiled_plan.py::compile_with_cache(ast, resolved_policy, jwt, compiler)`:
  - Build key from `doc_hash` (already computed in L1) +
    `resolved_policy.role_signature` + claim signature.
  - LRU lookup with `prefix="plan:"`.
  - On miss: run the compiler, store the resulting
    `(SQLAlchemy stmt, mask plan, row-filter recipe, column-allowlist)`.
- Replace direct compile calls in `resolvers.py`.
- Emit `cache.compiled_plan.{hit,miss}`.
- **Tests:**
  - Two users in same role with same relevant claims → cache hit on
    second user's request.
  - Two users in same role with different *irrelevant* claims (e.g.,
    different `email`, `iat`) → cache hit (claim signature equal).
  - Two users with different relevant claims → cache miss (correct
    isolation).

**Acceptance:** L2 cuts policy-eval + SQL-build CPU on hot paths; tenant
isolation preserved.

### Step 5 — L3 result cache + singleflight (the burst-protection step)

- Create `cache/result.py::execute_with_cache(sql, params, table_names, runner)`:
  - Compute key per §3.3.
  - Resolve TTL: `min(per_table_ttl[t] for t in table_names)` or
    `default_ttl_s`. TTL `0` → bypass cache (still go through lock for
    coalescing? **no** — `0` means "don't cache and don't coalesce";
    realtime tables go straight to warehouse).
  - Wait — reconsider: for tables marked `0`, we still benefit from
    coalescing concurrent identical queries. **Decision:** TTL `0` →
    coalesce but do not persist. `cache.set(..., expire=1)` (1-second
    micro-TTL) gives this naturally and keeps the code uniform. Document
    that "0 means realtime + minimal coalescing window."
  - Implement the GET → lock → re-check → SET flow.
- Wire into `resolvers.py` after compile, before warehouse execution.
- Emit `cache.result.{hit,miss,coalesced}` — distinguish a "wake to
  populated cache after lock wait" (coalesced) from a fresh hit, since
  the operational meaning is different.
- **Tests:**
  - Same query within TTL → cache hit.
  - Cold start, 100 concurrent identical queries → exactly 1 warehouse
    call (assert via mock runner counter).
  - TTL boundary, 100 concurrent identical queries → exactly 1 warehouse
    call.
  - Distinct queries → no contention, all run.
  - Lock holder crash (simulated) → safety timeout releases, next
    caller proceeds.
  - Per-table TTL `0` → coalesces but does not persist beyond ~1s.

**Acceptance:** burst test (`pytest -k burst`) demonstrates 100→1
coalescing; latency tests show TTL hits are <5ms.

### Step 6 — Redis backend integration test

- Add `tests/integration/test_cache_redis.py` using a `redis` fixture
  (testcontainers or `pytest-redis`).
- Run the same burst test against Redis-backed L3. Ensure two API
  workers (spawn two `httpx.AsyncClient`s against two Uvicorn workers,
  or simulate via two cashews `cache` instances pointed at the same
  Redis) coalesce across processes.
- **Acceptance:** multi-replica burst → 1 warehouse call (proves
  `cache.lock` works as a distributed lock on Redis).

### Step 7 — Operator-facing observability

- Expose cache stats endpoint or surface in existing `/metrics`:
  per-layer hit/miss/coalesce counters, current entry counts (where
  available), Redis connection state.
- Add `docs/configuration.md` section on cache config.
- Add `docs/architecture.md` section on the layer flow with this plan as
  reference.

### Step 8 — Optional escape hatches (deferred — only if real harm observed)

These are explicitly **not** in the MVP. Implement only if production
telemetry shows the need.

- **Short-circuit response** for over-budget requests:
  if a request waits >`short_circuit_after_s` seconds for the lock, return
  HTTP **203** with `X-Continue-Wait: true` and an empty body. The
  warehouse query continues in background and populates the cache for
  subsequent requests. Client retry policy is its own concern.
  - Rationale for 203 over Cube's "200 + JSON error": 203 (Non-Authoritative
    Information) is the closest standard status that signals "this is a
    placeholder, real data is pending." A 200 with a JSON `error` field
    abuses the success status code and forces every client to inspect
    response bodies. We refuse that pattern. (202 Accepted was considered;
    rejected because it implies async processing with no result to return,
    which conflicts with the synchronous GraphQL contract.)
- **Connection-pool guards** at the warehouse driver level (semaphore in
  front of the SQLAlchemy engine pool) — only if singleflight alone is
  insufficient.
- **Per-key concurrency caps** on lock acquisition timeouts, so a slow
  warehouse query can't pile up unbounded waiters.

## 8. Self-protection analysis

The user's question — "can a burst harass our server, leak connections,
hang us?" — deserves an explicit answer:

| Threat | Mitigation in MVP |
|---|---|
| Burst of identical queries → warehouse stampede | L3 lock coalesces to 1 warehouse call |
| Burst of distinct queries → warehouse pool exhaustion | **Not mitigated by MVP.** Existing SQLAlchemy pool sizing applies. If pool is N, the (N+1)th request blocks on pool checkout. Acceptable: clients see latency, server stays up. |
| Slow client + long warehouse query → connection held | Asyncio handles thousands of idle waiters cheaply (~few KB each). Client timeout is the client's job; ASGI server (Uvicorn) has its own keepalive limits as backstop. |
| Lock-holder crash mid-query | `expire=60s` on the lock auto-releases |
| Memory growth from queued waiters | Each `cache.lock.__aenter__()` waiter is a coroutine + a Redis BLPOP / asyncio.Event — bounded by HTTP server's max in-flight requests (Uvicorn `--limit-concurrency`) |
| Cache poisoning via crafted JWT | L2 key correctness (claim-path tracker §3.4) prevents cross-tenant leakage |

**Conclusion:** MVP self-protection is sufficient. We do **not** need
Continue Wait. The 203 escape hatch is documented for future use only.

## 9. Testing strategy

- **Unit tests** per cache module (`test_cache_keys.py`,
  `test_cache_parsed_doc.py`, `test_cache_compiled_plan.py`,
  `test_cache_result.py`).
- **Integration test** at the API layer (`test_cache_layers.py`):
  end-to-end GraphQL request goes through all three layers, second call
  hits cache, distinct JWTs isolate correctly.
- **Burst test** (`test_cache_burst_protection.py`): use
  `asyncio.gather()` to fire 100+ concurrent identical requests against
  a mock runner that increments a counter. Assert exactly 1 invocation.
- **Redis integration** (`test_cache_redis.py`): same burst test against
  multi-process / multi-cache-instance setup.
- **Soak test** (manual, optional): leave cache running for >TTL with
  random query mix; confirm no memory growth, no connection leaks.

## 10. Open questions

- **L2 invalidation on policy hot-reload (Sec-K).** Bump a generation
  counter included in the L2 key, or flush the `plan:` prefix. Lean
  toward generation counter — simpler, no flush coordination.
- **JWKS cache.** Sec-A territory, handled by `PyJWKClient.get_signing_key_from_jwt`
  natively. Out of scope here. Mentioned only because operators may
  expect it under the cache config umbrella; we explicitly don't put it there.
- **Per-query `@cached(ttl:)` directive override.** Hasura-style. Not in
  MVP; per-table TTL covers 95% of cases. Add later if operators ask.

---

**Implementation readiness:** This plan is sufficient to begin Step 1
without further design discussion. Each step has a clear acceptance
criterion and is independently shippable.
