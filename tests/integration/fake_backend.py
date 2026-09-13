"""A minimal, realistic fake backend used only by the leads end-to-end test.

Serves the CONFIRMED response shapes at the real paths (see ASSUMPTIONS.md
item 7/8): GET /api/service/leads, GET /api/service/leads/{id},
GET /api/service/leads/{id}/notes. Requires the DD-API-KEY header and returns
401 without a correct one, matching the spec.

This is test fixture code, not production code, and is not imported by
anything under src/.
"""

from __future__ import annotations

from fastapi import Depends, FastAPI, Header, HTTPException, Request

EXPECTED_API_KEY = "test-dd-api-key"

_LEADS: dict[int, dict] = {
    1: {
        "id": 1,
        "name": "Acme Corp",
        "phone": "+1***890",
        "email": "info@acme.test",
        "leadType": "web",
        "enquiryType": "purchase",
        "project": "Downtown Towers",
        "status": "open",
        "source": "facebook",
        "feedback": "Interested, follow up next week.",
        "priority": "high",
        "language": "en",
        "leadFor": "sales",
        "country": "AE",
        "assignedToManager": 10,
        "assignedToSales": 42,
        "bookedAmount": None,
        "createdAt": "2026-01-01T10:00:00+00:00",
        "updatedAt": "2026-01-05T12:30:00+00:00",
    },
    2: {
        "id": 2,
        "name": "Beta LLC",
        "phone": None,
        "email": "hello@beta.test",
        "leadType": "referral",
        "enquiryType": "rent",
        "project": None,
        "status": "closed",
        "source": "walk-in",
        "feedback": None,
        "priority": "low",
        "language": "en",
        "leadFor": "leasing",
        "country": "AE",
        "assignedToManager": 10,
        "assignedToSales": 43,
        "bookedAmount": 250000.0,
        "createdAt": "2026-01-02T08:00:00+00:00",
        "updatedAt": "2026-01-02T08:00:00+00:00",
    },
}

_NOTES: dict[int, list[dict]] = {
    1: [
        {
            "id": 1,
            "note": "Called, no answer.",
            "author": "Jane Doe",
            "author_id": 10,
            "createdAt": "2026-01-02T09:00:00+00:00",
        },
        {
            "id": 2,
            "note": "Left voicemail.",
            "author": None,
            "author_id": 11,
            "createdAt": "2026-01-03T09:00:00+00:00",
        },
    ],
    2: [],  # a lead with no notes: valid, not an error
}


def _meta(total: int) -> dict:
    return {"current_page": 1, "per_page": 25, "total": total, "last_page": 1}


class FakeBackend:
    """Wraps the fake FastAPI app and records the last DD-API-KEY and the last
    URL it saw, so tests can assert what was actually sent rather than infer it
    from a 200. The URL is recorded as the ASGI scope reconstructs it, scheme
    included -- which is what makes DODEAL_BACKEND_SCHEME checkable through the
    real httpx path rather than only at the string that builds it."""

    def __init__(self) -> None:
        self.last_dd_api_key: str | None = None
        self.last_url: str | None = None
        self.app = self._build_app()

    def _require_api_key(
        self,
        request: Request,
        dd_api_key: str | None = Header(default=None, alias="DD-API-KEY"),
    ) -> None:
        self.last_dd_api_key = dd_api_key
        self.last_url = str(request.url)
        if dd_api_key != EXPECTED_API_KEY:
            raise HTTPException(status_code=401, detail="Unauthorized")

    def _build_app(self) -> FastAPI:
        app = FastAPI()
        require_key = Depends(self._require_api_key)

        @app.get("/api/service/leads")
        def list_leads(_: None = require_key) -> dict:
            leads = list(_LEADS.values())
            return {"status": True, "data": leads, "meta": _meta(len(leads))}

        @app.get("/api/service/leads/{lead_id}")
        def get_lead(lead_id: int, _: None = require_key) -> dict:
            if lead_id == 999:
                # Deliberately malformed: proves schema validation fails
                # closed end to end, through the real HTTP path.
                return {"unexpected": "shape"}
            lead = _LEADS.get(lead_id)
            if lead is None:
                raise HTTPException(status_code=404, detail="Not found")
            return {"status": True, "data": lead}

        @app.get("/api/service/leads/{lead_id}/notes")
        def get_notes(lead_id: int, _: None = require_key) -> dict:
            if lead_id not in _LEADS:
                raise HTTPException(status_code=404, detail="Not found")
            notes = _NOTES.get(lead_id, [])
            return {"status": True, "data": notes, "meta": _meta(len(notes))}

        return app
