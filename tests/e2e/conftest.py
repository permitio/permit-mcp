"""The scratch world of the end-to-end suite, built once per session from the environment.

The suite runs only when selected (`-m e2e`; pyproject.toml's addopts deselects it
otherwise) and needs PERMIT_E2E_PROJECT_API_KEY and PERMIT_E2E_PROJECT_ID, and optionally
PERMIT_E2E_API_URL, and PERMIT_E2E_PDP_IMAGE to run a PDP image other than the pinned one.
The container PDP's tests also need docker on PATH. When a selected run
lacks one of these, pytest exits 2 before any test starts, and so before any request or
container, saying the suite did not run: never a pass, and never a skip.

When a test that used the container PDP fails, its report gets a section with the PDP's log.

When PERMIT_MCP_API_RECORD is set, tests/api_record.py records the requests the server sends,
and the fixtures declare the origins they talk to: the Permit API and the cloud PDP once the
world is built, the container PDP once it is healthy.
"""

from __future__ import annotations

import os
import shutil
from typing import TYPE_CHECKING

import pytest

from tests.api_record import CONTROL_PLANE, PDP, note_origin
from tests.e2e.pdp import ContainerPdp, pdp_image, run_container_pdp
from tests.e2e.scratch import (
    DEFAULT_API_URL,
    AdminClient,
    mask_in_actions,
    new_run_id,
    scratch_world,
)

if TYPE_CHECKING:
    from collections.abc import Generator, Iterator

    from tests.e2e.scratch import ScratchWorld

REQUIRED_VARIABLES = ("PERMIT_E2E_PROJECT_API_KEY", "PERMIT_E2E_PROJECT_ID")
DID_NOT_RUN_EXIT_CODE = 2
CONTAINER_PDP_FIXTURE = "container_pdp"
PDP_LOG_SECTION = "container PDP log, without health checks"


def missing_variables() -> list[str]:
    """Return the required variables that are unset or blank."""
    return [name for name in REQUIRED_VARIABLES if not os.environ.get(name, "").strip()]


def pytest_collection_finish(session: pytest.Session) -> None:
    """Stop with exit status 2 when e2e tests are selected to run without what they need.

    Listing them (--collect-only) needs nothing.
    """
    if session.config.option.collectonly:
        return
    selected = [item for item in session.items if item.get_closest_marker("e2e")]
    missing = missing_variables()
    if selected and missing:
        pytest.exit(
            f"The end-to-end suite did not run: set {' and '.join(missing)} (a project-level "
            "API key of a Permit project kept for these tests, and that project's ID or key). "
            "See CONTRIBUTING.md.",
            returncode=DID_NOT_RUN_EXIT_CODE,
        )
    uses_docker = any(
        CONTAINER_PDP_FIXTURE in getattr(item, "fixturenames", ()) for item in selected
    )
    if uses_docker and shutil.which("docker") is None:
        pytest.exit(
            "The end-to-end suite did not run: docker is not on PATH, and the container PDP's "
            "tests run a PDP with it. See CONTRIBUTING.md.",
            returncode=DID_NOT_RUN_EXIT_CODE,
        )


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(
    item: pytest.Item,
) -> Generator[None, pytest.TestReport, pytest.TestReport]:
    """Add the container PDP's log to the report of a failed test that used it."""
    report = yield
    started = getattr(item, "funcargs", {}).get(CONTAINER_PDP_FIXTURE)
    if report.when == "call" and report.failed and isinstance(started, ContainerPdp):
        report.sections.append((PDP_LOG_SECTION, started.log()))
    return report


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
        note_origin(built.api_url, CONTROL_PLANE)
        note_origin(built.settings().pdp_url, PDP)
        yield built


@pytest.fixture(scope="session")
def container_pdp(world: ScratchWorld) -> Iterator[ContainerPdp]:
    """Run a PDP container for the scratch environment, removed before the environment is.

    The image is the pinned one unless PERMIT_E2E_PDP_IMAGE names another.
    """
    name = f"{world.environment_key}-pdp"
    image = pdp_image(os.environ)
    with run_container_pdp(name, world.api_key, world.api_url, image=image) as started:
        note_origin(started.url, PDP)
        yield started
