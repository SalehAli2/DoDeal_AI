# Two decisions before Unit A gets past route skeletons

**Status:** proposed · **Decide by:** before step 6 (route skeletons) · **Owner:** lead engineer
**Audience:** backend team (decision 1), business (decision 1 §"what we need from you"), engineering (decision 2)

Both decisions are cheap this week and expensive after the first real route exists, because the route
skeletons bake in whichever answer we don't make. Neither is blocked on the key or the deploy.

---

## Decision 1 — Who calls us, and as whom

### The question

Every request to this service today must carry a **forwarded end-user JWT** (Tymon, HS256) and the tenant's
`Host` header. Gate 1 verifies the user, Gate 2 matches the tenant, and the verified `sub` is the identity we
key the rate limit on and group every per-rep output by.

That assumption is written nowhere in the integration guide and has never been confirmed. There are three
callers in the roadmap, and only one of them fits it:

| Caller | Carries a user JWT? | Fits today's gates? |
| --- | --- | --- |
| The CRM, after a note saves (Unit A, Project 1) | **Unknown — this is the open question** | Only if the CRM forwards the user's token |
| The CRM or a scheduler, for analytics and the daily brief (Unit A, Project 2) | Possibly not (no user is "acting") | No, unless a user is in the loop |
| Our own background workers (Unit B) | No — a worker has no request, no JWT, no Host | No |

### Why it cannot wait

