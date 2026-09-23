# OWASP LLM Top 10 (2025): Unit B checkpoint

Unit B (call intelligence) takes a recorded sales call, transcribes it and returns stage 1 (the transcript) to the CRM through a signed callback. This note checks Unit B against each item of the **2025** OWASP Top 10 for LLM Applications. For each item it records whether the item applies, the control, the file that holds it, and either the test that proves it or the batch that will build it.

**State at this checkpoint (branch `call-intelligence`, Unit B foundation batch).** No real speech-to-text adapter exists yet. `build_transcriber` refuses to build one, and the only transcriber is `FakeTranscriber`. No analysis pass exists either (summary, scoring, alarm phrases, number detection, voice ID, prosody), so **no call content reaches a language model today**. Where a control belongs to a pass that has not been built, the paragraph says so and names the batch that owes it. "Not built" means exactly that; it is not a claim that the risk is absent.

Scope: `src/dodeal_ai/units/call_intelligence/`, `src/dodeal_ai/api/routes/calls.py`, `src/dodeal_ai/workers/calls.py`, and the Unit B parts of core: `core/jobs.py`, `core/audio_download.py`, `core/callbacks.py`, `core/cost/limiter.py` (the calls budgets) and `core/cost/spend.py`.

---

## LLM01: Prompt injection

**Applies once the analysis batch lands; nothing is exposed today.** A caller controls what is said on a call, so every word of a transcript is attacker-shaped text in the same way a note is for Unit A. Today no transcript is put into any prompt: stage 1 stores and delivers the transcript and calls no model. The control the analysis batch must reuse already exists and is tested for Unit A. `core/prompting.py::build_prompt` puts caller text only in the `variable` half, between fixed BEGIN/END CALLER DATA markers. `_neutralise_delimiters` defuses any forged marker. Every answer goes through `units/structured_intelligence/llm_call.py::parse_output` before it is believed, and the single reprompt never quotes a rejected answer. **Proved for the shared machinery by** `tests/security/test_unit_a_injection.py::test_a_forged_delimiter_is_neutralised_in_every_prompt` and `::test_both_delimiters_forged_at_once_are_both_neutralised`. **Owed by the analysis batch:** Unit B templates under `prompts/call_intelligence/` (today only a `.gitkeep`), segments passed as the delimited data half, and an injection test that speaks the delimiters into a segment.

## LLM02: Sensitive information disclosure

**Applies now.** A call carries names, phone numbers and prices, and the push carries a signed link, two phone hashes and possibly a voiceprint.

- **Nothing reaches a log.** The audio link, the hashes, the voiceprint and every word of the transcript stay out of every log line, repr and exception. `Segment.text` and `Transcript.segments` are `repr=False` (`transcriber.py`), `Job.metadata` is `repr=False` (`core/jobs.py`), and download and callback errors carry fixed reason codes, unchained. **Proved by** `test_calls_route.py::test_no_line_carries_the_link_a_hash_or_the_voiceprint`, `test_call_observability.py::test_the_outcome_line_is_complete_and_carries_no_content`, `test_calls_read_route.py::test_every_read_is_one_audit_line_with_no_content`, `test_transcriber_conformance.py::test_segment_text_is_never_on_a_repr` and `test_audio_download.py::test_a_transport_failure_is_retryable_and_carries_no_url`.
- **A 72-hour hold.** `result_ttl_seconds` defaults to 259200 and cannot be raised above it (`config.py`). A finished job and its dedupe index expire on the same TTL (`core/jobs.py`), and the recording itself is deleted in a `finally` (`core/audio_download.py`). **Proved by** `test_calls_config.py::test_anything_else_malformed_is_refused` (259201 refused), `test_jobs.py::test_a_terminal_transition_is_final_and_expires_job_and_index` and `test_audio_download.py::test_the_file_is_deleted_when_the_callers_block_raises`.
- **Tenant isolation.** Every key carries the tenant. **Proved by** `test_jobs.py::test_the_same_call_id_in_two_tenants_is_two_jobs` and `test_calls_read_route.py::test_another_tenants_job_is_404`.
- **Owed by the analysis batch:** numbers masked before any model. `core/redaction.py` already masks phones, emails and ids for Unit A (`tests/unit/test_redaction.py`), and the `number_detection_enabled` switch is parsed but gates nothing yet.

