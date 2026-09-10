# ASSUMPTIONS

The single seam ledger for the service. Every provisional decision, its status,
the seam it lives behind, and how to correct it when the real answer lands.

**Status meanings**

| Status | Meaning |
| --- | --- |
| CONFIRMED | Verified with the backend. Documented by them, tested by them, or seen by us. |
| BUILT | Built, wired, working. May still hold placeholder values. |
| DECIDED | We have chosen. Not awaiting anyone. |
| PENDING | Awaiting a backend or business answer. |
| PARKED | Built and tested, not wired into the live path, awaiting a decision. |
| DEFERRED | Deliberately not built yet. |

**Evidence tags** (used on backend claims only)

- `[D]` Documented — a document states it. Nobody has run it.
- `[T]` Tested by them — covered by the integration guide's end-to-end checks.
- `[V]` Verified by us — we have made the call and seen the response.

> **Nothing is tagged `[V]`.** We have never made a successful live call to the
> backend. Phase 0 exit criterion 3 is open. Treat no backend behaviour as proven.

---

## AT A GLANCE

- **Unit A is READ-ONLY.** There is no write path to the CRM. Judgements are
  returned to the caller, which persists them.
- **Buildable today, no backend access needed:** the LLM seam, FakeLLM, token
  counters, unit schemas, route skeletons, classification, vague detection,
  scoring, the reprompt path, the clarification loop, the rate limit, real
  prompts. That is most of Project 1.
- **Blocked on the key and deploy:** wiring the real model, and every `[V]` tag.
- **Blocked on one sentence:** does adding a note bump the lead's `updatedAt`.
- **Blocked on one unstated assumption:** how the caller authenticates to us.
- **Blocked on data:** evaluation.
- **Biggest unresolved risks:** within-tenant exposure, and the two business
  lines sharing one rubric.

---

# 1. CONFIRMED — the backend contract

### 1.1 Token type — Tymon JWT, verified offline `[T]`
- The inbound token is a Tymon JWT, verified offline against a shared secret.
  Not a Sanctum token.
- **Seam:** `core/auth/verify.py` (`TokenVerifier` protocol; `JwtVerifier` today).
- **How to correct:** write a new `TokenVerifier` with the same interface and
  return it from `get_verifier()`. Gates, claim mapping and context are untouched.

### 1.2 Algorithm — HS256 today, RS256 agreed but not provisioned `[D]`
- HS256 (symmetric shared secret) in use. RS256 requested and agreed; public key
  not yet provisioned. On the local test key until then.
- With HS256 the held secret can MINT tokens, not only verify them. RS256 removes
  that risk. This is why RS256 was requested.
- **Unrelated to the lead/note integration.** Those endpoints use DD-API-KEY, not
  a JWT. RS256's status does not block them. Track separately.
- **Seam:** `core/config.py` `jwt_algorithm`.
- **How to correct:** set `DODEAL_JWT_ALGORITHM=RS256`, point the key config at
  the public key. No code change.
- Code side is RS256-ready: `pyjwt[crypto]` installed, RS256 round-trip and
  alg-confusion tests in `test_verify.py`. The switch is
  `DODEAL_JWT_ALGORITHM=RS256` + `DODEAL_JWT_SIGNING_KEY=<public PEM>`. Still
  waiting on the backend's public key (asked twice).

### 1.3 Claim names — sub / subdomain / database `[T]`
- The token carries `sub` (user id, INTEGER), `subdomain` (tenant), `database`,
  plus iat/exp (and nbf/jti/prv). **No role or permission claims.**
- **`database` holds `crm_<subdomain>`** — the tenant's database name, derived
  from the same subdomain that is the tenant identity. It is carried on
  `Identity` and `RequestContext` and recorded, but nothing routes on it: this
  service reaches no database, and isolation is enforced backend-side (§1.6).
- **Seam:** `core/auth/claims.py`, names read from `core/config.py`.
- **How to correct:** set `DODEAL_CLAIM_SUBJECT` / `_SUBDOMAIN` / `_DATABASE`.
  A rename is config, not code.

### 1.4 No iss, no aud `[T]`
- Neither claim is present. Those checks are removed. `exp` is verified.
- Clock skew: `exp`/`nbf`/`iat` verified with `DODEAL_JWT_LEEWAY_SECONDS`
  (default 30). Reason codes `token_expired` / `token_not_yet_valid` /
  `invalid_iat` are distinct from `invalid_token` so drift is diagnosable in the
  audit log.

### 1.5 Integer sub handling `[T]`
- PyJWT rejects a non-string `sub`. Tymon's is an integer, so PyJWT's check is
  disabled (`verify_sub: False`) and the claim layer normalises it instead.
- **DO NOT re-enable PyJWT's sub verification.** It would reject valid tokens.

### 1.6 Tenant — a validated DNS label at both boundaries `[T]`
- The tenant is the `subdomain` claim and it is authoritative. It is validated
  and lowercased as a single DNS label (`claims.TENANT_LABEL_RE`: 1-63 chars,
  `[a-z0-9-]`, no leading/trailing hyphen) at BOTH boundaries — the token claim
  and the Host header — using the one rule, defined once.
- Gate 2 compares the WHOLE host, case-insensitively, as
  `<tenant>.<inbound_base_domain>` after stripping a `:port` and one trailing
  dot. Comparing only the first label (the old behaviour) let
  `<tenant>.evil.com` through; that is audit finding H4.
- Reason codes: `invalid_tenant_claim` (the claim is present but not a valid
  label — audit finding H6), `invalid_host` (the Host is not
  `<label>.<inbound_base_domain>` at all: wrong domain, no subdomain, extra
  subdomain level, IPv6 literal, or absent), `tenant_mismatch` (valid shape,
  different tenant). All are a generic 403 to the client.
- Isolation is enforced backend-side by tenant database, keyed on subdomain. Lead
  records carry no tenant field, so the re-check is a host match.
- An absent or subdomain-less Host is a hard 403 (deliberate). `localhost` fails
  Gate 2; local testing needs a tenant Host header.
- `DODEAL_INBOUND_BASE_DOMAIN` is separate from `backend_base_domain`: the host
  of arrival is an open question with the backend and may become e.g.
  `ai.dodealcrm.com` without changing the outbound URL.
- **X-Forwarded-Host is deliberately NOT read** — deferred until the backend
  confirms the host of arrival. The seam is marked in `core/tenancy.py`.
- **Seam:** `core/tenancy.py`, `core/auth/claims.py`, `gate2_tenant` in
  `core/auth/dependencies.py`.

### 1.7 The service surface — three read endpoints only `[T]`
Base `https://<tenant>.dodealcrm.com/api/service`, DD-API-KEY header only.

| Endpoint | Behaviour |
| --- | --- |
| `GET /leads` | Paginated, newest first. `page`, `per_page` (default 25, max 100, 422 above), `since` (ISO-8601 on `updatedAt`), `feedback`, `leadStatus`, `leadSource` (exact match). |
| `GET /leads/{id}` | Single lead. 404 if absent in this tenant, 422 if id not numeric — both `[T]`. The **envelope shape** of the single-lead response is `[D]`: modelled by analogy, never observed. |
| `GET /leads/{id}/notes` | That lead's notes, newest first. `page`, `per_page`. Empty is `200` with `data: []`, not an error. |

- **There is no notes index and no filter by rep.** Notes are reachable only one
  lead at a time. This drives the fan-out problem in §6.2.
- **Envelope:** `{status, data, meta}` with `meta` carrying `current_page`,
  `per_page`, `total`, `last_page`. `data` holds the array directly.
  Earlier `posts.data` and `{success, message, data, meta}` assumptions were both
  wrong and are withdrawn.
- **Note:** the envelope key is `status`, which is also a lead field name. The
  parser is explicit about which is which and a fixture covers both.
- **Tag discipline on this entry.** The LIST endpoint's envelope is `[T]`. The
  SINGLE-lead envelope is `[D]` — assumed to be the same `{status, data, meta}`
  shape with `data` holding one object rather than an array, by analogy alone.
  Nobody has seen a single-lead response. The code already reflects this, and
  `scripts/real_fetch_check.py` exists to settle it with one real call. If it
  differs, the correction is `schemas/lead.py` plus `LeadsClient.get_lead` and
  nothing else.
- **Seam:** `dodeal_ai/schemas/lead.py`, `tools/leads.py`.

### 1.8 The lead contract `[T]`
- **Fields:** id, name, phone, email, leadType, enquiryType, project, status,
  source, feedback, priority, language, leadFor, country, assignedToManager,
  assignedToSales, bookedAmount, createdAt, updatedAt.
- **Every field except `id` may be null.** All optional in our schema. A missing
  input produces a suppressed result, never an imputed one.
- `phone` is **masked** for the service credential — deliberate backend behaviour,
  not a bug. See §7.1.
- `createdAt`/`updatedAt` are ISO-8601 **with the tenant's timezone offset**. Every
  daily/weekly boundary must respect it; a UTC boundary is a wrong-answer bug.
- `assignedToSales` / `assignedToManager` are **bare integers**. Nothing on our
  surface resolves them to a person, so every per-rep output is unlabelled.
- `bookedAmount` is null in the guide's own sample, with no type, units or
  currency stated, and no deal record to corroborate it. **Treated as unusable.**
- `extra="ignore"` on every model — unlisted fields are tolerated, not rejected.

