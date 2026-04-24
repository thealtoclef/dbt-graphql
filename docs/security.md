# Security

How `dbt-graphql` authenticates callers and where the line sits between
"authentication" (who are you?) and "authorization" (what can you see?).

- **Authentication** is delegated to an external Authorization Server.
  We trust a signed JWT.
- **Authorization** is local, declarative, compile-time, and lives in
  [`access.yml`](access-policy.md). The policy engine reads claims
  from the JWT payload and shapes the SQL accordingly.

---

## Design — OAuth 2.0 Resource Server

dbt-graphql is a **Resource Server**, not an Authorization Server.

```
 ┌────────────────────┐      1. log in       ┌──────────────────────┐
 │  End-user / client │ ───────────────────▶ │ Authorization Server │
 │   (browser, CLI,   │ ◀───── 2. JWT ────── │ (Auth0 / Keycloak /  │
 │    agent, BI tool) │                      │  Cognito / Clerk /   │
 └─────────┬──────────┘                      │  Cube / in-house)    │
           │                                 └──────────────────────┘
           │ 3. GET /graphql
           │    Authorization: Bearer <jwt>
           ▼
 ┌────────────────────┐
 │ dbt-graphql        │   4. verify JWT signature + standard claims
 │ (Resource Server)  │   5. evaluate access.yml against payload
 │                    │   6. compile GraphQL + policy into SQL
 └─────────┬──────────┘
           │
           ▼
 ┌────────────────────┐
 │ Warehouse          │
 └────────────────────┘
```

The Authorization Server is someone else's concern. We don't issue
tokens, we don't handle passwords, we don't hit a user-info endpoint on
every request. Given a valid signature, we trust the payload.

### Why this shape?

