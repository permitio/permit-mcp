"""The scratch world of the end-to-end suite, built once per session from the environment.

The suite runs only when selected (`-m e2e`; pyproject.toml's addopts deselects it
otherwise) and needs PERMIT_E2E_PROJECT_API_KEY and PERMIT_E2E_PROJECT_ID, and optionally
PERMIT_E2E_API_URL. When a selected run lacks one, pytest exits 2 before any test starts,
saying the suite did not run: never a pass, and never a skip.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest

from tests.e2e.scratch import (
    DEFAULT_API_URL,
    AdminClient,
    mask_in_actions,
    new_run_id,
    scratch_world,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from tests.e2e.scratch import ScratchWorld

REQUIRED_VARIABLES = ("PERMIT_E2E_PROJECT_API_KEY", "PERMIT_E2E_PROJECT_ID")
DID_NOT_RUN_EXIT_CODE = 2


def missing_variables() -> list[str]:
    """Return the required variables that are unset or blank."""
    return [name for name in REQUIRED_VARIABLES if not os.environ.get(name, "").strip()]


def pytest_collection_finish(session: pytest.Session) -> None:
    """Stop with exit status 2 when e2e tests are selected to run without the credentials.

    Listing them (--collect-only) needs no credentials.
    """
    selected = any(item.get_closest_marker("e2e") for item in session.items)
    missing = missing_variables()
    if selected and missing and not session.config.option.collectonly:
        pytest.exit(
            f"The end-to-end suite did not run: set {' and '.join(missing)} (a project-level "
            "API key of a Permit project kept for these tests, and that project's ID or key). "
            "See CONTRIBUTING.md.",
            returncode=DID_NOT_RUN_EXIT_CODE,
        )


@pytest.fixture(scope="session")
def world(pytestconfig: pytest.Config) -> Iterator[ScratchWorld]:
    """Build the scratch world for the session, and delete its environment at the end."""
    project = AdminClient(
        os.environ.get("PERMIT_E2E_API_URL", "").strip().rstrip("/") or DEFAULT_API_URL,
        os.environ["PERMIT_E2E_PROJECT_API_KEY"].strip(),
    )
    capture = pytestconfig.pluginmanager.getplugin("capturemanager")

    def mask(env_key: str) -> None:
        mask_in_actions(env_key, os.environ, capture)

    run_id = new_run_id(os.environ)
    project_id = os.environ["PERMIT_E2E_PROJECT_ID"].strip()
    with scratch_world(project, project_id, run_id, on_env_key=mask) as built:
        yield built