### 1.9 The note contract `[T]`
- **Fields:** id, note, author, author_id, createdAt. **There is no `updatedAt`.**
- `author` is null when the original author's account was deleted; `author_id`
  survives. **Every per-rep grouping uses `author_id`.**
- With no `updatedAt`, a revised note is indistinguishable from an original.

### 1.10 Behaviour to design around `[T]`
- **No role filtering.** A service credential has no user, so the endpoints return
  **every lead in the tenant**. The tenant boundary is the isolation boundary. Any
  narrowing is ours. See §7.2.
- **Empty is not an error.** A lead with no notes returns 200 and an empty array.
- **Incremental sync** uses `since` on `updatedAt`. Store the highest seen and
  pass it back. Depends entirely on §4.2.
- **Read-only.** These endpoints only read. Writing back is a separate
  conversation that has not happened.

### 1.11 Error codes `[T]`

| Code | Meaning | Our handling |
| --- | --- | --- |
| 401 | Missing, wrong, or other-tenant key | Deny. Never fail open. Alert — a 401 here is a configuration fault, not a user error. |
| 404 | Lead absent in this tenant | Normal for a stale id. Suppress the result, do not retry. |
| 422 | Invalid parameter | Our bug. Fail closed, surface in audit, never retry with the same parameters. |
| 403 `[D]` | Tenant subscription expired — whole tenant, not the credential | Distinct from 401 and must not be read as one. Stop work for that tenant, do not retry, report as a tenancy condition. |

### 1.12 Deployment status `[D]`
- The integration guide reports 25 of 25 end-to-end checks passing against a real
  tenant database (1,448 leads, 127 notes) through the full HTTP stack. That is
  why so much above is `[T]`.
- **But the endpoints are not yet deployed to a live tenant reachable over the
  network.** Until the joint call happens, nothing reaches `[V]`.

---

# 2. CONFIRMED — what exists in the CRM but we cannot reach

All real, none available to our credential. This is the register §4 turns into asks.

| Capability | What it would unblock |
| --- | --- |
| Note update endpoint | Persisting scores onto the note. Mitigated by returning the judgement to the caller — a limitation on **history**, not on launch. |
| Tenant-wide notes index | Bulk note analysis at reasonable cost. **The largest single engineering constraint.** |
| Roles, permissions, hierarchy | Gate 3 at the source, per-rep labels, every manager view. Two endpoints are known to exist — `GET /api/role-permissions/{role_id}` and `GET /api/roles/{role_id}/permissions` — **Sanctum-guarded, so unreachable with a DD-API-KEY**. See the note below. |
| User and agent lists | Turning `assignedToSales` and `author_id` into names. The CRM UI resolves them, so the data exists internally. |
| Deals, transactions, invoices, commission | Revenue analysis in any form. |
| Agent metrics, leaderboards, targets | Consuming rather than recomputing agent performance. |
| Campaigns, campaign contacts, ad spend | Campaign performance. **Note: the CRM UI shows Campaign and URL fields on the lead**, so campaign data exists — it is simply not on our surface. |
| Deal history, lead cycle, dispatch events | Pipeline trends, stage velocity, speed-to-first-contact. |
| Call logs, meetings, reminders, activities | Corroborating a claimed next step; the Recordings tab confirms recordings live against leads. |
| Tenant configuration store | Business-changeable weights and thresholds without a release. |

**On the permissions endpoints specifically.** They exist and are documented,
but they sit behind Sanctum auth, and our credential is a DD-API-KEY — a
different scheme entirely. So they are visible and unusable, which is the worst
of both: we know the data is there and cannot read it.

Two separate things would have to be exposed before Gate 3 could enforce
anything at the source, and asking for one without the other achieves nothing:

1. **The role → permission mapping** — what those two endpoints return.
2. **The user → role mapping.** The JWT carries `sub`, `subdomain` and
   `database` and **no role claim at all** (§1.3), so even holding the whole
   permission table there is no way to learn which roles the *caller* has.

Until both exist, Gate 3 stays parked behind a placeholder table (§9.1). That is
not a shortcut; it is the only honest position.

**Also record:** in the CRM's schema, `status = NULL` means **active**, not
"unset". A filter written as `status = 'active'`, or any `NOT NULL` guard, would
silently exclude every live record. Worth stating in the ledger rather than in a
code comment, because it will be written wrong at least once.

**The commercial argument.** That these exist is the reason not to rebuild them.
The right conversation is "expose them, or descope them" — not "build a worse
version against three endpoints."

---

# 3. DECIDED — ours, settled, not awaiting anyone

### 3.1 Shape and access

| Item | Position |
| --- | --- |
| **Shape** | Request-driven, synchronous, behind the gate chain. No scheduler, no background work, no new persistence layer. |
| **Read-only** | The unit writes nothing to the CRM. Judgements are returned to the caller, which persists what it chooses. |
| **Design A** | Settled **by requirement**: a scoring outage must never block a note save. That rules out sitting in the CRM write path. The note saves first; we read, judge, and return. |
| **The seam** | Unit A owns `core/llm/`. Unit B consumes it — which is why the client method is async and must not assume a short prompt. |
| **Schemas** | Root contracts live in `dodeal_ai/schemas/` (inside the package); unit-owned result types live in the unit. |

### 3.2 Cost and reliability

| Item | Position |
| --- | --- |
| **Counters** | Request and token counters stay separate. Never merged, never converted. Distinct namespaces, distinct limits. |
| **LLM call policy** | `retry=False` with an explicit longer timeout passed **per call**, never a raised global. A timed-out model call may have completed and billed. |
| **Malformed output** | Validate; reprompt **once** with a stricter instruction; then an enumerated error. The bad output is **never** fed back into the prompt. |
| **Stricter instruction placement** | A byte-identical retry returns the same answer, so the stricter instruction must differ **and** sit in the variable tail so the cached prefix still hits. |
| **Idempotency key** | `tenant + operation + item id + content fingerprint`. Tenant is mandatory — each tenant is a separate database and ids collide. |
| **Operational state** | A third named Redis client holds the attempt counter, the rate limit and the idempotency seen-keys. Distinct namespaces per concern. Not the queue db, not the cost db. |
| **Failure policy** | Auth and tenancy deny. Cost allows. An unavailable idempotency check cannot be read as "no duplicate", so it denies. *(Unit B adds a fourth: workers deny on counter failure. Unit A has no workers and does not carry it.)* |
| **Tenant expiry** | 403 is a tenancy condition, not an auth failure. Stop work for that tenant, do not retry. |
| **Caching** | Prompt caching from the first real call (prefix-first, already the pattern). Result caching per feature, **only after** cost or latency is measured. |
| **Cost in workers** | Workers charge spend directly (`enforce_cost` is a plain function, no FastAPI dependency) with real token counts. Workers fail CLOSED on a counter failure: pause the job, do not process, retry later. The request path keeps fail-open, unchanged. Fail-open assumed bounded exposure — one user, one request. A worker draining a backlog has no such bound. Do not unify these policies. |

### 3.3 Output integrity

| Item | Position |
| --- | --- |
| **Versioning** | Every output records prompt, model, rubric/formula and config version. Past outputs are never recomputed when rules change. |
| **Configuration** | Weights, thresholds and catalogues live in our configuration, per tenant, read at runtime. **Not yet business-changeable** — no reachable config store. State that plainly rather than letting it read as "administrator-changeable". |
| **Logging** | Client responses are generic, from a fixed enumerated set with no interpolation. Raw note text and model output are never logged. Exceptions from outside this codebase are logged by type only; our own carry fixed-vocabulary messages. Tracebacks are frames-only, unchained. `OutputValidationError` never holds the pydantic error or the input. Enforced by `tests/security/test_log_safety.py`. |
| **Never invent** | If budget was not discussed, the field stays empty. A wrong figure is more damaging than a blank one. |
| **Salary firewall** | Nothing this unit produces is written into a performance, rating, target or salary record. Currently guaranteed by having no write access at all — it must survive the day that changes. |

### 3.4 Project 1 — business rules

> **Note on provenance:** the BRD was drafted with an AI, not written by the
> business. Treat every rule below as **our** decision, held until someone with
> authority disagrees. It is not a requirement handed down.

| Item | Position |
| --- | --- |
| **Classify before judging** | Six note types — no contact, callback, discovery, viewing, negotiation, won/lost. The type decides which bar applies. No-contact notes are exempt from the full checklist. |
| **Definition of vague** | Missing any of: what happened, what the client said or wanted, a next step with a date. Floor test: *could another agent continue from this note alone, with no handover?* |
| **Scoring weights** | What happened 25, what the client said 20, next step and date 25, deal specifics 20, clarity 10. |
| **Bands, not numbers** | 0-100 internally; shown as Poor (0-39), Fair (40-69), Good (70-84), Excellent (85-100). A visible number invites argument and gaming. |
| **Total computed in code** | The model returns component marks; the total and band are computed by us. Models are unreliable at arithmetic. |
| **Prompt once only** | Ask once, then store as written. A second request teaches people to write filler to escape the prompt. |
| **Prompt must be specific** | Not "please improve this note" but "what was the reason?" or "when are you following up?". Partial improvement is accepted silently. |
| **Rate limit** | Three prompts per person per hour, keyed on the **verified subject from the inbound token**. |
| **Enforcement** | Advisory at launch. Blocking is built but shipped off — and cannot be enforced by us in any case, since we hold no write or veto path. |
| **Language** | Arabic, English and mixed notes judged identically. No salesperson scores lower for the language they wrote in. |
| **Never for pay** | Coaching only, never pay, commission or discipline. |
| **Original preserved** | Trivially guaranteed today — we cannot modify a note. The rule stands for the day a write path appears. |

