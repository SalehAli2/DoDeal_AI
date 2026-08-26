"""Startup signal for a misprovisioned deployment (audit finding F2): an empty
dd_api_keys map means nothing can reach the backend, and that must be loud at
startup, not discovered later as every data call failing."""

from __future__ import annotations

import logging

from fastapi.testclient import TestClient

from dodeal_ai.core.config import get_settings
from dodeal_ai.main import app


def test_backend_keys_missing_logs_error_when_map_is_empty(monkeypatch, caplog):
    monkeypatch.setenv("DODEAL_JWT_SIGNING_KEY", "test-key")
    monkeypatch.delenv("DODEAL_DD_API_KEYS", raising=False)
    get_settings.cache_clear()

    with caplog.at_level(logging.ERROR, logger="dodeal_ai.startup"), TestClient(app):
        pass

    assert "backend_keys_missing" in caplog.text
    get_settings.cache_clear()


def test_backend_keys_missing_not_logged_when_map_is_non_empty(monkeypatch, caplog):
    monkeypatch.setenv("DODEAL_JWT_SIGNING_KEY", "test-key")
    monkeypatch.setenv("DODEAL_DD_API_KEYS", '{"nasir3":"a-real-key"}')
    get_settings.cache_clear()

    with caplog.at_level(logging.ERROR, logger="dodeal_ai.startup"), TestClient(app):
        pass

    assert "backend_keys_missing" not in caplog.text
    get_settings.cache_clear()
