# FUTURE PATTERNS — Adopt When You Reach That Phase

Patterns from production LLM-engineering references that are worth adopting.
None of these are needed for the foundation itself. Each is tagged with WHEN it
becomes relevant, so it is not built too early and not forgotten when the time
comes.

The rule: do NOT build these now. Read the matching entry when you start the
phase named in "When."

---

## 1. Idempotency keys

**When:** any request that must not be processed twice — today the note-scoring
request (tenant + operation + item id + content fingerprint, ASSUMPTIONS §3.2);
a CRM write-back does not exist and is not planned.

**Why:** our watchdog retries once on failure. A read is safe to retry. A WRITE
is not — a retry could create the same note or score twice. ASSUMPTIONS.md
already flagged this risk; this is the fix.

**Pattern:**
- Build an idempotency key from stable inputs, e.g.
  `idempotency_key = hash(user_id + operation_type + business_object_id)`
  (for a note: `hash(sub + "note_write" + lead_id)`).
- Send it with the write so a repeat with the same key is a no-op, not a
  duplicate.
- OR make the write itself idempotent (upsert by a natural key) so retrying is
  safe.
- Seam: wrap writes with `retry=False` in `core/resilience.py` until the write
  endpoint supports an idempotency key, then switch retry back on.

**Rule to hold:** model output selects the action; deterministic, authenticated
code executes it. The model never directly authorizes an irreversible write.

---

## 2. Lethal trifecta — prompt-injection containment

**When:** Unit C1 (the conversational assistant) and Unit C2 (sales automation) —
any feature that both reads untrusted content AND can act/send externally.
NOT needed for note scoring (no external egress).

**Why:** "separate instructions from data" is necessary but not sufficient —
models cannot reliably tell injected instructions inside data apart from real
ones. Filters get bypassed in shipped products. So containment must be
architectural, not filter-based.