## LLM03: Supply chain

**Applies now, and grows with the first adapter.** The supply here is:

- **Speech-to-text vendors.** No adapter is shipped. Any adapter must satisfy the `Transcriber` protocol and pass the conformance suite before it can be listed. The factory has no test switch, so a fake can never be selected by configuration. **Proved by** `test_transcriber_conformance.py` (the suite, parametrised over `IMPLEMENTATIONS`) and `::test_the_factory_refuses_with_no_adapter_and_never_builds_the_fake`.
- **Open models that may run on the device for voice ID and prosody.** None are present. Their passes are owed by later batches, which must pin model files by version and hash.
- **Pinned versions.** Every Python dependency is locked in `uv.lock`. The model ID is pinned by configuration. The provider and model a transcript actually came from are recorded on it and on every job outcome line (`test_call_observability.py::test_the_outcome_line_is_complete_and_carries_no_content`).

**Owed by the STT adapter batch:** the vendor's data-processing terms, the region where audio is stored (ASSUMPTIONS 3.6, B13), and an adapter that passes the suite.

## LLM04: Data and model poisoning

**Does not apply.** Nothing in Unit B trains, fine-tunes, or builds an index from call audio, transcripts or voiceprints. Every model is used through a request/response interface that keeps no state between calls. This becomes live the day any tenant's calls are used to tune or index anything.

## LLM05: Improper output handling

**Applies now to the transcript, and to model output once the analysis batch lands.** A transcriber's output is untrusted until it is checked. `Transcript` validates itself on construction (`transcriber.py`): segments must be in order and never overlap, and every segment names a speaker. The language profile and the uncertainty flag are recomputed in code from the segments, and an adapter that hands back a profile or a flag its own segments contradict is refused. `eligible_for_full_analysis` is decided in code (`worker.py::stage1_result`), never taken from any provider. **Proved by** `test_transcriber_conformance.py::test_a_transcript_that_contradicts_its_segments_is_refused`, `::test_segments_are_ordered_and_never_overlap` and `test_process_call.py::test_short_or_uncertain_calls_are_not_eligible_for_full_analysis`. **Owed by the analysis batch:** every pass through `parse_output` against an `extra="forbid"` schema, and a quote check that refuses any quotation a pass returns unless it appears verbatim in a segment.

## LLM06: Excessive agency

**Applies, and is held by design.** The service sends nothing to anyone except one signed POST, to the callback URL the tenant configured (`core/callbacks.py`). There is no messaging API, no CRM write and no tool call, so a model can trigger no action at all. A URL in a push body can never become a destination: the body is `extra="forbid"`, and `audio_url` is only ever fetched, never posted to. **Proved by** `test_callbacks.py::test_stage1_goes_signed_to_the_callback_url_only` and `test_calls_route.py::test_a_malformed_body_is_422_and_admits_nothing` (a `callback_url` in the body is a 422). **Owed by the analysis batch:** the WhatsApp follow-up suggestion goes back to the CRM as text in the stage 2 body and is never sent by this service. That batch must add a test proving no send path exists.

## LLM07: System prompt leakage

**Applies once Unit B templates exist; none exist today.** The rule, already held for Unit A, is that a prompt carries no secret. Templates are versioned files that hold instructions only. Keys are read in one place each and never enter a prompt: the LLM key in the adapter's header builder, and the callback secret in `core/callbacks.py::_signature`. **Proved by** `test_callbacks.py::test_the_callback_secrets_are_read_in_one_place` and, for the LLM key, `tests/unit/test_openai_compatible_adapter.py::test_key_is_never_in_the_body_or_the_url`. **Owed by the analysis batch:** a scan asserting that no Unit B template contains a key, a tenant name or a URL.

