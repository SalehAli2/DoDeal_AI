# Audio service: what `diarized_http` expects of the owner's server

The contract for the owner's own speech-to-text server, used by a company whose STT profile names `"provider": "diarized_http"`. The client is `units/call_intelligence/http_stt.py::DiarizedHttpTranscriber`. This file is the whole contract: the AI service sends nothing else and reads nothing else.

## Request

- `POST {base_url}/v1/transcriptions`. `base_url` is the profile's own, for example `http://10.0.0.5:8001`.
- `Authorization: Bearer <key>`. The key comes from the environment variable that the profile's `api_key_env` names. An `http` base URL sends that key in clear, so use it only on a private network.
- A `multipart/form-data` body with these fields:

| Field | Value |
|---|---|
| `file` | the recording, one file: WAV, MP3, AIFF, AAC, OGG or FLAC |
| `model` | the profile's `model`, passed through unchanged |
| `diarize` | `true` |
| `language_hint` | `en`, `ar` or `mixed`; only present when the CRM gave one. It is a hint and may be wrong. |

- Each attempt is one request, and a request can run up to `DODEAL_CALL_JOB_TIMEOUT_SECONDS`. The server must not retry on its own. The AI service retries a failed transcription once, and only when no response arrived or the status says to retry.

## Response: `200`

```json
{
  "model": "owner-stt-1",
  "segments": [
    {"start": 0.0, "end": 4.2, "speaker": "A", "text": "Good morning, this is the sales office.", "language": "en", "confidence": 0.93},
    {"start": 4.5, "end": 8.0, "speaker": "B", "text": "أريد فيلا قرب البحر", "language": "ar", "confidence": null}
  ]
}
```

| Field | Rule |
|---|---|
| `segments` | Required, possibly empty. Each one is a stretch of one voice. Any order is accepted; they are sorted by `start`. |
| `start`, `end` | Required. Seconds from the start of the file, `>= 0`. A segment that overlaps the one before it is clamped to start where that one ends. |
| `speaker` | Any label, the same for the same voice throughout the call. The labels are renamed `speaker_1`, `speaker_2`, and so on, in order of first speech, and the roles pass then decides which voice is the agent. With no labels at all, the transcript is uncertain (`stt_no_speakers`). |
| `text` | Required. What was said, verbatim. Do not translate it. |
| `language` | Optional. An ISO 639 code, lower case (`en`, `ar`), optionally with a region (`ar-AE`). Any other value is ignored, and the segment's language is then taken from the script it is written in. |
| `confidence` | Optional. A number from 0 to 1, or `null`. Do not send log-probabilities. |

Any other field is ignored. A `200` whose body does not fit these rules is a permanent failure: the call is not sent again.

## Errors

The service reads the status only. The error body is never read or logged.

| Status | Meaning to the AI service |
|---|---|
| `400`, `401`, `403`, `404`, `413`, `415`, `422`, any other `4xx` | Permanent (`stt_request_refused`). The call fails and is not retried. |
| `408`, `429`, any `5xx`, a timeout, a dropped connection | Retryable (`stt_unavailable`). The worker retries the call once. |

## Switching a company to this server

1. Add a profile to `DODEAL_CALL_STT_PROFILES`, for example `{"owner":{"provider":"diarized_http","base_url":"http://10.0.0.5:8001","model":"owner-stt-1","api_key_env":"OWNER_STT_KEY"}}`.
2. Set `OWNER_STT_KEY` in the workers' environment and restart them. A profile that cannot be built, for example one with no key or no `base_url`, stops the worker from starting.
3. Set the company's `unit_b.stt_profile` to `"owner"` with the admin `PUT`. This changes routing only, so the `policy_version` moves and the `config_version` does not.