It is the same split used by every SOTA data platform with an auth
story — [Cube](https://cube.dev/docs/product/auth/methods/jwt),
Hasura, PostGraphile-with-pg-jwt, Snowflake's external OAuth, every
Auth0-protected API, Envoy's JWT filter, Kubernetes' API server. The
reasons repeat:

1. **One subject of truth for identity.** If the org already has an
   IdP, bolting a second identity store onto the data API splits
   users/groups/onboarding/offboarding into two systems that will
   drift.
2. **Stateless authentication.** No session store, no round-trip to
   the IdP per request. The signed payload *is* the credential.
3. **Credential rotation is a key-management problem, not an app
   problem.** Rotate the IdP's signing key, JWKS endpoint updates,
   we pick up the new `kid` automatically.
4. **Clean separation from policy.** The IdP stamps claims (`groups`,
   `org_id`, `role`, …); `access.yml` interprets them. Changing policy
   never means changing the auth system.
5. **Pluggable front-end.** Translation, exchange, mTLS, cookies, API
   keys — all of it belongs in a proxy or sidecar (see next section).
   The app always sees the same thing: a JWT.

---

## Middleware layer (between the caller and us)

The Resource Server assumes `Authorization: Bearer <jwt>` on the wire.
What sits in front of us and produces that header is intentionally out
of scope — it's a separate deployment concern and there are many valid
shapes.

| Shape | Middleware does | Resource Server sees |
|---|---|---|
| **Direct JWT from client** | Nothing — the client already has a JWT from the IdP. | `Bearer <jwt>` |
| **Opaque token exchange** | Client sends an opaque token; proxy calls IdP introspection or token-exchange endpoint, mints a JWT, forwards. | `Bearer <jwt>` |
| **Session cookie → JWT** | Browser sends a session cookie to an edge proxy; proxy mints a short-lived JWT for the backend call. | `Bearer <jwt>` |
| **mTLS → JWT** | Service-to-service mTLS terminated at an Envoy/Istio sidecar; sidecar stamps a SPIFFE-style JWT with the caller's identity. | `Bearer <jwt>` |
| **API key → JWT** | An API gateway keeps a table of `(api_key → role/claims)`; on each request it swaps the key for a freshly-minted JWT. | `Bearer <jwt>` |

None of this requires code in dbt-graphql. The point of the
Resource-Server split is that we don't need to care *how* the JWT got
there — only whether its signature checks out.

> **Why not ship API-key support in-app?** It looks tempting ("just
> accept `X-Api-Key`"), but it re-introduces a credential store, a
> rotation policy, and an audit trail for the Resource Server to own —
> exactly the split we avoided by adopting the Resource-Server
> pattern. Keep keys in the proxy/gateway that already owns
> customer-facing credentials.

---

## What we verify (and what we trust)

When Sec-A signature verification lands (see [ROADMAP.md](../ROADMAP.md)),
the auth backend will enforce:

| Check | Meaning |
|---|---|
| Signature | JWT was issued by the configured IdP and hasn't been tampered with. HMAC (HS256) via shared secret, or asymmetric (RS256/ES256) via a JWKS endpoint. |
| `alg` allow-list | Reject `none` and any algorithm not explicitly listed — closes the classic `alg=none` downgrade. |
| `exp` | Token is not expired (with configurable leeway for clock skew). |
| `nbf` | Token is valid *now* (not-before). |
| `iss` | Issuer matches the configured IdP (optional but recommended). |
| `aud` | Audience matches this API (optional but recommended). |

Everything else in the payload — `sub`, `email`, `groups`,
`claims.org_id`, `claims.region`, … — is **trusted** once the above
checks pass. Interpretation lives in `access.yml`.

### Claim conventions

No claims are required by dbt-graphql itself. The policy engine reads
whatever `access.yml` asks it to. Common conventions:

| Claim | Typical use |
|---|---|
| `sub` | Stable user ID — useful for `row_level: "owner_id = {{ jwt.sub }}"`. |
| `email` | Display / auditing. |
| `groups` | List of role-like strings — drives `when: "'analysts' in jwt.groups"`. |
| `claims.org_id` | Multi-tenant isolation — drives `row_level: "org_id = {{ jwt.claims.org_id }}"`. |
| `claims.region` | Region-scoped filters. |

If you control the IdP, match your claim structure to what's
convenient in policy; both sides are yours to design.

---

## Anonymous access

There is no `anonymous_role` config. When a request arrives with no
`Authorization` header (or the header is malformed), the auth backend
produces an **empty** `JWTPayload` — all attribute accesses return
`None`. Express anonymous access in policy directly:

```yaml
policies:
  - name: anon
    when: "jwt.sub == None"
    tables:
      products:
        column_level: { includes: [product_id, name, price] }
        row_level: "published = TRUE"
```

This keeps one code path (everything is "evaluate policy against a
JWT"), avoids a second config surface for anonymous-role mapping, and
makes the anonymous policy visible in the same file as every other
policy.

Once Sec-A signature verification lands, a request with an *invalid*
token (bad signature, expired, wrong issuer) is rejected with HTTP 401
— it does not fall through to the anonymous policy. Anonymous is "no
token sent"; invalid is "something tried to lie."

---

## Related components

| Component | Role | File |
|---|---|---|
| `JWTAuthBackend` | Reads `Authorization` header, decodes JWT, exposes `request.user.payload`. | [`src/dbt_graphql/api/security.py`](../src/dbt_graphql/api/security.py) |
| `AuthenticationMiddleware` | Starlette middleware that runs `JWTAuthBackend` per request. | [`src/dbt_graphql/api/app.py`](../src/dbt_graphql/api/app.py) |
| `PolicyEngine` | Evaluates `access.yml` against the payload, returns a `ResolvedPolicy`. | [`src/dbt_graphql/api/policy.py`](../src/dbt_graphql/api/policy.py) |
| `compile_query` | Applies the `ResolvedPolicy` — strips blocked columns, rewrites masks, appends `WHERE`. | [`src/dbt_graphql/compiler/query.py`](../src/dbt_graphql/compiler/query.py) |

---

## Related docs

- [access-policy.md](access-policy.md) — policy language, `when:` /
  `row_level:` reference, evaluation model.
- [configuration.md](configuration.md) — `security.policy_path`
  (and the upcoming `security.jwt` block for Sec-A).
- [../ROADMAP.md](../ROADMAP.md) — Sec-A through Sec-L security
  roadmap.
