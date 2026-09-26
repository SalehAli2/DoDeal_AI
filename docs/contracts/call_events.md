# Call events: what the CRM receives from Unit B

This is the contract for the three signed callbacks the AI service sends about a call job: `call.stage1`, `call.stage2` and `call.failed`. It also gives the phone rule the CRM must hash numbers with. The sender is `core/callbacks.py`; the bodies are built in `units/call_intelligence/delivery.py`. Every example below is a real body produced by the code on an invented call, with its timestamp and signature left illustrative.

## 1. Receiving an event

Every event is one `POST` to the tenant's configured `callback_url` (the `unit_b` section of the tenant's config). It is never sent to a URL from a push body. The request carries a JSON body and these headers:

| Header | Value |
|---|---|
| `Content-Type` | `application/json` |
| `X-DODEAL-Event` | `call.stage1`, `call.stage2` or `call.failed` |
| `X-DODEAL-Timestamp` | unix seconds, as a decimal string |
| `X-DODEAL-Event-Id` | a UUID, one per (job, event), the same on every retry |
| `X-DODEAL-Signature` | lowercase hex HMAC-SHA256 of `<timestamp>.<body>` under the tenant's callback secret |

**Verify before you trust anything.** Recompute the signature over the exact bytes you received: the timestamp header, a full stop, then the raw body. Do not re-serialise the JSON first. Compare in constant time, and refuse a timestamp more than 300 seconds from your clock. The body is compact JSON with sorted keys.

**Answer with any 2xx once the event is stored.** Anything else, a timeout or a network error is retried after 60, 300, 1800 and 7200 seconds. After the last retry the delivery has failed, and the result can still be read by `GET`. Redirects are not followed. The same event may therefore arrive more than once: use `X-DODEAL-Event-Id` to ignore a repeat. A tenant with no callback secret gets no callback at all; its results are read by `GET` only.

## 2. The envelope

Every body has these fields:

| Field | Type | Meaning |
|---|---|---|
| `event` | string | the same as `X-DODEAL-Event` |
| `event_id` | string | the same as `X-DODEAL-Event-Id` |
| `job_id` | string | the job the push was admitted as |
| `call_id` | integer | the CRM's call id, from the push |
| `lead_id` | integer | from the push |
| `author_id` | integer | the agent, from the push |

Each event then adds its own fields (sections 3 to 5 and 7). A re-analysis job's events also carry `"reanalysis": true` (section 7). No body ever carries the audio link, a phone hash, a voiceprint, a phone number or its hash, or model reasoning.

## 3. `call.stage1`

**When it is sent.** Once per job that ends `done`, as soon as stage 1 is stored. A call with nothing to transcribe (voicemail, no answer, or shorter than `min_transcribe_seconds`) is done with an outcome label and no transcript.

**Added field.** `result` is the stage-1 result, the same object `GET /api/v1/calls/jobs/{job_id}` returns under `result` while it is held (72 hours at most).

