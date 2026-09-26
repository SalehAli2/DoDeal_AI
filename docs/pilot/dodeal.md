# DoDeal pilot settings

The company label is `dodeal`, so every request goes to Host
`dodeal.<DODEAL_INBOUND_BASE_DOMAIN>`. The two bodies are
`scripts/pilot/dodeal_unit_a.json` and `scripts/pilot/dodeal_unit_b.json`.
Each is a complete admin PUT body: every field the section may set, by its
schema name. They hold no secrets.

## What is on

| Section | On | Off |
| --- | --- | --- |
| `unit_a` (notes) | `pilot`, `enforcement_mode: strict`, `blocking_enabled`, `rep_numbers_enabled`, `deal_specifics_applicable` (the deal switch) | nothing else changes from the default |
| `unit_b` (calls) | `pilot`, `calls_enabled`, `scoring_enabled`, `number_detection_enabled`, `alarm_phrases_enabled` | `voice_id_enabled` (not built), `prosody_enabled` |

- `pilot: true` puts `"pilot": true` on every judgement, brief, per-agent
  measure, call stage event and call status response.
- The deal switch counts `deal_specifics` only for notes whose lead block
  sends `deal_type`. Other notes are still marked out of 80.
- `unit_b.keyword_vocabulary` holds "Peace Homes Skyline" in Latin and Arabic.
  `timezone` is `Asia/Dubai` in both sections.

## Placeholders to fill

`apply_pilot.py` refuses to send anything while one is left.

| Field | Put here |
| --- | --- |
| `unit_a.blocking_stages[0]` | DoDeal's CRM stage name for "qualified" |
| `unit_a.blocking_stages[1]` | DoDeal's CRM stage name for "won" |
| `unit_a.blocking_stages[2]` | DoDeal's CRM stage name for "lost" |
| `unit_b.callback_url` | The https URL where the CRM receives call events |
| `unit_b.audio_hosts[0]` | The exact host the recordings are fetched from (add more if needed) |
| `unit_b.alarm_phrases[0]` | The phrases DoDeal wants alarms on (a list) |

A stage name that does not match the CRM's exactly blocks nothing, and
nobody is told. Check each one against the CRM.

## Apply

1. Fill the placeholders. Only placeholder values change.
2. Check the bodies. Nothing is sent:
   `uv run python scripts/pilot/apply_pilot.py --dry-run`
3. Apply both sections. Run this with the service's environment and a
   service token for `dodeal`:
   `DODEAL_PILOT_SERVICE_TOKEN=... uv run python scripts/pilot/apply_pilot.py --base-url https://<the service>`

The script PUTs `unit_a` first, then `unit_b`. It does not retry, and if a PUT
fails it stops and says which section was already applied. Both version stamps
are set by the service.

## Switch a feature off: one PUT

A PUT replaces the whole section, and a field left out goes back to its
default. So to switch one feature off:

1. Set that one field to `false` in its file (for example
   `"blocking_enabled": false` in `dodeal_unit_a.json`).
2. PUT that whole file once:

```
curl -X PUT "https://<the service>/api/v1/admin/tenant-config" \
  -H "Host: dodeal.<DODEAL_INBOUND_BASE_DOMAIN>" \
  -H "Authorization: Bearer $DODEAL_PILOT_SERVICE_TOKEN" \
  -H "Content-Type: application/json" \
  --data @scripts/pilot/dodeal_unit_a.json
```

For a `unit_b` field, PUT `dodeal_unit_b.json` to
`/api/v1/admin/tenant-config/unit_b`. `GET` on the same route shows the rules
in force. To end the pilot label, set `pilot` to `false` in both files.

Turning the deal switch off or on moves `config_version`: marks made under
each setting are not compared. Every other switch moves only `policy_version`.