**Decided during the Unit A Project 1 campaign (phases A–J).** Everything above predates the build;
everything below was settled while building it and is equally ours, held until someone with authority
disagrees.

| Item | Position |
| --- | --- |
| **Attempt key** | `attempt:{tenant}:{lead_id}:{note_id}` — keyed on the note, not the person. The cap is "this note has been asked about once", so two salespeople sharing a lead cannot each spend an attempt on the same note. INCR, `EX` on create and whenever TTL is `-1`. |
| **Fingerprint** | Hex SHA-256 of the **fetched note text** as UTF-8, with **no normalisation** — no trim, no case fold, no Unicode form change. Normalising would make two genuinely different notes look identical, and the fingerprint is what decides 200 versus 409. |
| **Reserve and release** | The idempotency key is reserved (`SET NX EX`) **after the fetch and before any model call**, and released (`DEL`, best effort) on **every** non-200 outcome after reservation. So a `model_unavailable` can be retried by the caller without meeting a 409 for work that was never done. |
| **`prompt_withheld`** | `null` when a prompt was sent **and** when the decision was `accept_silent` — there was nothing to withhold. Otherwise one of `resubmission`, `attempt_cap`, `rate_limited`, `nothing_to_ask`, in that fixed precedence. It names why a prompt that *would* have been sent was not. |
| **Rate limit never 429** | Hitting the clarification rate limit on this route returns **200** with `prompt_withheld: "rate_limited"`, never a 429. The judgement was still produced and is still worth returning; only the question is withheld. A 429 would tell the CRM the request failed when it did not. |
| **Thin evidence** | `min_note_chars 15`, `min_note_tokens 3`, both applied to the **stripped** text, and **both** must pass — "ok" fails on length, a long run of one repeated word fails on tokens. Whitespace-split, deliberately crude, and identical for Arabic, English and mixed text. Checked **before** any reservation and before any model call, so a thin note costs nothing. |
| **Denominators** | 100 with everything applicable · **80** under Q13 (deal_specifics suppressed) · **60** for no_contact under Q13 · 75 for no_contact with Q13 resolved. **The 75 is not reachable** from §2.2's own weights: no_contact suppresses `client_said` *and* `deal_specifics` by type, so lifting Q13 leaves 25+25+10 = 60. Recorded as a disagreement between the campaign brief and the arithmetic; the tree is followed. See CAMPAIGN_REPORT.md, Phase F. |
| **Band is derived** | Never accepted from any input and never returned by the model. A `band` or `total` field in a model answer is malformed output, rejected by `extra="forbid"`, and earns the single reprompt. |
| **Relative times count as dates** | A time anchored to when the note was written — "tomorrow", "after 2 hrs", "next Tuesday", "end of the week" — satisfies `next_step_with_date`. The corpus writes `cb tmrw` far more often than a calendar date. |
| **An explicit closure is a next step** | A stated outcome with a reason — closed, bought elsewhere, withdrew, dropped — takes full marks for `next_step_date`, because nothing follows and the note says so. |

### 3.5 Project 2 — analytics rules

| Item | Position |
| --- | --- |
| **Compute from raw** | Nothing to consume, so every measure is computed from the lead and note records we can read. |
| **Compute before generate** | Anything a formula can produce is produced by a formula. Model calls are confined to narration and prioritisation. |
| **No per-lead model calls** | No aggregate path calls a model once per lead. A hard boundary, not a performance target. |
| **Components always shown** | Any score shown to a person carries its component breakdown. A bare number cannot be defended or acted on. |
| **Evidence travels with the score** | A score carries how much data backed it. Below a configurable floor it is **suppressed**, not shown low. Suppressed is a state in the schema — never a null, never a zero. |
| **Bounded aggregates** | Every aggregate declares a maximum scope and refuses an oversized run **before** fetching. |
| **Note rubric untouched** | The Project 1 rubric stays per note. Lead scoring is a different mechanism at a different grain. |
| **Nothing outcome-calibrated** | No deal or revenue record is reachable, so no score is presented as a probability of closing. |
| **Unlabelled by default** | Per-rep output is keyed on `author_id` and shown as an identifier until a user list exists. We do not guess names. |

---

## 3.6 Unit B — analysis rules

| Item | Position |
|---|---|
| Speaker separation is a precondition | Coaching, scoring, and "did the salesperson address the objection" all need to know who said what. Two problems: diarization (splitting audio into speaker turns — an audio problem, must come from the STT provider) and identification (which turn is the agent — a context problem, solvable in the LLM layer). If separation fails, coaching and scoring are suppressed for that call, not guessed. |
| Uncertainty travels | Every output carries an uncertainty flag. Content from unclear audio is marked uncertain, never stated as fact. Low overall confidence suppresses scoring and coaching entirely. A confident wrong summary is worse than none. |
| Never fill blanks | Anything not discussed is left empty. Never estimated, never inferred. |
| Summary | Six elements in order: what the client wanted, what was discussed, their position and concerns, what was agreed, next step with owner and date, how the call ended. Four to six sentences of prose plus a separate extracted-details block. Always written in English regardless of call language. |
| Objections | Nine fixed categories. For each: category, the client's actual words, whether the salesperson addressed it, whether the client seemed satisfied. The last two carry most of the value — an objection raised and never addressed predicts a lost deal better than overall mood. |
| Coaching feedback | 2-3 specific observations, at least one strength and one improvement, each tied to a quoted moment. Built as a chain (find key moments → score against rubric → write feedback), a fixed sequence, not an autonomous agent. Advisory — a human reviews before it reaches the rep. |
| Handling rubric | Understanding the client 25, handling objections 25, securing a next step 20, professionalism 15, product knowledge 15. Bands: 85+ Excellent, 70-84 Good, 50-69 Needs work, under 50 Coaching required. |
| Escalations regardless of score | Over-promising or guaranteeing returns; quoting price, availability or terms incorrectly; rudeness or pressure; unprofessional competitor talk; ending a qualified call with no next step. |
| Fairness rules | Score only calls over two minutes with genuine engagement. No score where the recording was unclear — a poor score caused by a poor phone line is worse than none. No score on secondary-language calls. Check the checklist against 30 hand-scored calls before enabling scoring. |
| Processing tiers | Under 30s or voicemail: outcome label only, no transcript. 30s-2min: transcript, summary, mood. Over 2min: everything. Everything above 30s is transcribed even when no analysis follows — search value comes from completeness. |
| Launch coverage | Every call over two minutes fully analysed for the first month, even at higher cost, before thresholds are narrowed. A system cannot be tuned on a sample chosen to save money. |
| Never used for pay | Same as Unit A. Stated openly to the sales team at launch. |
| Configurability | The nine objection categories and the five rubric weights are business-owned and will change. They live in configuration, and past calls must be re-runnable when they change. |
| B7 — access is audited | Access to recordings and transcripts is audited: **who read what, and when**. A call recording is the most sensitive content this service will hold — a client's voice, saying whatever they said to a salesperson — and "who can read it" is a weaker control than "who has read it", because the second one is the only one that survives a permission being granted for a good reason and never revoked. **A precondition for any pilot recording**, not a hardening step afterwards: an audit trail that starts after the first recording cannot answer questions about the recordings that came before it. |
| B2 — the summary is English | The call summary is written in **English**, regardless of the language the call was conducted in (§3.6 "Summary" above). An **Arabic summary is a later ask and is not in scope**: it is a second output to evaluate, a second quality bar to hold, and it doubles the review burden on whoever signs the summaries off. Recorded here so that "the summaries are in English" reads as a decision rather than as an oversight — and so that asking for Arabic later is understood as new scope, not a fix. |
| B13 Q2 — storage country unknown | **The storage country for audio and transcripts is not known.** Where a recording physically rests, and under whose jurisdiction, has not been established for any candidate provider or bucket. **This is the same residency answer as Q20** (data residency and a DPA for note text sent to a model provider — `docs/STATUS.md` §6, currently unowned): one question, two kinds of customer content, and answering it for note text without answering it for audio would leave the more sensitive half uncovered. Ask them together, of the same owner. |

## 3.7 DECISION[DIRECT_ROUTE] — the CRM sends the saved note

**Status: DECIDED, and BUILT in Piece K.** The lead's decision, recorded here; not ours to argue.

**The decision.** A new route accepts the note's TEXT in the request body:
`POST /api/v1/notes/judgements/direct` and `POST /api/v1/notes/judgements/direct/resubmission`. This is the
**one exception** to the campaign's never-list item "accept note text in a request body", and it is granted
for these two routes only. **The fetch route stays the contract and is unchanged.**

**The reason.** The CRM's read surface has been unavailable for six weeks, so `GET /leads/{id}/notes` cannot
be relied on to return the note we are asked to judge. The CRM will send the saved note **server-side, after
the save** — so the text we judge is the text that was stored, and the judgement still happens outside the
write path (Design A is untouched: a scoring outage cannot block a note save).

### The three points