```json
{
  "author_id": 27,
  "call_id": 7,
  "event": "call.stage1",
  "event_id": "0a4f74f3-58de-52ac-ab51-3a3e69e10c12",
  "job_id": "job-1",
  "lead_id": 1656,
  "result": {
    "stage": 1,
    "call_id": 7,
    "duration_seconds": 150,
    "outcome_label": null,
    "eligible_for_full_analysis": true,
    "transcript": {
      "provider": "fake",
      "model": "fake-stt-1",
      "language_profile": "mostly_en",
      "uncertain": false,
      "segments": [
        {"start_s": 0.0, "end_s": 5.0, "speaker": "agent", "language": "en", "confidence": 0.9,
         "text": "Good morning, this is the sales office about the villa."},
        {"start_s": 5.0, "end_s": 10.0, "speaker": "lead", "language": "en", "confidence": 0.9,
         "text": "I want a villa, my budget is 1,200,000 AED."},
        {"start_s": 10.0, "end_s": 15.0, "speaker": "agent", "language": "en", "confidence": 0.9,
         "text": "Text me on my own mobile 055 765 4321 for photos."},
        {"start_s": 15.0, "end_s": 20.0, "speaker": "lead", "language": "en", "confidence": 0.9,
         "text": "Shall we meet on Tuesday? I am happy with that."}
      ]
    },
    "languages": {"client": null, "agent": null, "profile": "mostly_en", "source": "script"},
    "signals": {
      "version": "call_signals_v2",
      "agent": {"talk_share": 0.5, "words_per_minute": 126.0, "interruptions": 0},
      "client": {"talk_share": 0.5, "words_per_minute": 114.0, "interruptions": 0},
      "talk_balance": "balanced",
      "talk_reason": null,
      "numbers": [
        {"speaker": "agent", "start_s": 10.0, "segment": "s3", "last4": "4321", "match": "agent_personal"}
      ],
      "alarms": [
        {"phrase": 0, "speaker": "agent", "start_s": 10.0, "segment": "s3"}
      ],
      "escalations": [
        {"type": "off_channel_contact", "source": "number", "speaker": "agent", "start_s": 10.0, "segment": "s3"},
        {"type": "off_channel_contact", "source": "alarm_phrase", "phrase": 0, "speaker": "agent", "start_s": 10.0, "segment": "s3"}
      ]
    },
    "analysis": {
      "language": "en",
      "uncertain": false,
      "summary": "The client wants a villa. They meet on Tuesday.",
      "crm_note": "Villa, budget 1,200,000 AED; meeting Tuesday.",
      "elements": {
        "wanted": {"text": "A villa.", "quote": "I want a villa", "segment": "s2"},
        "discussed": ["budget", "viewing"],
        "concerns": [],
        "agreed": [{"text": "a meeting on Tuesday", "quote": "Shall we meet on Tuesday?", "segment": "s4"}],
        "next_step": {"action": "Meet", "owner": "agent", "due": "Tuesday",
                      "quote": "Shall we meet on Tuesday?", "segment": "s4"},
        "ending": "moved_forward"
      },
      "details": {
        "budget": {"value": "1,200,000 AED", "state": "stated", "quote": "my budget is 1,200,000 AED", "segment": "s2"},
        "area": {"value": null, "state": "not_mentioned", "quote": null, "segment": null},
        "property_reference": {"value": null, "state": "not_mentioned", "quote": null, "segment": null},
        "timeline": {"value": null, "state": "not_mentioned", "quote": null, "segment": null},
        "payment_method": {"value": null, "state": "not_mentioned", "quote": null, "segment": null},
        "decision_maker": {"value": null, "state": "not_mentioned", "quote": null, "segment": null}
      },
      "mood": {"value": "positive", "quote": "I am happy with that", "segment": "s4", "uncertain": false}
    },
    "analysis_reason": null,
    "versions": {
      "prompt": "unit_b_prompts_v11",
      "signals": "call_signals_v2",
      "model": "fake-model-pinned",
      "transcriber": "fake/fake-stt-1",
      "alarm_list_digest": "cc460d5af42213b05eb501f7cfe9cc86b7e966d0b430183a6d7097288ea08b4e"
    }
  }
}
```

**`result`, field by field**

