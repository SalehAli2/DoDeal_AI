"""The call workers register the same tenant-config sections as the API."""

from dodeal_ai import main
from dodeal_ai.workers import calls


def test_the_workers_register_the_apis_sections() -> None:
    assert calls.TENANT_CONFIG_SECTIONS == main.TENANT_CONFIG_SECTIONS