## LLM08: Vector and embedding weaknesses

**Applies to the voiceprint.** This service keeps no vector store. The only embedding is `agent_voiceprint`, which the CRM holds and sends with a push. Its schema is strict base64, capped at 16384 characters (`schemas.py`). It is kept with the job only while the tenant's `voice_id_enabled` consent switch is on, and dropped otherwise (`admission.py::job_metadata`). It is never logged, never put in a callback body, and expires with the job. **Proved by** `test_calls_route.py::test_the_voiceprint_is_kept_only_under_the_voice_id_switch`, `::test_no_line_carries_the_link_a_hash_or_the_voiceprint` and `::test_a_malformed_body_is_422_and_admits_nothing` (non-base64 and oversize voiceprints). **Owed by the voice ID batch:** the comparison itself, run on the device, and a test that a voiceprint never leaves it.

## LLM09: Misinformation

**Applies.** The rule is never to invent. A call with nothing to transcribe gets an outcome label (voicemail, no answer, too short) with a null transcript, never a made-up one (`worker.py::unpaid_outcome`). A transcript whose time-weighted confidence falls below 0.6, or that holds no speech, is flagged uncertain in code (`transcriber.py::is_uncertain`), and an uncertain transcript is not eligible for full analysis. **Proved by** `test_process_call.py::test_a_short_or_unanswered_call_is_done_with_its_label_unpaid`, `test_transcriber_conformance.py::test_low_confidence_is_uncertain_by_spoken_time` and `::test_no_speech_is_uncertain_and_other`. **Owed by the analysis batch:** prompts that tell the model not to guess, and the quote check under LLM05.

## LLM10: Unbounded consumption

**Applies now.**

- **Budgets, each on its own keys and never on the live pair.** `cost:calls:tenant` caps pushes (2000 a day), `tokens:calls:tenant` caps analysis tokens, and `audio_seconds:calls:tenant` caps transcription (360000 seconds a day, charged at download).
- **Caps.** The download is at most `max_audio_bytes` (with a 200 MiB ceiling) and must finish inside `CALL_DOWNLOAD_TIMEOUT_SECONDS`. Each job gets at most `CALL_MAX_TRIES` attempts, one transcription ever, and one job per call.
- **Fails closed.** A worker pauses (`paused_budget`) over a budget *or* with the budget store down, and the job store refuses rather than guessing. Every task's spend is priced on its outcome line.

**Proved by** `test_calls_budget.py::test_calls_charges_touch_only_calls_keys`, `::test_an_outage_pauses_the_preflight_and_the_charge`, `test_process_call.py::test_over_budget_or_store_down_pauses_before_any_spend`, `::test_a_crash_after_transcription_retries_delivery_only`, `::test_a_lost_transcription_is_dead_lettered_not_paid_again`, `test_audio_download.py::test_an_oversize_stream_is_refused_and_its_bytes_deleted`, `test_jobs.py::test_two_concurrent_pushes_of_one_call_make_one_job` and `test_calls_route.py::test_over_the_calls_cap_is_429`.

---

## A note on the Unit A checkpoint's numbering

`docs/security/owasp-llm-unit-a.md` uses the **older (2023, v1.1)** numbering. There, LLM02 is "Insecure output handling", LLM03 "Training data poisoning", LLM04 "Model denial of service", LLM05 "Supply chain", LLM06 "Sensitive information disclosure", LLM07 "Insecure plugin design", LLM08 "Excessive agency", LLM09 "Overreliance" and LLM10 "Unbounded consumption / model theft". This note uses the 2025 list, so the same number can name a different item in the two documents: Unit A's LLM02 is this note's LLM05, and Unit A's LLM05 is this note's LLM03. The Unit A note is left unchanged here.
