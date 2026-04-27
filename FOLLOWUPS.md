# Follow-ups from the 2026-04-27 deep review

Remaining deferred items. The Tier 1–3 closeout (doc sweep, Arch.1 serve
collapse, security hardening, Jinja `row_level` removal, mask conflict
structured error, nested-relation × policy test, singleflight lock
timeout test, pool admission HTTP 503 test, schema CI guard) shipped in
the same pass. What's left is the architectural / SOTA work that needs
design before commitment.

---

## D. Test breadth gaps

### D.3 MCP HTTP transport e2e tests
`test_mcp_enrichment.py` tests `SchemaDiscovery` directly. There's no
test that drives the MCP Streamable-HTTP endpoint and verifies tool
listing / invocation / resources / prompts at the protocol level. Phase
3 (SOTA MCP surface) will add more tools — bake the e2e harness now so
the new tools come with HTTP coverage by default.

---

## F. SOTA gaps (architectural — bigger than quick wins)

### F.1 DataLoader-style sibling subquery batching (HIGHEST LEVERAGE)
Today, nested relations compile to per-field correlated subqueries.
SOTA peers (Hasura, Cube) batch sibling relations into a single
`WHERE id IN (...)` lookup. For result sets with repeated relations
this is 2–5× warehouse concurrency reduction. The result cache
(Sec-J) only helps for **identical full** queries — it does nothing
for subquery overlap. Worth a design doc + spike before committing.

### F.2 Query cost / complexity scoring
`max_depth` exists but no complexity scoring. SOTA: Apollo, Hasura,
GraphJin. Without this, a deeply nested query can compile to an
expensive SQL the warehouse will struggle with. Sec-N/pool admission
catches the symptom but not the cause.

### F.3 Persisted queries / APQ
Independent of Sec-E (Query Allow-List) — APQ is an Apollo-defined
spec that lets clients send a hash on subsequent calls. Worth
considering when Sec-E is implemented since they share the
"client-supplied hash → server-known query" structure.

### F.4 Subscriptions / federation — confirmed out of scope
`docs/architecture.md` correctly excludes these. No action needed
beyond ensuring the rationale is explained briefly in the doc (right
now it just says "no subscriptions, unions, federation, defer/...").

---

## Recommended sequencing

1. **F.1 DataLoader spike** — design doc, then spike, then ship. Highest
   leverage of remaining work.
2. **D.3 MCP HTTP harness** — bake before Phase 3 lands new tools.
3. **F.2 / F.3** — opportunistic, paired with Sec-E when that ships.
