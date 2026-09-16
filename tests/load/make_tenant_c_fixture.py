"""Write tests/load/fixtures/tenant-c-load.json from the vendored tenant-a corpus.

Every lead id, note id and author id moves up by ID_OFFSET and the corpus's tenant
becomes tenant-c; no other field changes. The two corpora then share no id, which
test_s5_two_tenants.py checks against the committed file rather than assumes.

Run from the repository root: `uv run python tests/load/make_tenant_c_fixture.py`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

SOURCE = Path(__file__).resolve().parents[1] / "fixtures" / "fake_crm" / "tenant-a.json"
TARGET = Path(__file__).resolve().parent / "fixtures" / "tenant-c-load.json"
TENANT = "tenant-c"
# Above every lead, ordinary note and author id in the source corpus.
ID_OFFSET = 100000


def _shift_note(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        **raw,
        "id": raw["id"] + ID_OFFSET,
        "author_id": raw["author_id"] + ID_OFFSET,
    }


def _shift_by_lead(section: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    """A lead-keyed section with its keys and every record in it shifted."""
    return {
        str(int(lead_id) + ID_OFFSET): [_shift_note(raw) for raw in records]
        for lead_id, records in section.items()
    }


def shifted(payload: dict[str, Any]) -> dict[str, Any]:
    """`payload` with its ids moved up by ID_OFFSET and its tenant set to tenant-c."""
    return {
        **payload,
        "tenant": TENANT,
        "leads": [{**lead, "id": lead["id"] + ID_OFFSET} for lead in payload["leads"]],
        "notes": _shift_by_lead(payload["notes"]),
        "timeline_events": _shift_by_lead(payload["timeline_events"]),
    }


def main() -> None:
    payload = json.loads(SOURCE.read_text(encoding="utf-8"))
    TARGET.parent.mkdir(exist_ok=True)
    # The source file's own layout, so the two diff line for line.
    text = json.dumps(shifted(payload), indent=2, ensure_ascii=False) + "\n"
    TARGET.write_bytes(text.encode("utf-8"))


if __name__ == "__main__":
    main()