**1. Credential.** The CRM forwards **the note author's own user JWT** on the direct call, so the route sits
behind the same gates as the fetch route — Gate 1 (auth) → Gate 2 (tenancy) → Gate 4 (cost), one
`Depends(gate4_cost)`, exactly as before. `author_id` in the body is **trusted as the CRM's stored author**
and is **not checked against `sub`**. When they differ, the outcome log line carries
`author_differs_from_subject: true` (**ids only, never text**); **the request is never rejected for it.** The
per-user cost cap and the clarification rate limit **key on `sub`**, as built.

**2. Length limits.** Two, at different layers:

| Limit | Where | Value | Outcome |
| --- | --- | --- | --- |
| Hard ceiling | `DirectJudgementRequest.note_text` (`schemas.py`) | **4,000 characters** | **422** `invalid_request`, before the pipeline is entered |
| Soft limit | `TenantConfig.max_note_chars`, beside `min_note_chars` | **2,000 characters** | **200** suppressed, `not_scorable` / **`note_too_long`** |

The soft limit is checked **in the same place as the thin gate** — on the stripped text, before any
reservation and before anything is spent — and applies to **both routes**. **No third `SuppressedReason`:**
`note_too_long` is a new `SuppressedDetail` under the existing `not_scorable`.

**3. Both numbers are provisional.** The real sample's longest note is **340 characters**, so both limits are
an order of magnitude of headroom above anything observed. They are chosen to be obviously safe, not measured.

### Correction paths

| If | Then |
| --- | --- |
| **The CRM calls with its own credential** (a service principal rather than a forwarded user JWT) | D1's service principal is **built as its own piece**, behind `TokenVerifier`. **This route does not change** — it already takes a `TenantScope`, so only what builds the scope moves. |
| **The CRM forwards another user's JWT** | The **rate-limit key builder in `state.py` switches to `author_id` for this route**, so the limit follows the person being asked about rather than whoever's token was borrowed. |
| **The limits turn out wrong** | Both are one-line changes: `MAX_NOTE_TEXT_CHARS` in `schemas.py` (a 422 bound) and `max_note_chars` in `config.py` (a suppression bound). Changing the second **bumps `config_version`**; past judgements are never rescored. |

**Seams:** `units/structured_intelligence/pipeline.py` (`judge_note_direct`, joining the shared `_judge` at the
length gate), `api/routes/judgements.py` (the two routes; `get_leads_client` is deliberately not in their
dependency chain), `schemas.py` (`DirectJudgementRequest`, `LeadContext`, `MAX_NOTE_TEXT_CHARS`),
`config.py` (`max_note_chars`, `config_version` → `tenant-cfg-default-2`).

**What stops it becoming the default path:** the fetch route is the documented contract and is unchanged; the
direct body is `extra="forbid"` with no score-shaped field, so it can only ever carry a note and never an
answer; and both routes run the same `_judge`, so there is no behaviour to gain by choosing the direct one.
It exists because the read surface is down, and it is the read surface's return that removes the reason for it.

---

# 4. PENDING — awaiting a backend answer

Ordered by what they release. Items 4.1–4.3 are the critical path.

### 4.1 Deploy, key, and the joint call `[BLOCKS EVERYTHING]`
- The endpoints are not yet on a reachable tenant, and no key is provisioned.
- **Releases:** Phase 0 exit criterion 3, and the first `[V]` tag in this ledger.
- **Owners:** tenant list is ours to supply; deploy and provisioning are the
  backend's. Both are named in the integration guide's own open items — they need
  chasing, not negotiating.

### 4.2 Does adding a note bump the parent lead's `updatedAt`?
- Notes are reachable only per lead, so `since` over **leads** is our only
  incremental route to **notes**.
- **If no:** incremental note sync is unsound; only full sweeps are correct. A
  wrong assumption here produces silently missing data, not an error.
- One sentence to answer. Nothing in their 25 tests covers it.

### 4.3 Is the JWT `sub` the same identifier as a note's `author_id`?
- Both look like CRM user ids, but this has never been confirmed.
- **Note:** our credential *does* give us the acting user — `sub` is verified by
  Gate 1 and sits in `RequestContext.subject`. The DD-API-KEY having no user
  affects **what the backend returns to us**, not who called us. The open question
  is only whether the two id spaces match.
- **If they differ:** we cannot join a caller to their own notes at all.
- **Marker: `ASSUMPTION[Q7]`.** Grep it:
  `src/dodeal_ai/units/structured_intelligence/pipeline.py` and `state.py`,
  `README.md`'s provisional-answers table, and here. **Assumed:** they are
  different id spaces, and the campaign never joins them. The rate limit is keyed
  on the **verified `sub`** from `RequestContext`; the judgement reports the
  note's `author_id` as it came from the backend; nothing anywhere compares the
  two or resolves a name from either. **If they turn out to match:** nothing
  breaks and nothing has to change — the join simply becomes possible, which is
  what "coaching over time, per rep" would need. **If they differ (assumed):**
  the correction path is a mapping table, and it is the backend's to provide.

### 4.3b Is `since` compared with an offset, or with naive local time?
- `GET /leads` filters on `updatedAt` via `since` (§1.7). The CRM's internal
  `lastEdited` is a **naive local** timestamp — no offset, no zone.
- **The question:** does the backend compare `since` as an absolute instant, so
  an offset is respected, or against naive local time, so an offset is at best
  ignored and at worst subtracted?
- **If naive:** every `since` sweep silently misses or double-counts a window the
  size of the UTC offset. As with §4.2, a wrong assumption here produces missing
  data, not an error.
- **Until answered:** the client always sends an explicit offset and **never
  `Z`**, so a naive-comparison backend fails visibly rather than quietly shifting
  everything by the offset.
- **Seam:** the `since` kwarg in `tools/leads.py`, landing at step 4.

### 4.4 A notes index on the service surface `[ARCHITECTURAL]`
- Notes across the tenant, filterable by date range and `author_id`, paged.
- **Releases:** turns a tenant-wide note analysis from ~1,463 sequential calls
  into paging over notes — on the order of 90 calls for 9,000 notes at 100 per
  page, and far fewer for a date-bounded sweep.
- Raise as an **architectural request with the arithmetic attached**, not a
  convenience. It decides whether tenant-wide analytics fits the agreed shape.

### 4.5 Does `/leads/{id}/notes` return timeline events as well as notes?
- The CRM UI shows both in one feed: human notes alongside system events like
  "Feedback updated to Not Interested by X" and "Bulk assigned to sales by Y".
- **If the endpoint returns both**, we would be scoring machine-generated text as
  if a salesperson wrote it.
- Either a filter requirement or a seventh note type. Confirm before the scoring
  prompt is written, or check it directly the moment a key exists.
- **Marker: `ASSUMPTION[Q6]`.** Grep it:
  `src/dodeal_ai/units/structured_intelligence/classify.py` and `schemas.py`,
  `README.md`'s provisional-answers table, and here. **Decided rather than
  waited for:** the seventh note type. `system_event` is a member of `NoteType`,
  the classifier is told to decide it **first** and whatever else the text
  mentions, and a note classified `system_event` is suppressed `not_scorable`
  immediately — never vague-checked, never scored. So the endpoint may return
  both and we are still correct; we do not need the answer to be safe, only to
  know how often it happens. **If the endpoint returns notes only:** the type
  costs one enum member and one branch that never fires, and nothing has to be
  undone. **Verified against the corpus:** the vendored fixture keeps
  `timeline_events` in a separate top-level key from `notes`, and
  `tests/eval/test_structural_eval.py` runs all 27 of them through the pipeline
  to prove each stops after classification with no vague and no score call.

### 4.6 Lower priority, ask when the work needs them
- **User list** — id, name, team, manager, active flag. Releases labels on every
  per-rep output. The UI resolves agent names, so the data exists.
- **`bookedAmount` contract** — type, units, currency, whether null and zero
  differ. Or confirmation that it is unusable.
- **Stored-score read endpoint**, once §5.1 is agreed — filterable by date and
  `author_id`. Releases score history and the improved-after-prompting measure.
- **Per-tenant configuration read** — releases business-changeable thresholds.
  **Premature for a pilot**: runtime config in our own settings is sufficient
  until a tenant needs different weights.
- **Per-tenant DD-API-KEY provisioning.** The seam is built (§8.7,
  `tools/keys.py`, `DODEAL_DD_API_KEYS`) — this is now purely "the backend
  hands us the real keys," not a code change.
- **RS256 public key** — unrelated to the lead/note integration.
- **Rate limiting on `/api/service/*` — UNASKED.** Is there one; is it keyed by
  key, IP or tenant; and what does it return when tripped (429 with
  `Retry-After`, or something else)? Releases the concurrency cap and the
  projected-call ceiling at step 16, and decides whether the tool layer needs a
  429 branch in its error taxonomy at step 4. Worth asking *before* the fan-out
  arithmetic in §6.2 is put to them, since a rate limit changes that number.

---

# 5. PENDING — awaiting a business or product answer

### 5.1 How does the caller authenticate to us, and what does it persist? `[BLOCKS DESIGN]`
Two halves of one conversation, and the first is the single unstated assumption
the whole of Project 1 rests on.

- **Authentication.** We assume the CRM forwards the end user's JWT and the tenant
  Host header, so the request passes Gate 1 and Gate 2 unchanged and `sub` gives
  us the subject for free. **If instead it calls server-to-server with its own
  credential**, that request carries no user, cannot pass Gate 1 as built, and we
  are in exactly the non-request identity problem that forced the daily brief
  on-demand and still blocks Unit B's workers.