**The rule (Simon Willison's lethal trifecta):** an agent that combines all three
can be tricked into exfiltrating data —
1. access to private data,
2. exposure to untrusted content,
3. the ability to communicate externally.

Break at least ONE leg by architecture. Example: the component that reads
untrusted content (a lead note, an inbound WhatsApp message) does NOT also hold
privileged tools or open outbound egress.

**Practical for us:**
- The note-scoring path reads untrusted note text but has no external egress →
  one leg already broken. Keep it that way.
- When the assistant can both read untrusted content and send messages, split
  those responsibilities so no single component holds all three legs.
- Everything influenced by untrusted input is itself untrusted — including the
  model's own output. Validate it before it can trigger any tool.
- Security controls live in deterministic, auditable code outside the model. A
  system prompt is never a security boundary.

---

## 3. Model gateway

**When:** the first time we make a real LLM call (Unit A). Grow `core/llm/` into
this rather than scattering provider logic across units.

**Why:** one place to centralize provider routing, fallback, key management,
pinned model versions, prompt caching, timeouts, circuit breakers, and
per-tenant quotas. Scattering provider calls through feature code is the mistake
this avoids.

**What it centralizes:**
- Provider routing + fallback (primary model down → fallback model).
- Pinned model versions (treat a provider "upgrade" as a change to be evaluated).
- Prompt caching (see item 4).
- Timeouts + circuit breakers on the model call.
- Per-tenant quota enforcement (ties directly to our cost gate).

**Seam:** `core/llm/` already exists as the provider abstraction — this is its
mature form. Keep the watchdog (`core/resilience.py`) wrapping the model call.

---

## 4. Prompt caching (a direct cost lever)

**When:** as soon as real prompts exist (Unit A), and revisit for every feature.
Directly relevant to the CEO's cost/credit concern.

**Why:** providers discount cached prompt tokens heavily (documented up to ~90%
cost and ~85% latency reduction on long stable prefixes). This is often the
single largest cost lever in an LLM system.

**Pattern:**
- Structure every prompt as: STABLE PREFIX (system prompt, tool defs, reference
  docs) + VARIABLE SUFFIX (the specific note/lead/task).
- The stable prefix is cached and reused across requests; only the variable part
  is paid for at full price.
- Our server-side prompt builder (`core/prompting.py`) already assembles prompts
  from versioned files — structure them prefix-first so caching works.

**Related cost levers (note for later):**
- Model routing/cascade: default to a small model, escalate to a large one only
  on hard cases.
- Batch API for offline work (e.g. Unit B call analysis) — typically ~50%
  cheaper.
- Token-efficient tool responses and context compaction cut spend directly.
- Track spend per route, per tenant, per feature; alert on anomalies.

---

## 5. Reliability patterns for the async pipeline

**When:** Unit B (call-recording analysis) — the async Redis + Celery pipeline.
NOT needed for the synchronous note-scoring path.

**Why:** a slow, async, at-least-once pipeline needs failure handling a fast
synchronous call does not.

**Adopt when building Unit B:**
- **Dead-letter queue (DLQ):** a job that keeps failing goes to a DLQ, not an
  infinite retry loop.
- **Idempotent jobs:** a recording processed twice must not double-write results
  (ties to item 1). Key the job on `recording_id`.
- **Circuit breaker on the LLM/transcription call:** stop hammering a failing
  provider; fail fast and recover.
- **Job state modelling:** explicit states (queued → running → done → failed →
  dead-lettered) persisted outside the worker, so a crash/deploy can resume.
- **Compact error feedback:** on a tool/transcription failure, feed a short error
  summary back, with a counter that escalates after N consecutive failures.

---

## 6. Evaluation from real examples

**When:** building any feature (Unit A first). This is what the engineers'
current prep (collecting 10–15 real notes) feeds into.

**Why:** you cannot trust a feature's output without a way to measure it. Small,
real eval sets catch most regressions — no need to wait for a perfect benchmark.

**Pattern:**
- Start with ~15–20 real examples, hand-inspected.
- Each example defines expected behavior + must-not-do + success criteria.
- Grow the set from production: corrections, misses, edge cases, injection
  attempts, permission-boundary tests.
- Layer graders: cheap deterministic checks (schema, cost, latency) everywhere;
  LLM-as-judge for fuzzy qualities (calibrate against human labels, control for
  position/verbosity/self-preference bias); human review for high-stakes.
- Run evals on every meaningful change (prompt, model version, tool schema) and
  gate deploys on them. Convert every production incident into a regression case.

---

## 7. Observability — IDs to propagate

**When:** before a pilot. Extends the audit log we already have.

**Why:** to trace one request across async hops and diagnose non-deterministic
failures, every log/trace should carry a consistent set of identifiers.

**Propagate end to end:**
`request_id · trace_id · conversation_id · user_id · tenant_id ·
prompt_version · model_version · workflow_version`

- We already propagate `request_id` (currently "unknown" until request-id
  middleware is built — see ASSUMPTIONS.md). Add the rest as features introduce
  them.
- Monitor four layers plus unit economics: availability (error rate,
  circuit-breaker state), performance (P50/P95/P99, queue depth), cost (tokens,
  cache hit rate, **cost per successful task**), quality (task success,
  escalation rate, feedback).

---

## 8. OWASP LLM Top 10 — the security checkpoint

**When:** it is a CHECKLIST you run at gates, not something you build once. Three
moments —
1. **Before each feature ships** (Unit A first): ask "which of the 10 apply to
   THIS feature?" and confirm each is handled.
2. **Heaviest use before the assistant (Unit C) ships:** the agent is where most
   of these risks live.
3. **A full pass before the pilot** (our Phase 4 hardening): go through all 10
   systematically as the security sign-off.
Plus: re-check the relevant ones on every meaningful change (new tool, prompt,
or feature) — especially injection (LLM01) and output handling (LLM05).

**Why:** these are the 10 most common ways LLM apps get compromised. Running the
list at each gate catches the class of bug that does not show up in normal tests.

**What we ALREADY cover structurally (no formal pass needed yet):**
- LLM10 unbounded consumption → the cost gate.
- LLM05 improper output handling → the validation layer (model output is
  untrusted input to everything downstream).
- LLM01 prompt injection → the server-side prompt builder treats input as data.
- LLM02 sensitive-info disclosure → tenant isolation + no secrets/PII in logs.
- LLM07 system-prompt leakage → no secrets or auth logic in prompts.

**The ones ranked for OUR service (read-only note/call, not the agent yet):**
1. **LLM01 prompt injection** — note/call text is untrusted; most relevant.
2. **LLM10 unbounded consumption** — cost gate (building now).
3. **LLM05 improper output handling** — validate model output (built).
4. **LLM02 sensitive-info disclosure** — never leak across tenants (isolation).
5. **LLM07 system-prompt leakage** — assume the prompt is public; keep it clean.

**Agent-heavy ones — defer to Unit C:** LLM06 excessive agency (minimal tools,
minimal permissions, human confirmation for high-impact actions), LLM08 vector/
embedding weaknesses (only if we add retrieval). These pair with the lethal
trifecta (item 2).

**The one rule under all of them:** security controls live in deterministic,
auditable code OUTSIDE the model. A system prompt is never a security boundary.

---

## What to explicitly IGNORE (not our world)

- **Self-hosted inference** (vLLM, quantization, KV cache, GPU serving) — only
  relevant if we run our own models. We call a provider API, so this does not
  apply. Skip entirely.
- **RAG / retrieval / vector-store ACLs** — only if we add retrieval. Not in
  scope now.
- **Agent state machines / checkpoint-resume / pause-for-human** — only for the
  long-running agent (Unit C2). Note scoring and call analysis are single-shot;
  they do not need this.

---

## The through-line

Most of these confirm the foundation is built the right way — budgets, timeouts,
retry discipline, output-as-untrusted, deterministic controls outside the model.
The genuinely new adoptions are: idempotency keys (item 1, fixes a flagged gap),
the lethal trifecta (item 2, for the assistant), and prompt caching (item 4, a
cost lever). Everything else is right-thing-at-the-right-phase, not now.
