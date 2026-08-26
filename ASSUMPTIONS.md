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

### 1.3 Claim names — sub / subdomain / database `[T]`
- The token carries `sub` (user id, INTEGER), `subdomain` (tenant), `database`,
  plus iat/exp (and nbf/jti/prv). **No role or permission claims.**
- **Seam:** `core/auth/claims.py`, names read from `core/config.py`.
- **How to correct:** set `DODEAL_CLAIM_SUBJECT` / `_SUBDOMAIN` / `_DATABASE`.
  A rename is config, not code.

### 1.4 No iss, no aud `[T]`
- Neither claim is present. Those checks are removed. `exp` is verified.

### 1.5 Integer sub handling `[T]`
- PyJWT rejects a non-string `sub`. Tymon's is an integer, so PyJWT's check is
  disabled (`verify_sub: False`) and the claim layer normalises it instead.
- **DO NOT re-enable PyJWT's sub verification.** It would reject valid tokens.

### 1.6 Tenant — subdomain, authoritative, Host must match `[T]`
- The tenant is the `subdomain` claim and it is authoritative. The request also
  arrives at `<subdomain>.dodealcrm.com`; the Host subdomain must match the token
  subdomain or the request is denied (403).
- Isolation is enforced backend-side by tenant database, keyed on subdomain. Lead
  records carry no tenant field, so the re-check is a subdomain match.
- An absent or subdomain-less Host is a hard 403 (deliberate). `localhost` fails
  Gate 2; local testing needs a tenant Host header.
- **Seam:** `core/tenancy.py`, `gate2_tenant` in `core/auth/dependencies.py`.

### 1.7 The service surface — three read endpoints only `[T]`
Base `https://<tenant>.dodealcrm.com/api/service`, DD-API-KEY header only.

| Endpoint | Behaviour |
| --- | --- |
| `GET /leads` | Paginated, newest first. `page`, `per_page` (default 25, max 100, 422 above), `since` (ISO-8601 on `updatedAt`), `feedback`, `leadStatus`, `leadSource` (exact match). |
| `GET /leads/{id}` | Single lead. 404 if absent in this tenant, 422 if id not numeric. |
| `GET /leads/{id}/notes` | That lead's notes, newest first. `page`, `per_page`. Empty is `200` with `data: []`, not an error. |

- **There is no notes index and no filter by rep.** Notes are reachable only one
  lead at a time. This drives the fan-out problem in §6.2.
- **Envelope:** `{status, data, meta}` with `meta` carrying `current_page`,
  `per_page`, `total`, `last_page`. `data` holds the array directly.
  Earlier `posts.data` and `{success, message, data, meta}` assumptions were both
  wrong and are withdrawn.
- **Note:** the envelope key is `status`, which is also a lead field name. The
  parser is explicit about which is which and a fixture covers both.
- **Seam:** `schemas/lead.py`, `tools/leads.py`.

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
| Roles, permissions, hierarchy | Gate 3 at the source, per-rep labels, every manager view. |
| User and agent lists | Turning `assignedToSales` and `author_id` into names. The CRM UI resolves them, so the data exists internally. |
| Deals, transactions, invoices, commission | Revenue analysis in any form. |
| Agent metrics, leaderboards, targets | Consuming rather than recomputing agent performance. |
| Campaigns, campaign contacts, ad spend | Campaign performance. **Note: the CRM UI shows Campaign and URL fields on the lead**, so campaign data exists — it is simply not on our surface. |
| Deal history, lead cycle, dispatch events | Pipeline trends, stage velocity, speed-to-first-contact. |
| Call logs, meetings, reminders, activities | Corroborating a claimed next step; the Recordings tab confirms recordings live against leads. |
| Tenant configuration store | Business-changeable weights and thresholds without a release. |

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
| **Schemas** | Unit-owned schemas live in the unit, not root `schemas/`. The service response envelope is a root contract; our result types are not. |

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
| **Logging** | Client responses are generic, from a fixed enumerated set with no interpolation. Raw note text and model output are never logged. |
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
- **Per-tenant DD-API-KEY provisioning.**
- **RS256 public key** — unrelated to the lead/note integration.

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
### 7.5 Retry multiplication — a money bug, not just a reliability one

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
- Reads or generates `X-Request-ID`, sets `request.state.request_id`, echoes it
  back, carries `RequestObservability` (trace_id today; prompt/model/workflow
  version reserved).
- Structured JSON to stdout, `dodeal_ai` tree at `DODEAL_LOG_LEVEL` (default
  INFO) so audit allow lines are not dropped.

### 8.3 Global error handler is ASGI-level
- The fail-closed catch-all runs inside Starlette's outermost
  ServerErrorMiddleware, before the gate chain. Deliberate 401/403/429 pass
  through; anything else is logged at ERROR with the request id and returned as a
  generic 500.
- **Why:** Starlette's ServerErrorMiddleware re-raises after handling, which under
  the test client bypasses an installed 500 handler.
- **Middleware ordering has caused two regressions.** Add new middleware alone and
  run the full suite immediately.

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
  both clients — **hardcoded, not config-driven**. Promote to `Settings` if they
  need tuning without a code change.
- Backend client: `dd_api_key = "test-dd-api-key"`, `backend_base_domain = "dodealcrm.com"`.
- **`dd_api_key` fail-closed treatment is now urgent, not tidy** — DD-API-KEY *is*
  the credential for every data call, and it still carries a placeholder default
  unlike the signing key.

---

# 14. Housekeeping

- Check in the integration guide's sample payloads as fixtures, tagged `[D]`, with
  the guide's date recorded beside them.
- Record the sample discrepancy: 127 notes in the tested tenant vs ~9,000
  described. Confirm which tenant the evaluation sample comes from.
- **Delete `study.py`** — personal notes, referenced nowhere.
- **Do NOT delete `_probe.py` yet** — it is mounted in `main.py` and
  `tests/security/test_chain.py` and `test_exit_demo.py` depend on it. It goes
  when a real feature route replaces what those tests exercise.
- Note that "the README" in earlier planning documents refers to the **Laravel CRM
  backend's** README, not ours. Every conclusion drawn from it is withdrawn — it
  describes what exists, not what our credential can reach.

# 15. Review questions for the Unit B engineer

Ask these directly when he presents his design. Clear answers mean he understands the failure modes; hesitation shows where to focus review.

- Where exactly is the sync/async boundary, and how is the event loop managed per task? Celery workers are sync processes; this codebase is async. Watch for blocking calls inside async code, and for one network client shared across forked processes.
- How many provider calls does one failing job actually make? See §7.5.
- What does a job record look like when the model returns malformed output? Watch for `json.loads` on the raw reply, and for a broad `except: pass` making a failed job look successful. The broad except in `core/errors.py` is deliberate at the ASGI boundary only.

The hard gate: the STT spike recommendation is written and accepted before any pipeline code merges.