If the CRM calls us **server-to-server with its own credential**, no request passes Gate 1 as built, and
Project 1 does not ship until the identity model is redesigned. The rate limit ("three prompts per person per
hour") and every per-rep measure need a subject; with a service call there is none unless the CRM asserts it.
Unit B has the same problem in a worker regardless of what the CRM does. Building route skeletons now, on the
forwarded-JWT assumption, means rebuilding them if the answer is anything else.

### The options

**A. Forwarded user JWT (today's assumption).** The CRM passes the end user's token and tenant host through
unchanged. *Pro:* nothing to build; `sub` is verified for free; the audit trail names a real user. *Con:* the
CRM must be willing and able to forward a user token to a third service; analytics with no acting user and
every worker still need a separate answer, so this option alone does not cover the roadmap.

**B. Service-to-service only.** The CRM calls with a shared service credential; the tenant and acting user
ride in the request body. *Pro:* simplest for the CRM. *Con:* the subject is **asserted, not verified** — the
rate limit and per-rep outputs rest on a string the caller supplies, which is exactly the flaw two earlier
editions of the plan were corrected for. Gate 1 would be verifying the CRM, not the user.

**C. A principal model with three kinds of caller (recommended).** One concept — a *principal* — with three
verified sources, each producing the same downstream `TenantScope` that the tool layer requires:

- **User principal** — a forwarded user JWT (what exists today). Verified subject, verified tenant.
- **Service principal** — a token *we* issue to the CRM (or RS256-signed by the CRM), asserting the tenant and,
  optionally, an on-behalf-of subject. The tenant is verified by the signature; the subject is recorded as
  *asserted* and audited as such. Rate limits key on it; per-rep outputs label it as caller-asserted.
- **Job principal** — a signed job payload for workers: tenant, job id, source principal. A worker cannot
  construct a `TenantScope` any other way.

*Pro:* covers all three callers; keeps "verified" and "asserted" distinguishable in the audit log rather than
blurred into one `subject` field; the tool layer takes a `TenantScope`, so neither a route nor a worker can
hand-build tenant identity. *Con:* one more token type to agree with the backend, and one more verifier
behind the existing `TokenVerifier` seam.

### Recommendation

**C**, with **A as the first implemented source** (it already exists). Add the service principal when the CRM
integration contract is agreed (ask 7), and the job principal with Unit B step 4. The code change now is
small: introduce `TenantScope`, make `RequestContext` produce one, and type the tool layer to accept only
that. Everything else is additive.

### What we need from the backend

1. One sentence: *when the CRM calls the AI service after a note saves, does it forward the end user's JWT and
   the tenant Host, or does it call with its own credential?*
2. If its own credential: agreement on a signed service token carrying `tenant` and `on_behalf_of` (user id),
   RS256 preferred so the signing key is not shared. This is the same RS256 migration already agreed for
   user tokens — it is not new work, it is the same key.
3. Confirmation that the CRM user id in the JWT `sub` is the same id space as a note's `author_id` (ask 3 in
   ASSUMPTIONS §4.3). Without it, no caller can be joined to their own notes under any option.

### What we need from the business

Acceptance that where the subject is **asserted** rather than **verified** (any server-to-server path), per-rep
outputs will say so, and enforcement of the per-person prompt limit is advisory. A number attached to a
person must be traceable to a verified person, or labelled as not.

---

## Decision 2 — One execution model, not three

### The question

The request path is asynchronous at its edges (the HTTP transport and the LLM seam are `async`) and
synchronous in the middle (the Redis client and the gates run in a thread pool). Unit B is written up as
Celery — a synchronous, forking process model. Nothing in Unit B exists yet: `workers/celery_app.py` is a
four-line docstring. Celery was assumed, not chosen.

If we proceed as planned, the codebase runs three execution models at once:

| Path | Model | Known hazards (already named in the Unit B plan) |
| --- | --- | --- |
| Request edges (httpx, LLM) | async | — |
| Request middle (Redis, gates) | sync inside a thread pool | a 2 s Redis timeout holds a pool thread per request; a future async unit calling the sync Redis client blocks the event loop |
| Workers (Celery) | sync, forked | cached Redis clients shared across forks; the LLM seam is `async` and must be driven from sync code per task; retry multiplication between Celery redelivery and the watchdog |

### Why it cannot wait

Step 3 (token counters) decides whether the cost client is sync or async. Step 6 decides whether routes are
`def` or `async def`. Unit B step 4 decides the worker runtime. After those three, changing the model means
touching every call site that talks to Redis or a model.

### The options

**A. Keep three models; manage the boundaries.** *Pro:* no decision to make. *Con:* every hazard in the table
above has to be managed by convention forever, and the Unit B review questions in ASSUMPTIONS §15 exist
because the reviewer already expects it to go wrong.

**B. All-async, one runtime (recommended).** Request path fully async: `redis.asyncio` for the cost and
operational clients, `async def` routes, gates unchanged in shape. Workers run the *same async code* under an
async-native task runner — `arq` (Redis-backed, small, well-understood) or a Redis Streams consumer we own —
instead of Celery. One Redis client type, no fork, no sync-to-async bridge; a job calls `LLMClient.complete()`
exactly the way a route does. Job state, priority lanes, dead-lettering and idempotency are still designed in
(they are the *shape* of the pipeline, not the runtime), but on one model. *Con:* `arq` has a smaller
ecosystem than Celery, and the team must be comfortable reasoning about an event loop in a worker. The
two-stage/three-lane design maps cleanly onto arq queues or Streams consumer groups; nothing in the Unit B plan
depends on a Celery-only feature.

**C. All-sync.** Drop async everywhere. *Con:* throws away the concurrency the note sweep needs (§6.2 assumes
eight-fold concurrent note reads) and makes the LLM seam Unit B consumes synchronous. Not viable.

### Recommendation

**B.** Concretely, in the next three steps: step 3 builds the token counter on `redis.asyncio` and converts
the existing cost client with it (the Lua script is unchanged; only the call is awaited); step 6's routes are
`async def`; Unit B step 4 is written against `arq` or Streams, not Celery, and `celery_app.py` is deleted
rather than filled in. The watchdog already works with either; the `retry=False`-for-paid-calls rule is
unchanged.

### What this does not decide

The fail-closed policy for worker cost charging (workers pause on a counter failure, requests fail open) is
a policy carried per caller and is independent of the runtime. The audio-governance and STT-spike gates on
Unit B are untouched. This decision only removes a class of bugs the Unit B plan already lists as expected.

---

## If we do nothing

Decision 1 unmade: the route skeletons assume a forwarded user JWT; if the CRM calls server-to-server, Project 1
stalls at integration, after the code is written. Decision 2 unmade: step 3 ships a sync Redis client that the
first async unit blocks the event loop on, and Unit B inherits a fork/async boundary that its own plan calls a
trap. Both failures are discovered at build time, in front of the client, instead of this week, on paper.