- **Persistence.** The CRM stores the returned analysis and score. Agree the shape
  of what it stores, and ask it to expose that back to us later.
- **What this costs while unanswered:** without agreement, Project 1 produces
  judgements that nothing keeps, and every coaching measure over time is impossible.
- **And the other half of the same call: which host does it arrive at?** Gate 2
  requires `Host == <tenant>.<DODEAL_INBOUND_BASE_DOMAIN>` (§1.6). Nobody has
  confirmed whether the CRM calls `<tenant>.dodealcrm.com`, a separate
  `ai.dodealcrm.com`, or something proxied that rewrites `Host` in transit.
  `inbound_base_domain` is already a setting distinct from `backend_base_domain`
  precisely so the answer is configuration and not a code change, and
  `X-Forwarded-Host` is deliberately **not** read until the answer exists —
  trusting a forwarded host before knowing which hop is trusted hands Gate 2 to
  the caller. This travels with the authentication question above: same call,
  same conversation, and both are needed before route skeletons (step 6).
- **Marker: `ASSUMPTION[Q1]`.** Grep it to find every place the assumption is
  load-bearing: `src/dodeal_ai/units/structured_intelligence/pipeline.py`
  (`judge_note`'s docstring), `README.md`'s provisional-answers table, and here.
  **Assumed:** the CRM forwards the END USER's JWT, so the route runs Gate 1 →
  Gate 2 → Gate 4 exactly as the probe route does. **If wrong:** the principal
  source swaps behind the D1 seam. `judge_note` already takes a `TenantScope`
  rather than a `RequestContext`, so the pipeline itself does not change — only
  what builds the scope does, and subjects arriving from a service principal get
  labelled `asserted` rather than `verified`. The rate limit is keyed on that
  subject, so a service principal would collapse every salesperson into one
  bucket: that is the line to re-read first.

### 5.2 Two business lines share one rubric `[DECIDE OURSELVES]`
- The CRM serves **real estate and digital marketing**. The rubric's "deal
  specifics" component (20 points) is real-estate-shaped: budget, property,
  timeline, payment method. A creator or marketing lead has no property and no
  payment plan.
- **Consequence if unaddressed:** a perfectly good marketing note loses 20 points
  for reasons that say nothing about note quality. Every marketing rep scores
  lower than every real-estate rep, permanently.
- **Our position:** keep the five components; make "deal specifics" resolve to a
  different checklist per business line, selected from a lead field. Same 20
  points, different contents. Same mechanism as varying the bar by note type, one
  level up.
- **Still to determine (answerable from the sample, not from them):** which lead
  field identifies the line, and the checklist per line. Where the line cannot be
  determined, fall back to the three universal components and **suppress** the
  specifics weight rather than scoring zero.
- Cheap now, expensive after launch — rescoring history is not something we do.
- **Marker: `ASSUMPTION[Q13]`.** Grep it:
  `src/dodeal_ai/units/structured_intelligence/config.py` (the two fields that
  carry the decision), `README.md`'s provisional-answers table, and here. It is
  also cited in `scoring.py` and in three test modules, which is deliberate —
  the arithmetic changes when it resolves. **Assumed:** no lead field is
  confirmed to carry the business line, so `business_line_field = None` and
  `deal_specifics_applicable = False`; `deal_specifics` is suppressed for every
  type and the denominator is 80 rather than 100. Suppressed is a **state**, not
  a zero — the weight leaves the denominator instead of dragging the total down.
  **If wrong / when answered:** set both fields in `config.py` and nothing else
  moves — `applicable_components` reads them, the denominator becomes 100, and
  `tests/unit/test_scoring.py::Q13_RESOLVED` already pins the resolved
  arithmetic. Past judgements are **not** recomputed; they carry
  `config_version` so a reader can see which rubric produced them.

### 5.3 Evaluation data
- **300-500 real notes**, target 1,000: random not hand-picked, across at least
  10 salespeople, all note types, real language mix, worst notes included,
  untouched by cleaning. *Note samples are in hand; confirm they meet this bar.*
- **50 notes hand-scored** by the standard owner — the calibration reference set.
- **Which tenant?** The guide's test tenant holds 127 notes against ~9,000
  previously described. Confirm the sample's source before building fixtures.
- **Lead samples are not in hand.** Notes without their parent leads cannot
  exercise the deal-specifics component or any lead-level scoring.

### 5.4 Scope: revenue, campaigns, agent KPIs
- All need data not on our surface. Built from what we can read, each would be a
  worse version of a number the CRM already produces correctly.
- **Either** extend the service surface, **or** descope those surfaces in writing.
  Both are workable. Leaving them in an acceptance document nobody can build
  against is not.

### 5.5 Bounds on two open phrases
- The contents of the **recommendation catalogue**, and the list of **tracked
  trend measures**. Both expand indefinitely otherwise.

### 5.6 Written salary firewall
- Confirmation that nothing this unit produces reaches performance, rating,
  target or salary records. Today that is guaranteed by having no write access —
  protection by accident, which disappears the day a write path is granted.

---
## 5.7 Audio governance `[BLOCKS THE SPIKE]`

- May real customer call audio be used for testing, and may it be sent to an external STT provider that may process it outside the country?
- Consent and personal-data question. Ask early — legal answers outlast technical ones, and this gates the spike, which gates the whole unit.
- Fallback if clearance is slow: spike on public call recordings in the same dialect. Weaker — no code-switching in our pattern, no property or area vocabulary, not our line quality — but it answers "is dialect STT viable at all" and lets us shortlist providers.
- Current state: the business has said they may not have real recordings. If they genuinely do not, Unit B ships unvalidated: every acceptance criterion depends on hand-checked real calls. That is a scoping conversation, not an accuracy caveat.

## 5.8 The accuracy metric — define before the spike starts

- Do NOT use word error rate. It misleads badly with dialect and Arabic-English code-switching.
- Use downstream task accuracy: 10-15 calls, hand-record what the summary and objections should say, check the pipeline's output against that set.
- Accuracy targets come FROM the spike, not before it. The business accepted this in writing. Report per language profile — mostly English, mostly Arabic, heavily mixed, other — never one average.

## 5.9 Coaching feedback — owner not named

- Owner must be someone in the business who manages salespeople (sales manager or head of sales), whose judgement reps would accept. Without a named owner the engineer invents the rubric, and an invented standard that judges employees is a real risk.
- Name them before that output is built.

## 5.10 Can the note being judged fall off page one?

- **Marker: `ASSUMPTION[Q8]`.** Grep it:
  `src/dodeal_ai/units/structured_intelligence/pipeline.py` (at `_fetch_note`),
  `README.md`'s provisional-answers table, and here.
- **Assumed:** notes come back newest first, 25 per page, and the note a caller
  is asking about is on **page one** — so one un-paged fetch finds it. In
  practice the CRM calls us right after the note is saved, which is exactly when
  it is newest.
- **Why it is a product question and not only a backend one:** the answer is
  "how many notes does a busy lead accumulate before someone asks us about an
  older one". The endpoint's paging behaviour is confirmed (§1.7); what is not
  confirmed is whether the usage pattern ever reaches past 25.
- **Nothing branches on it.** There is no paging code to take a second path, and
  the note is matched **by id**, never by position (Design A) — so if the note
  is not on page one the caller gets a clean `404 note_not_found`, never the
  wrong note. That is the property worth having: the failure is visible and
  correct, not silent and wrong.
- **If wrong / correction path:** query parameters in `tools/leads.py` at
  **step 4**, not a change in the pipeline. The campaign was explicitly barred
  from adding them (§0.7).
- **Watch for:** a rise in `note_not_found` on a route the CRM only calls with
  ids it has just written. That is this assumption failing, and it is the
  cheapest signal available today.





# 6. PENDING — architectural, ours to resolve

### 6.1 Where the idempotency seen-key check lives
- Redis is the natural candidate but nothing uses it that way yet. Same store as
  the attempt counter and rate limit (§3.2), distinct namespace.
  Also holds Unit B's job state if the background model lands there. See §6.4.

### 6.2 Fan-out: tenant-wide note analytics does not fit the agreed shape

| Operation | Calls | Note |
| --- | --- | --- |
| Full lead sweep | ~15 | 1,448 leads at `per_page` 100. Cheap. |
| Notes for one lead | 1 | More if a lead holds over 100 notes. |
| Full tenant note sweep | ~1,463 | One call per lead. Most leads have no notes and we still have to ask. |
| Same, wall clock | ~4-6 min serial | ~40s at eightfold concurrency. Still does not fit a synchronous request. |

**A correction to an earlier claim.** A sweep does **not** consume ~1,463 units of
our request counter. `gate4_cost` charges once per **inbound** request; outbound
calls to the backend are not counted by it at all. One sweep is one unit of our
counter and ~1,463 calls against **their** API. The concern is real but it belongs
to their rate limits and our latency. A ceiling on outbound calls would be a **new
counter and a new decision**. Either way the bound is the projected call count,
refused before anything is fetched.

**Three ways out, in order of preference:**
1. **§4.4, a notes index** — the conflict disappears entirely.
2. **Bound the scope** — `since` plus the status and feedback filters, so a sweep
   covers tens of leads. Depends entirely on §4.2.
3. **Revisit no-background-work** — a bounded async job with a scheduler and a
   status endpoint. Contradicts a settled constraint. Hold as the fallback if
   §4.4 is refused; do not start speculatively.

**The bad case, worth stating out loud:** if a note does *not* bump `updatedAt`
(§4.2) **and** the notes index is refused (§4.4), then tenant-wide note analytics
costs ~1,463 calls every time and cannot be done synchronously. Present that
combination as the scenario forcing a scope or shape decision — before it is
discovered at build time.

## 6.4 Background execution model — Unit B only

- No longer a shared blocker. The daily brief is on-demand (§3.4 consequence), so Unit A needs no background execution at all. This is off Unit A's critical path.
- Current state: `workers/celery_app.py` is a four-line placeholder — no app instance, no tasks, no queue name, no schedule. Celery is not firmly chosen beyond a docstring. There is no async infrastructure of any kind.
- Blocked on §4.8 — how results reach the backend shapes the whole model. Settle that first or it may need rebuilding.
- Known regardless:
   - Job state persisted outside the worker (queued → running → done → failed → dead-lettered), so a crash or deploy loses nothing.
   - Celery is a sync process model against an async codebase. The boundary must be stated explicitly, not improvised.
   - Two stages, three priority lanes — fast outputs, slow outputs, overnight bulk, plus a priority jump for negotiating leads. That is a queue design decision, not an afterthought.
   - Build it once, for Unit B.
- Seam: `workers/`, plus a named Redis client if job state lives there.

---

# 7. Risks accepted and carried

### 7.1 Masked in the field, unmasked in the text
- The CRM masks phone numbers for callers lacking `show_phone_number`. Our
  credential has no user, so we receive the masked default. **Do not ask for this
  to be lifted — we have no use case.**
- **But note bodies are free text, and salespeople write phone numbers into them.**
  So we may receive, and forward to a model provider, exactly the data the
  permission model withholds in the structured field. Never logging raw note text
  does not cover the provider call.
- Raise deliberately, in writing, rather than at a security review.

### 7.2 Within-tenant exposure is designed-in, not incidental
- The integration guide states it plainly: a service credential has no role, so
  the endpoints return every lead in the tenant, and the tenant boundary is the
  isolation boundary.
- This is **the vendor's stated model**, not a pilot risk we are quietly carrying.
  Any narrowing is ours to build, from the verified subject on the inbound token.
- **Consequences:** any rep can read any lead's notes. Project 2 aggregates expose
  other reps' performance by design.

### 7.3 Analytics invite argument
- A number attached to a person or a lead will be challenged. An unexplainable
  score will not survive that. Hence components always shown, evidence levels,
  and minimum-n before reporting.

### 7.4 Small-sample instability
- Per-rep and per-campaign metrics over thin data swing wildly and read as noise.
  The evidence gate is the answer, not a tuned weight.
### 7.5 Two lead shapes in one CRM
- The `/api/service` surface is **not** the CRM's own lead model. It is a
  hand-built translation over `posts.data`, with its own names and types:
  `leadName`, `leadContact`, `booked_amount`, and **naive local timestamps** with
  no offset — which is what makes §4.3b a real question rather than a pedantic
  one.
- **The consequence, and it is the whole point of this entry:** a field added to
  a lead in the CRM **does not appear on our surface** unless a person edits that
  translation to add it. There is no schema propagation, no automatic passthrough
  and no error. The field is simply absent, and absent looks exactly like a field
  that was never set.
- So "the CRM has that data" and "we can read that data" are different claims,
  and §2 is the register of where they diverge. When a new field is promised, ask
  specifically whether it has been added to the service surface.
- **Carried, not solved:** we cannot detect this from our side. A field we never
  knew about is indistinguishable from one that is null.

### 7.6 Retry multiplication — a money bug, not just a reliability one

- Celery redelivers on worker crash, and `call_with_watchdog` retries once by default. Three job attempts each retrying once is six transcription calls and six bills for one recording. Nothing currently prevents this.
- Mitigate: `retry=False` on provider calls inside a worker, bound the job retries, and make the job idempotent so a redelivery is a no-op.
- Idempotency key for Unit B additionally includes prompt version and model version, so a re-run after a checklist change produces a new analysis rather than silently doing nothing.
---

# 8. BUILT — working, may hold placeholders

### 8.1 Cost gate (Gate 4) + Redis — atomic, metered, fail-open
- Wired into the gate chain. Per-tenant and per-user counters increment together
  in ONE atomic Lua script — both move or neither does.
- **Fail-open:** if Redis is unreachable, the request is allowed and
  `cost_cap_bypassed` is logged at WARNING. Money guard, not a security guard.
- `enforce_cost(tenant, subject, amount=1)` — `amount` is the token hook.
- `get_usage()` reads without incrementing and **fails loud**, not open — a
  reporting read is not a request-blocking decision.
- Placeholder caps: 10000 per tenant, 1000 per user, 86400s window.

### 8.2 Request-id middleware + structured logging
- Sets `request.state.request_id`, echoes it back on the response, and carries
  `RequestObservability` (trace_id today; prompt/model/workflow version
  reserved).
- **The inbound header is validated, not trusted** (audit finding M6). It is
  accepted only if it matches `[A-Za-z0-9._-]{1,128}`; anything else — an
  overlong value, an embedded newline that would forge a second log record, a
  leading `{` — is discarded and a uuid4 generated as if no header had been
  sent. The rejected value is **never logged**, since logging it would put the
  untrusted bytes into the stream the check protects.
- Structured JSON to stdout, `dodeal_ai` tree at `DODEAL_LOG_LEVEL` (default
  INFO) so audit allow lines are not dropped.
- **uvicorn's three loggers** (`uvicorn`, `uvicorn.error`, `uvicorn.access`) have
  their own handlers cleared and `propagate` set back to `True`, so their lines
  reach the same JSON handler instead of landing on stdout as plain text in a
  second format no collector could index (audit finding L6). Their levels are
  left exactly as uvicorn set them: this changes the format, not what is logged.
- Audit fields travel as `extra=` and are merged at the top level by the
  formatter. The formatter no longer parses messages: it used to unpack a
  message that was itself a JSON object, which let any line whose text began
  with `{` forge `decision` / `reason_code` (audit finding M9). The field set,
  levels, and JSON shape on stdout are unchanged.

### 8.3 Global error handler is ASGI-level
- The fail-closed catch-all runs inside Starlette's outermost
  ServerErrorMiddleware, before the gate chain. Deliberate 401/403/429 pass
  through; anything else is logged at ERROR with the request id and returned as a
  generic 500.
- **Why:** Starlette's ServerErrorMiddleware re-raises after handling, which under
  the test client bypasses an installed 500 handler.
- **Middleware ordering has caused two regressions.** Add new middleware alone and
  run the full suite immediately.

### 8.4 LLM seam — one thin interface, gateway concerns behind the factory
- `LLMClient` is a `@runtime_checkable` Protocol with **exactly one method**:
  `async complete(prompt: AssembledPrompt, *, profile: str, max_output_tokens:
  int | None)`. Async because Unit B's prompts are long; structural, so a fake in
  a test and a real adapter satisfy it without inheriting anything.
- `LLMResponse` is a frozen slots dataclass: `text`, `input_tokens`,
  `output_tokens`, `model`, `finish_reason`, `provider_request_id`. Field names
  map one-to-one onto OpenTelemetry `gen_ai.*` attributes, so tracing at step 6
  is a mapping and not a redesign. **`text` is `repr=False`** — it is raw,
  untrusted model output and must not reach a log line by accident.
  `model` is what the provider REPORTS ran, not what config asked for; it becomes
  `model_version` on every stored judgement.
- `FinishReason` has three members (`STOP`, `MAX_TOKENS`, `OTHER`). Adapters
  normalise provider strings into it and unknown values map to `OTHER`, never
  raise.
- Errors: `LLMProviderError(reason, transient=...)` — `str()` is a fixed string
  derived only from the reason, never a provider message, so it is safe in a log
  line. `LLMConfigurationError` is a **`ConfigError` subclass** with the fixed
  message `llm_not_configured`, defined once, in `core/llm/client.py` (it was
  defined twice until fix 6a).
- `get_llm_client()` reads `Settings` lazily — never at import — and raises
  `LLMConfigurationError` unless BOTH `llm_provider` and `llm_model` are set. A
  configured provider raises `NotImplementedError("llm_provider_not_wired")`
  until the adapter lands: loud, never silent.
- **Deliberately NOT on the Protocol:** retries and timeouts (the watchdog, per
  call, `retry=False`), provider routing and fallback (behind the factory),
  quota (Gate 4), validation (`core/validation.py`), cost charging, and the
  model itself — provider, model id and temperature are the profile table's,
  not a caller's.
- **Model profiles are named PER TASK** (Piece M, `core/llm/profiles.py`;
  campaign report R17, refining master document §9.3). `unit_a.classify`,
  `unit_a.vague` and `unit_a.score` are the only names Unit A may pass, and a
  test greps `src/dodeal_ai/units/` to keep it that way. The profile arrives as a
  keyword on the METHOD and not on the factory, because every test injects
  `FakeLLM` through `dependency_overrides[get_llm_client]` and a factory that
  took an argument would break that on day one.
- **The fallback pair serves single-model deployments.** A name absent from
  `DODEAL_LLM_PROFILES` resolves to the `llm_provider` / `llm_model` pair at
  temperature 0 with no ceiling of its own, so running one model everywhere is
  the configuration that already exists and profiles are opt-in. An unknown name
  with no pair configured raises the same `LLMConfigurationError`
  (`llm_not_configured`) the factory does.
- **The ceiling rule:** a profile's `max_output_tokens`, when set, is used only
  if it is LOWER than the task's own ceiling (64 / 1024 / 256), never higher, and
  the effective ceiling is what a `MAX_TOKENS` finish is judged against. The task
  constants are sized against the longest answer each task can produce; a profile
  that raised one would buy room the task has no use for.
- **UNCONFIRMED, and deliberately so:** no profile is configured anywhere in this
  repo, no adapter reads one, and nothing here has run against a real provider.
  The resolution and the call-site discipline are what Piece M built; item 76's
  adapter is what consumes them.
- **Seam:** `core/llm/client.py`, `core/llm/profiles.py`, `core/llm/__init__.py`.

### 8.5 AssembledPrompt — the caching and trust boundary in one type
- A frozen slots dataclass with three fields: `stable` (the trusted system
  template, byte-for-byte from a versioned file in `prompts/`), `variable` (the
  delimited CALLER DATA section, delimiters included, **`repr=False`** because it
  is untrusted text), and `tail` (a trusted trailing instruction rendered AFTER
  the data, reserved for the step 10 stricter reprompt; empty today and
  `build_prompt()` does not populate it).
- `.text` renders `stable` + `variable` [+ `tail`]. With an empty tail it is
  **byte-identical** to what `build_prompt()` returned before the type existed,
  and a test guards exactly that.
- The ordering is the caching shape (stable prefix, variable suffix,
  FUTURE_PATTERNS item 4) and the injection boundary at the same time — one
  ordering serving both. Adapters read `.stable` and `.variable` for a cache
  breakpoint and **never re-split `.text`**; re-splitting would move the
  injection boundary out of the one module that tests it.
- Caller text that contains either delimiter is rewritten to
  `[filtered-delimiter]`, so a caller cannot forge an early END marker to escape
  the data region.
- **Seam:** `core/prompting.py`.

### 8.6 Packaging — schemas and prompts ship inside the wheel
- Audit finding F1: the wheel used to contain only `src/dodeal_ai`; `schemas/`
  and `prompts/` lived at the repo root, so an installed copy raised
  `ModuleNotFoundError` on `tools/leads.py` and `core/prompting.py` could not
  find its templates. It only worked in the repo because pytest sets
  `pythonpath = ["."]` and the dev install is editable.
- CI installs the built wheel into a clean venv and imports every module
  (`scripts/verify_wheel.py`); the Dockerfile installs non-editable so the
  running container proves the same thing.
- `DODEAL_PROMPTS_DIR` overrides the prompt location for local iteration only.

### 8.7 Backend key resolver — per-tenant, fail closed
- Audit finding F2: `dd_api_key` was ONE key for every tenant, with a
  placeholder default — unworkable once a second tenant is provisioned (the
  backend's contract is a key valid only against its own tenant host), and a
  misconfigured deployment could send the literal placeholder to the real
  backend with nothing failing closed.
- `TenantKeyResolver` Protocol, `SettingsKeyResolver` from `DODEAL_DD_API_KEYS`
  (a JSON map, no default for any tenant). An unknown tenant fails closed
  (`BackendKeyError`) before any call is made — never retried, never wrapped
  by the watchdog. Startup logs `backend_keys_missing` at `ERROR` when the map
  is empty, without refusing to start (the gate chain and `/ready` must work
  before a key is provisioned).
- The key is read out of its `SecretStr` in exactly one place:
  `LeadsClient._headers()`.

### 8.8 Tenant label rule — one definition, both boundaries
- Audit findings H4 and H6. The tenant arrives twice — as the `subdomain` claim
  and in the `Host` header — and both are validated by the SAME rule, defined
  once: `claims.TENANT_LABEL_RE`, a single DNS label (1–63 chars, `[a-z0-9-]`,
  no leading or trailing hyphen), lowercased.
- Gate 2 compares the **whole host**, not its first label. `tenant_from_host()`
  requires the host to equal exactly `<label>.<base_domain>`, tolerating one
  trailing dot and one `:port`, rejecting bracketed IPv6 literals outright, and
  rejecting a label containing a dot of its own. Comparing only the first label
  is what let `<tenant>.evil.com` through.
- Three distinct reason codes, all a generic 403 to the client:
  `invalid_tenant_claim` (claim present but not a valid label),
  `invalid_host` (wrong domain, no subdomain, an extra subdomain level, an IPv6
  literal, or absent), `tenant_mismatch` (valid shape, different tenant).
- `TenantMismatchError` carries the reason code **only** — never the host or the
  claim value.
- Property-based tests (hypothesis) cover both parsers.
- **Seam:** `core/tenancy.py`, `core/auth/claims.py`.

### 8.9 JWT clock skew and RS256 readiness
- `jwt.decode` is called with `leeway=jwt_leeway_seconds`
  (`DODEAL_JWT_LEEWAY_SECONDS`, default 30 — **never set it to 0**), so ordinary
  drift between the CRM's clock and ours is not read as an attack.
- Skew failures get their **own** reason codes, distinct from `invalid_token`, so
  drift is diagnosable in the audit log rather than looking like forgery:
  `token_expired`, `token_not_yet_valid` (iat/nbf in the future beyond the
  leeway), `invalid_iat` (present but not a number). Also distinct:
  `missing_required_claim` and `bad_algorithm`. `invalid_token` is the catch-all,
  ordered last.
- The algorithm comes from config and is passed as an explicit allow-list, so
  `alg:none` is rejected. `iss` and `aud` are not verified (the token carries
  neither, §1.4); `exp` is required and verified; PyJWT's `sub` check is disabled
  because Tymon's `sub` is an integer (§1.5).
- **RS256 is ready on our side:** `pyjwt[crypto]` is a runtime dependency, and
  the RS256 round-trip plus an alg-confusion test live in
  `tests/security/test_verify.py`. The switch is
  `DODEAL_JWT_ALGORITHM=RS256` + `DODEAL_JWT_SIGNING_KEY=<public PEM>`, with no
  code change. Still waiting on the backend's public key (§1.2, Q3).
- **Seam:** `core/auth/verify.py`, `core/config.py`.

### 8.10 Log safety — content cannot reach a log line
- The rule (§3.3) is enforced by code in three places, not by discipline:
  - `core/log_safety.py::safe_error_fields()` returns `error_type` and
    `error_module` for ANY exception, and the message (`error`) **only when the
    class is defined under `dodeal_ai.`** — because our own exceptions build
    messages from a fixed vocabulary while a foreign exception's message is
    frequently the data that failed. `cause_type` names the chained class;
    the cause's message is never included.
  - `frames_only()` returns traceback FRAMES alone — no exception line, no
    chained "During handling..." section. `format_exception` would walk
    `__cause__`/`__context__` and print every message in the chain.
  - `core/validation.py` raises `OutputValidationError` **`from None`**. It
    carries a label, a count, and `(dotted location, pydantic error type)` pairs
    — never the `ValidationError`, its messages, or `input_value`. Chaining it
    would carry the rejected content into any traceback formatted downstream.
    Deliberate: do not "restore" the chain.
- The JSON formatter never parses a message. Structured fields reach the output
  one way only — `extra=` attributes merged at the top level. An earlier version
  unpacked a message that was itself JSON, so any line beginning with `{` could
  forge `decision` or `reason_code` (M9).
- Sentinel tests in `tests/security/test_log_safety.py` fail if raw content
  reaches a line. They bind the sentinel on a non-raising line, so the traceback
  frame's own source text cannot be mistaken for a leak.
- **Seam:** `core/log_safety.py`, `core/validation.py`, `core/logging_config.py`.

### 8.11 Unit A Project 1 — built against FakeLLM and the fake CRM, nothing `[V]`

- **Nothing in this unit has been verified against a real dependency.** Not one
  judgement has been produced by a real model, and not one note has been read
  from a real CRM. Every claim in §3.4, every number in the report, and every
  test in the suite is a claim about behaviour against
  `tests/helpers/fake_llm.py` and `tests/fixtures/fake_crm/tenant-a.json`. The
  `[V]` marker is not used anywhere in this section and must not be until a real
  provider and a real backend key have both been exercised.
- **The model.** `get_llm_client()` still raises. `FakeLLM` is injected through
  `app.dependency_overrides` and the factory has **no test switch** — adding one
  was forbidden for the whole campaign and remains the rule. What this means in
  practice: prompts have never been read by a model, so nothing is known about
  how any real model responds to them. The reprompt rate, the malformed-output
  rate and the per-pass token cost are all **unmeasured**. Step 16 is where they
  first become observable; step 18 is where quality is first measurable
  (`tests/eval/test_quality_eval.py`, skipped until `DODEAL_EVAL_REAL=1`).
- **The CRM.** The vendored corpus is generated, not exported. It is
  **127 notes carrying only 57 distinct texts** across 19 leads, plus 27
  `timeline_events` and 1448 leads — one of which (`1661`) does not satisfy
  `Lead` and is skipped and counted by the loader. So the corpus proves the
  pipeline *survives* real-shaped text; it proves nothing about the real
  distribution of note types, lengths or languages.
- **What IS established, and is worth keeping:** the pipeline runs every one of
  the 127 notes to a `Judgement` or a `Suppressed` state and never an exception
  (`tests/eval/test_structural_eval.py`); a note cannot forge a delimiter or
  supply its own marks; a model cannot return a band or a total; and no note text
  or model output reaches a log line on any path
  (`tests/security/test_unit_a_injection.py`).
- **Seam:** `core/llm/client.py` (`get_llm_client`), `tests/helpers/fake_llm.py`,
  `tests/helpers/fake_leads.py`.

---

# 9. PARKED

### 9.1 Gate 3 — permission enforcement
- The token carries no roles, so the model is undecided. **Confirmed (§1.10):**
  the backend does no role filtering, so any per-role scoping is ours to build.
  That narrows what Gate 3 must decide; it does not resolve the role table.
- The live chain ends at auth + tenancy. `resolve_permissions` /
  `require_permission` remain unit-tested in isolation. Identity and
  RequestContext keep an empty `roles` field so shapes stay stable.
- Role table is a placeholder (`agent`, `viewer`); ~9 real roles expected.
- **How to correct:** replace the dict with real roles **and** wire the gate in —
  both together, so a real user is not under- or over-granted.

---

# 10. DEFERRED

### 10.1 Input size limit — home undecided
- Built once as ASGI middleware, removed after a regression (forced eager config
  load; middleware-ordering conflict).
- Re-add **last**, alone, run the full suite immediately, watch the
  error/chain/audit tests. Read the cap **lazily** — never call `get_settings()`
  in middleware `__init__`.
- **The in-app limit is not coming back.** The edge limit is DevOps's (Q21), and
  a byte cap belongs where bytes are first accepted, not one layer inside the
  app that has already read them. **Load shedding is a different guard** — a
  count of requests in flight, not a size of one — and it lives in
  `middleware/inflight.py` (Piece L, register item 73), where it does obey the
  lazy-read rule this section wrote down.

### 10.2 Per-unit prompt-injection hardening
- **Built:** the injection-resistant prompt builder and its structural tests.
  Generic, reused by every unit.
- **Deferred:** each unit's real prompt and its task-specific adversarial tests.
- **Caveat:** the delimiter boundary is defence-in-depth, not a guarantee. No
  scheme makes a model fully injection-proof.

### 10.3 Lead-list query parameters — not implemented
- `page`, `per_page`, `since`, `feedback`, `leadStatus`, `leadSource` are all
  documented and none are wired into `get_leads()` today.
- **Now on the critical path** for any bulk read. Add as keyword arguments
  forwarded as query params.

### 10.4 Per-endpoint error handling for get_lead / get_lead_notes
- 404 and 422 currently fall through the watchdog into a generic
  `ExternalCallError`, like any transport failure.
- If a caller needs "not found" distinguished, add it deliberately in
  `tools/leads.py` — not by weakening the watchdog's fail-closed default.

### 10.5 Provenance on every figure
- Recording which endpoint or formula produced each number is right eventually.
  With one data source and pre-pilot scale it is ceremony — the version stamps
  already answer "which rules". **Add when there is more than one source.**

---

# 11. Deliberate decisions — DO NOT reverse

- Gate 3 parked (§9.1).
- PyJWT `verify_sub` disabled — integer sub (§1.5).
- Cost gate enforcement fails **open**; `get_usage` fails **loud** (§8.1).
- Auth and tenancy fail **closed**.
- The error handler catches broad `Exception` on purpose (§8.3) — deliberate at
  the ASGI boundary **only**, never a pattern to copy into a worker.
- `.env` holds only a throwaway local signing key, gitignored. Real keys come via
  a secret manager. **UTF-8 with NO BOM** — a BOM corrupts the variable name on
  Windows.
- `LeadNote` is modelled stricter than `Lead`: only `author` is nullable. The
  "all optional except id" instruction was scoped to leads and not repeated for
  notes. Do not loosen without a confirmed reason.
- Tenant labels are validated and lowercased at the boundary; do not loosen the
  regex to admit a value — fix the token instead.

---

# 12. Watchdog retry vs non-idempotent writes

- Retry-once is safe for **reads**. A non-idempotent **write** could
  double-execute.
- Also dangerous for a **paid call that may already have completed** — you pay
  twice for one result. This is why LLM calls use `retry=False`.
- **Seam:** `core/resilience.py::call_with_watchdog` accepts `retry=False` and a
  per-call `timeout=`. Both already exist — an earlier plan wrongly claimed the
  timeout override needed building.
- **Never raise the global `external_call_timeout_seconds`** — its 10s default is
  sized for a CRM fetch and weakening it weakens every external call.

---

# 13. Placeholder config values

- Watchdog: `external_call_timeout_seconds = 10.0`, `external_call_retry_once = True`.
- Redis client timeouts: `socket_connect_timeout=2.0`, `socket_timeout=2.0` on
  both clients — **still hardcoded, not config-driven**. This is the last
  hardcoded operational value left in the service. It is promoted to `Settings`
  at **step 3**, together with the breaker that audit finding H3 asks for; until
  then a Redis outage costs 2.0s per request in the threadpool and the `/ready`
  ping can exceed a default Kubernetes probe timeout.
- Backend client: `backend_base_domain = "dodealcrm.com"` (still a placeholder;
  real hosts come from DevOps).
- `redis_operational_url` — the **db2** connection Unit A's three state concerns
  use (idempotency, the clarification rate limit, the attempt counter). Distinct
  from the cost gate's client on purpose: different database, different failure
  policies, and a Gate 4 outage must not take the judgement route's idempotency
  with it. **`/ready` does not yet report db2** — it pings the cost client only,
  so a service whose operational store is unreachable currently reports ready and
  then denies every judgement with `503 idempotency_unavailable` (fail closed, by
  design — see §3.4). Both the `/ready` probe and this client's socket timeouts
  are **step 3**, together with the hardcoded timeouts above.
- `dd_api_keys`: per-tenant map, **no default, fail closed** (`tools/keys.py`).
  The single shared `dd_api_key` with a placeholder default is **gone** — removed
  in fix 2, audit finding F2. There is no placeholder credential anywhere in the
  service now: an unconfigured tenant raises `backend_key_missing` before any
  network call rather than sending a literal placeholder to a real host.
- **Rotation.** All of the above are read through `Settings`, which
  `get_settings()` caches for the process lifetime and which is frozen. There is
  no hot reload: **rotating any secret means a deploy.** Procedure, blast-radius
  search terms and who does what: `docs/runbooks/secret-rotation.md`. Record each
  rotation here, beside the entry for the secret that moved.

---

# 14. Housekeeping

- Check in the integration guide's sample payloads as fixtures, tagged `[D]`, with
  the guide's date recorded beside them.
- Record the sample discrepancy: 127 notes in the tested tenant vs ~9,000
  described. Confirm which tenant the evaluation sample comes from.
- ~~**Delete `study.py`**~~ — **done in fix 6a** (`git rm study.py`,
  commit `e138149`). The README file-reference row for it went with it.
- **Do NOT delete `_probe.py` yet** — it is mounted in `main.py` and
  `tests/security/test_chain.py` and `test_exit_demo.py` depend on it. It goes
  when a real feature route replaces what those tests exercise.
- Note that "the README" in earlier planning documents refers to the **Laravel CRM
  backend's** README, not ours. Every conclusion drawn from it is withdrawn — it
  describes what exists, not what our credential can reach.
- **Our README no longer refers to `.env.example` as missing.** It was added in
  fix 1 (audit finding M8) and audited field-by-field against
  `core/config.py::Settings` in fix 6b — every `DODEAL_*` field is present, in
  source order, with its default or `change-me-local-only` for a secret.

# 15. Review questions for the Unit B engineer

Ask these directly when he presents his design. Clear answers mean he understands the failure modes; hesitation shows where to focus review.

- **Which async runtime, and why not Celery?** — ask this; D2 is accepted
  (`docs/decisions/0001-principal-model-and-execution-model.md`; `ACCEPTED` in
  `docs/STATUS.md` §2). It **replaces** the older "where is the sync/async
  boundary" question rather than joining it, because under D2 there is no
  boundary left to place. The recorded answer: workers run the *same* async code
  under an async-native runner — `arq` (a Redis Streams consumer we own is the
  fallback) — and `celery_app.py` is deleted rather than filled in. One execution
  model, not three. Celery's cost was never its feature set: a fork-based sync
  worker against an async codebase buys one Redis client shared across forked
  processes, a sync-to-async bridge at every seam, and retry multiplication on
  top of `call_with_watchdog` — six paid provider calls for one recording
  (§7.6), a money bug and not merely a reliability one. A good answer names what
  the two-stage / three-lane design maps onto (arq queues or Streams consumer
  groups), shows nothing in the Unit B plan depends on a Celery-only feature,
  and still says where the event loop lives per task and what keeps a blocking
  call from landing inside async code.
- How many provider calls does one failing job actually make? See §7.6.
- What does a job record look like when the model returns malformed output? Watch for `json.loads` on the raw reply, and for a broad `except: pass` making a failed job look successful. The broad except in `core/errors.py` is deliberate at the ASGI boundary only.

The hard gate: the STT spike recommendation is written and accepted before any pipeline code merges.