| Field | Meaning |
|---|---|
| `stage` | always `1` |
| `outcome_label` | `voicemail`, `no_answer` or `too_short` for a call done unpaid; else `null` |
| `eligible_for_full_analysis` | at least `scoring_min_seconds` long and not uncertain, so a `call.stage2` will follow |
| `transcript` | `null` when nothing was transcribed. `language_profile` is `mostly_en`, `mostly_ar`, `mixed` or `other`. `uncertain` is decided in code from the time-weighted confidence. A segment's `speaker` is the transcriber's label; `agent` is the agent, and any other label is the client. The text is as said, **unmasked**. |
| `signals` | **Top-level, and always there with a transcript**, whatever became of the analysis; `null` without one. Found in code with no model. `agent` and `client` carry `talk_share` (0 to 1), `words_per_minute` and `interruptions`. `numbers` is `null` while the tenant's `number_detection_enabled` is off; `alarms` is `null` while `alarm_phrases_enabled` is off. |
| `signals.numbers[]` | one per phone number said: `speaker` (`agent` or `client`), `start_s`, `segment`, `last4`, and `match`. `match` is `lead` (the push's `lead_phone_hash`), `agent_company` (the push's `agent_phone_hash`), `agent_personal` (the agent gave a different number while the push named the company's), `agent_unverified` (the agent gave a number and the push named no company number) or `new_client_number`. Never the number, never its hash. |
| `signals.alarms[]` | one per alarm phrase per segment: `phrase` is its index in the tenant's list, sorted and normalised (`versions.alarm_list_digest` names that list), then `speaker`, `start_s` and `segment` |
| `signals.escalations[]` | `off_channel_contact`, from a number (`source: number`, the agent's `agent_personal`) or an agent's alarm phrase (`source: alarm_phrase`, with `phrase`), in time order |
| `analysis` | `null` when a pass failed; `analysis_reason` then says which pass and why, for example `extract_model_unavailable`, `prose_malformed_output`, `extract_pass_interrupted` or `llm_not_configured`. With no transcript it is `null` and `analysis_reason` is `no_transcript`. |
| `analysis.language` | the summary language, `en` or `ar`, decided in code |
| `analysis.elements` | **The evidence fields.** `wanted` (or `null`), every `concerns[]` and `agreed[]` item, and the `next_step` carry a `quote` and the `segment` it is from. Each quote is at most 25 words and has been checked, in code, to appear word for word in that segment; the same holds for `details` and `mood`. `next_step.owner` is `agent`, `client` or `unknown`; its `action`, `due`, `quote` and `segment` are `null` when there is none. `ending` is `moved_forward`, `stalled`, `needs_follow_up` or `dead`. |
| `analysis.details` | budget, area, property_reference, timeline, payment_method and decision_maker. Each has `state`: `stated` (with a value and a quote), `not_mentioned` (all `null`), or `uncertain` (stated, but from a low-confidence segment or an uncertain call). Nothing is filled from general knowledge. |
| `analysis.mood` | `positive`, `neutral` or `negative`, with its quote and `uncertain` |
| `versions` | the prompt set, the signals version, the model(s) wave 1 ran on, `provider/model` of the transcriber, and the alarm list's digest (`null` while alarms are off) |

A **segment id** is `s<n>`, 1-based in `transcript.segments` order: `s3` is the third segment.

**Added in batch 4, in `result`:**

- `transcript.segments[].speaker`: a diarized call's labels are mapped by the roles pass to `agent` or `client`. A stereo call's labels come from its channels. `confidence` may be `null` when the engine reports none (Gemini).
- `transcript.uncertain_reasons`: fixed codes that make the transcript uncertain whatever its confidence. They are `low_speech_ratio`, `low_volume`, `roles_failed`, `roles_unclear`, `speakers_over_two`, `stt_no_speakers`, `single_voice`, `long_call_no_diarization` and `no_speech`.
- `audio`: `{duration_seconds, channels, speech_ratio, mean_volume_db}`, measured by ffmpeg before any paid call. It is `null` when the recording was not inspected. Under 0.30 speech or -40 dB, the transcript is uncertain.
- `roles`: `null` when no voice needed mapping. Otherwise `{speakers: [{speaker, role, quote, segment}], applied, reasons}`, where `role` is `agent`, `client` or `unclear` and `applied` is false unless exactly one voice is the agent and none is unclear.
- `signals.keywords[]`: the company's `keyword_vocabulary` spotted in code after transcription, `{term, segment, start_s}`, with the term as listed. `null` when the list is empty. The vocabulary is never sent to the speech-to-text engine.
- `versions.passes`: `{<pass>: {provider, model}}` for each pass that answered, including `roles`. `versions.transcriber` is the transcriber's `provider/model`.

**Speakers, signals and escalations, in `result`:**

- **Speaker unknown.** While the roles are not applied (the roles pass failed or was unclear, or the engine named no speaker), a segment's speaker is `unknown` in `signals.numbers[]` and `signals.alarms[]`, never `agent` or `client`.
- **`unattributed_number`.** A number said by an unknown speaker that matches neither hash. A number matching the lead's is still `lead`, and the company's `agent_company`.
- **`off_channel_contact_review`.** An escalation for a person to listen to. A number raises it only when it is `unattributed_number`; the lead's and the company's raise nothing. An alarm phrase from an unknown speaker raises it too.
- **`roles_not_applied`.** While the roles are not applied, `agent` and `client` carry `talk_share`, `words_per_minute` and `interruptions` as `null`, `talk_balance` is `null`, and `signals.talk_reason` is `roles_not_applied`. Otherwise `talk_reason` is `null`.
- **`talk_balance`.** Beside the numbers, from the agent's `talk_share`: `client_led` below 0.35, `balanced` from 0.35 to 0.65, `agent_heavy` above 0.65. `null` when there is no share.
- **`single_voice`.** In `transcript.uncertain_reasons`: one voice on a call of 30 s or more. The transcript is uncertain.
- **`long_call_no_diarization`.** In `transcript.uncertain_reasons`: a Gemini call over 1800 s is sent without diarization, so every speaker is `unknown` and the transcript is uncertain.
- **`no_speech`.** A transcript with no segments. It is uncertain, no model is called, and `roles`, `signals`, `analysis` and `versions` are `null`, with `analysis_reason` `no_speech`.
- **Audio format.** `ffprobe` must decode the start of the file, or the job fails with `audio_format_unknown` before any paid call. Every engine is then sent 16 kHz mono FLAC, converted by `ffmpeg`; a stereo call goes as two, one per side. A file `ffmpeg` cannot convert fails with `audio_unreadable`.

**Languages, in `result`:**

- **`roles.call_languages`.** The roles pass also answers `{client, agent}`, each `{language, quote, segment}`. `language` is one of `gulf_ar`, `egyptian_ar`, `levantine_ar`, `iraqi_ar`, `maghrebi_ar`, `msa_ar`, `en`, `hi`, `ur`, `ru`, `zh`, `fr`, `fa`, `tr` or `other`, judged from the words and not the script: Urdu is `ur`, never Arabic, and French is `fr`, never English. The quote is checked, in code, to come from a voice the answer gives that side's role. A side not heard is all `null`.
- **`languages`.** `{client, agent, profile, source}`. With the roles applied, `client` and `agent` are the pass's codes, `source` is `model`, and `profile` is read from them: every side Arabic is `mostly_ar`, every side English `mostly_en`, one of each `mixed`, anything else `other`. Otherwise both are `null`, `source` is `script`, and `profile` is the transcript's own. `analysis.language` (the summary language) follows `profile`: `ar` for `mostly_ar`, `en` for `mostly_en` and `other`, and the language with more words for `mixed`. So an Urdu call is summarised in English.

**Masking.** A model reads a masked copy of each segment. Every phone number, email address and any other run of 9 to 19 digits is masked; a price grouped in thousands with commas is kept. So a quote may contain `[PHONE]` or `[EMAIL]` where the transcript has the number. The transcript itself is as said.

## 4. `call.stage2`

**When it is sent.** Only for a job whose `eligible_for_full_analysis` was true, and always after its `call.stage1`. Wave 2 runs on its own queue after stage 1 has gone; the job stays `done` throughout. `GET` shows where it is under `stage2`: `not_eligible`, `pending`, `done` or `failed`. While the result is held (72 hours at most), `GET` also returns it under `stage2_result`.

**Added field.** `result` is the stage-2 result.

```json
{
  "author_id": 27,
  "call_id": 7,
  "event": "call.stage2",
  "event_id": "80bd7778-7ed9-5043-8931-4d1259082c34",
  "job_id": "job-1",
  "lead_id": 1656,
  "result": {
    "stage": 2,
    "call_id": 7,
    "objections": {"raised": 0, "addressed": 0, "satisfied": 0, "items": []},
    "score": {
      "total": 63,
      "band": "needs_work",
      "raw": 38,
      "applicable_weight": 60,
      "product_question_asked": {"answer": "no", "quote": null, "segment": null},
      "components": {
        "understanding": {
          "weight": 25, "mark": 10,
          "checks": {
            "asked_budget": {"answer": "yes", "quote": "I want a villa, my budget is 1,200,000 AED", "segment": "s2"},
            "asked_timeline": {"answer": "no", "quote": null, "segment": null},
            "asked_purpose": {"answer": "no", "quote": null, "segment": null},
            "asked_decision_maker": {"answer": "no", "quote": null, "segment": null},
            "listened_more": {"answer": "yes", "source": "code"}
          }
        },
        "objections": {"weight": 25, "mark": null, "suppressed": "no_objections"},
        "next_step": {
          "weight": 20, "mark": 13,
          "checks": {
            "specific_commitment": {"answer": "yes", "quote": "Shall we meet on Tuesday?", "segment": "s4"},
            "has_date": {"answer": "yes", "quote": "on Tuesday", "segment": "s4"},
            "named_owner": {"answer": "no", "quote": null, "segment": null}
          }
        },
        "professionalism": {
          "weight": 15, "mark": 15,
          "checks": {
            "courteous": {"answer": "yes", "quote": "Good morning", "segment": "s1"},
            "no_over_promise": {"answer": "yes", "quote": null, "segment": null},
            "no_pressure": {"answer": "yes", "quote": null, "segment": null},
            "not_interrupting": {"answer": "yes", "source": "code"}
          }
        },
        "product_knowledge": {"weight": 15, "mark": null, "suppressed": "no_product_question"}
      }
    },
    "escalations": {
      "items": [
        {"type": "off_channel_contact", "source": "number", "speaker": "agent", "start_s": 10.0, "segment": "s3"},
        {"type": "off_channel_contact", "source": "alarm_phrase", "phrase": 0, "speaker": "agent", "start_s": 10.0, "segment": "s3"}
      ]
    },
    "coaching": {
      "language": "en",
      "observations": [
        {"kind": "strength", "text": "Opened the call politely.",
         "quote": "Good morning, this is the sales office about the villa.", "segment": "s1",
         "say_it_like_this": null},
        {"kind": "improvement", "text": "Could ask about the client's needs sooner.",
         "quote": "Good morning, this is the sales office about the villa.", "segment": "s1",
         "say_it_like_this": "What matters most to you in a new home?"}
      ],
      "moments": [],
      "plan": ["Ask about budget early.", "Offer two viewing slots.", "Confirm the next step aloud."],
      "stages": {
        "opening": {"done": "no", "quote": null, "segment": null},
        "rapport": {"done": "no", "quote": null, "segment": null},
        "discovery": {"done": "no", "quote": null, "segment": null},
        "qualification": {"done": "no", "quote": null, "segment": null},
        "presentation": {"done": "no", "quote": null, "segment": null},
        "objections": {"done": "no", "quote": null, "segment": null},
        "close": {"done": "no", "quote": null, "segment": null}
      }
    },
    "extras": {
      "keywords": [{"kind": "topic", "said": "a villa", "english": "villa", "segment": "s2"}],
      "tags": {"outcome": "moved_forward", "stage": "viewing", "client_type": "end_user"},
      "agent_dialect": {"dialect": "unknown", "quote": null, "segment": null},
      "whatsapp_dialect": null,
      "whatsapp_suggestion": {"language": "en", "text": "Thank you for your time. See you on Tuesday for the villa viewing."},
      "seriousness": {
        "band": "B",
        "yes": 3,
        "manager_only": true,
        "checks": {
          "budget_stated": {"answer": "yes", "reason": "The client gave a budget.", "quote": "my budget is 1,200,000 AED", "segment": "s2"},
          "timeline_stated": {"answer": "no", "reason": "No move date was said.", "quote": null, "segment": null},
          "decision_maker_named": {"answer": "no", "reason": "Nobody else was named.", "quote": null, "segment": null},
          "next_step_agreed": {"answer": "yes", "reason": "A meeting on Tuesday.", "quote": "Shall we meet on Tuesday?", "segment": "s4"},
          "client_engaged": {"answer": "yes", "reason": "The client answered and proposed a meeting.", "quote": "I am happy with that", "segment": "s4"}
        }
      }
    },
    "reasons": {},
    "versions": {
      "prompt": "unit_b_prompts_v11",
      "objection_list": "objection_list_v1",
      "rubric": "call_rubric_v1",
      "tone_list": "tone_list_v1",
      "model": {
        "objections": "fake-model-pinned",
        "score": "fake-model-pinned",
        "escalations": "fake-model-pinned",
        "coaching": "fake-model-pinned",
        "extras": "fake-model-pinned"
      }
    }
  }
}
```

**`result`, part by part.** Each of the five parts is an object, or `null` with the reason under `reasons`. A pass that failed twice leaves only its own part null, and the other parts are delivered. A reason is `<pass>_malformed_output`, `<pass>_model_unavailable` or `<pass>_pass_interrupted`. The score may also be null by design: `scoring_off` (the tenant's `scoring_enabled` is off), `language_not_enabled`, `not_eligible`, `not_engaged` (the client's talk share is under 0.20) or `objections_unavailable`.

**`language_not_enabled`.** A call whose client language is not on the tenant's `coaching_languages` (default `gulf_ar`, `egyptian_ar`, `levantine_ar`, `iraqi_ar` and `en`) gets `coaching: null` and `score: null`, both with `language_not_enabled`, and neither pass is run. The client language is stage 1's `languages.client`; when stage 1 heard none, the transcript's profile decides: `mostly_en` needs `en` listed, `mostly_ar` any Arabic code, `mixed` both, and `other` is never coached. With `scoring_enabled` off, the score's reason stays `scoring_off`.

| Part | What it holds |
|---|---|
| `objections` | `raised`, `addressed` and `satisfied` counts, and `items[]`. Each item has a `category`: `price`, `timing`, `competitor`, `trust`, `property_fit`, `payment_finance`, `location`, `third_party_approval` or `service_charges_fees` (objection_list_v1). It also has the client's `quote` and `segment`; `addressed` (`yes`/`no`) with the agent's `agent_quote` and `agent_segment`; and `satisfied` (`yes`/`no`/`unclear`) with `satisfied_quote` and `satisfied_segment`. |
| `score` | call_rubric_v1. Every `mark`, the `raw` sum, the `applicable_weight`, the `total` (0 to 100) and the `band` are computed in code, never by a model. `band` is `excellent` (85 and up), `good` (70 to 84), `needs_work` (50 to 69) or `coaching_required` (under 50). A component with `mark: null` is `suppressed`: `no_objections`, or `no_product_question`. It is left out of `applicable_weight`, not scored zero. A marked product_knowledge carries `correctness_unverified: true`. A check with `source: code` was decided in code, not by the model. |
| `escalations` | `items[]` in time order: stage 1's `off_channel_contact` items merged with the model's flags. A model flag has `type`, `issue`, `source: model`, `speaker`, `start_s`, `segment` and `quote`. `issue` is one of `over_promise_or_guarantee`, `wrong_price_or_terms`, `rudeness_or_pressure`, `unprofessional_competitor_talk` or `qualified_no_next_step`; `type` equals `issue`, except that **`wrong_price_or_terms` goes out as `type: claim_to_verify`**, a claim to check and not a finding. |
| `coaching` | in the summary `language`. 2 or 3 `observations` (at least one `strength` and one `improvement`; an improvement has `say_it_like_this`). Up to 4 `moments`, each with `timestamp` (`mm:ss`) and `start_s` read from its segment in code. A 3-action `plan`. The seven `stages`, each `done` with a quote when yes. |
| `extras` | `keywords[]`: `kind` (`project`, `community`, `developer` or `topic`), `said` as spoken and checked in its `segment`, and `english` or `null`. `tags`: `outcome` (`moved_forward`, `stalled`, `needs_follow_up`, `dead`), `stage` (`first_contact`, `follow_up`, `viewing`, `negotiation`, `closing`) and `client_type` (`end_user`, `investor`, `broker`, `unknown`). `whatsapp_suggestion`: at most 60 words in `language`. **This service never sends it**; show it to the agent to send or not. `seriousness`: five checks, each with a `reason` and a quote for a yes. The `band` is computed in code from the `yes` count: `A` for 4 or 5, `B` for 2 or 3, `C` for 0 or 1. **`manager_only: true`**: show it to the agent's manager, never to the agent. |
| `extras.agent_dialect` | `{dialect, quote, segment}`: the agent's Arabic dialect, `gulf_ar`, `egyptian_ar`, `levantine_ar`, `iraqi_ar`, `maghrebi_ar` or `msa_ar`, with a quote checked to come from one of the **agent's** segments; `unknown` with `null` quote and segment when the agent's words do not show it |
| `extras.whatsapp_suggestion.language` | the language the message is written in, decided in code: the client's (stage 1's `languages.client`), `ar` for any Arabic code; else the summary language. One of `ar`, `en`, `hi`, `ur`, `ru`, `zh`, `fr`, `fa` or `tr`. The text is checked to be in that language's script |
| `extras.whatsapp_dialect` | the Arabic dialect the message was asked in, decided in code: the agent's `agent_dialect` when known, else the tenant's `whatsapp_default_dialect` (default `gulf_ar`). `null` when the message is not in Arabic |
| `versions` | the prompt set, the objection list, the rubric, the tone list, the model each pass's answer came from, and `passes`, `{<pass>: {provider, model}}` (a pass that did not answer is absent from both) |
| `extras.keywords[].canonical` | the company's `keyword_vocabulary` name this keyword is, copied exactly as listed; `null` when it is none, and the keyword is kept as found |

Every quote in every part is at most 25 words and has been checked to appear word for word in the segment it cites, from the right speaker where it matters. Quotes read the masked copy, so they may contain `[PHONE]` or `[EMAIL]`.

## 5. `call.failed`

`call.failed` comes in two forms. Only one of them can ever be sent for a job, so it has one event id.

**Stage 1 failed: the job is `failed` or `dead_letter`.** No `call.stage1` was sent and none will be.

```json
{
  "author_id": 27,
  "call_id": 9,
  "event": "call.failed",
  "event_id": "be166bec-5b4d-5093-b3f6-a738d5ab8d65",
  "job_id": "job-3",
  "lead_id": 1656,
  "reason": "transcription_interrupted",
  "status": "dead_letter"
}
```

`status` is `failed` (it can never succeed: a refused link, `calls_not_enabled`) or `dead_letter` (its tries ran out). `reason` is a fixed code. Examples: `calls_not_enabled`, `audio_link_expired`, `audio_host_not_allowed`, `audio_too_large`, `audio_source_unavailable`, `audio_download_timeout`, `transcription_interrupted`, `max_tries_exceeded` and `job_deadline_exceeded`.

**Stage 2 only failed: the job stays `done`, and stage 1 stands.** It carries `stage: 2`. No `call.stage2` was sent and none will be.

```json
{
  "author_id": 27,
  "call_id": 8,
  "event": "call.failed",
  "event_id": "1476869c-b8e7-5f63-8922-bdd42953b351",
  "job_id": "job-2",
  "lead_id": 1656,
  "reason": "stage2_lost",
  "stage": 2,
  "stage2": "failed",
  "status": "done"
}
```

Its `reason` is one of:

- `stage1_result_gone`: the stage-1 result, and its transcript, expired first;
- `llm_not_configured`;
- `token_budget_exceeded`, `audio_budget_exceeded` or `cost_store_unavailable`, on the last of its runs;
- `job_deadline_exceeded`;
- `stage2_lost`: its run was lost, it was re-queued once, and lost again.

A stage 2 whose own passes fail is still `done`; see section 4.

## 6. The phone rule: how the CRM must hash a number

The push's `lead_phone_hash` and `agent_phone_hash` are compared with the numbers said on the call. They match only if the CRM hashes by this rule. `C` is the tenant's `phone_country_code` (default `971`).

1. Write every digit as an ASCII digit and drop every separator (spaces, hyphens, full stops, parentheses).
2. Drop a leading `+` or `00`: `+971…` and `00971…` both become `971…`.
3. Otherwise replace a leading single `0` with `C`, for mobiles and landlines alike: `050 123 4567` becomes `971501234567`, and `04 123 4567` becomes `97141234567`.
4. The result is a phone number when it had a `+` or `00` and is 8 to 15 digits, or starts with `C` and has 8 or 9 digits after it, or started with a single `0` and was 9 to 11 digits.
5. The hash is the lowercase hex SHA-256 of those ASCII digits. Case does not matter when the hashes are compared.

So `+971 50 123 4567`, `00971-50-123-4567`, `050.123.4567` and `(050) 1234567` all hash as `971501234567`. A number with another country's code and no `+` or `00` is not found. A price, a year or a unit number is never a phone number.

## 7. Re-analysis and `call.translation`

**Re-analysis** (`POST /api/v1/calls/reanalysis`, service token, counted on `cost:reanalysis:tenant`):

- The body is `call_id`, `lead_id`, `author_id`, `duration_seconds`, `recorded_at`, `transcript` (the stage-1 `transcript` as delivered), `reason` (`objection_list_changed`, `checklist_changed` or `prompt_changed`) and `stages` (`[1]`, `[2]` or `[1, 2]`). Any other field is a 422.
- The answer is `202 {job_id, status}`. The job is a new one, on the overnight queue. The same call, stages and versions give the same job. The call's first job and its results are never touched.
- The audio is never fetched and the transcript is never made again. The passes run on the current prompt set, lists and rubric, through the company's model route.
- Its `call.stage1` (for stage 1) and `call.stage2` (for stage 2) carry `"reanalysis": true`. With `stages: [2]`, no `call.stage1` is sent, and the stored stage 1 has `analysis_reason: "stage1_not_requested"`.

**`call.translation`** (asked for with `POST /api/v1/calls/jobs/{job_id}/translation {"target": "ar"|"en"}`, counted on the reads counter):

- The answer is `202 {job_id, target, status: "queued"}`. The response is `409 result_expired` once the stage-1 result is gone, and `409 already_in_language` when the call is mostly in the target language already.
- The event is signed like every other and retried on the same schedule: one attempt, then after 60, 300, 1800 and 7200 seconds, with a delivery state of its own per target. A retry sends the held translation; the model is never asked again. `GET /api/v1/calls/jobs/{job_id}` shows it under `translations.<target>` while it is held.
- The pass asks a chunk the model never answered once more. A malformed answer gets one reprompt, then the translation fails. The task itself is never re-run.
- `result` is `{target, segments, reason, versions}`. Each segment is `{segment, start_s, end_s, speaker, text}`; the id, the times and the speaker are the transcript's, and only the text is translated. Numbers stay masked as `[PHONE]`. `segments` is `null` with `reason` `translate_model_unavailable` or `translate_malformed_output` when the pass failed.

## 8. Writing the WhatsApp suggestion again

`POST /api/v1/calls/jobs/{job_id}/whatsapp {"language": "<code>"}` (service token, counted on the reads counter):

- `language` is one of the twelve: `gulf_ar`, `egyptian_ar`, `levantine_ar`, `iraqi_ar`, `en`, `hi`, `ur`, `ru`, `zh`, `fr`, `fa` or `tr`. Anything else is a 422.
- The answer is `200 {job_id, language, dialect, text}`, at most 60 words. `dialect` is the Arabic code asked for, or `null`. **This service never sends it**; show it to the agent.
- It is written from the stored stage-1 transcript by one small model pass, charged to the calls budget of the call's author. The response is `404 call_job_not_found` for no job of this company's, `409 result_expired` once the stage-1 result or its transcript is gone, `409 whatsapp_in_progress` while another request writes the same language, `429 token_budget_exceeded` at the calls budget, and `503 cost_store_unavailable`, `503 model_unavailable` or `503 malformed_output` when it could not be written.
- One paid pass per language: once written, the same language is answered from the store, without a model call. It is held no longer than the stage-1 result, and `GET /api/v1/calls/jobs/{job_id}` shows it under `whatsapp.<language>` with its `versions`.
