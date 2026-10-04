"""The container PDP of the end-to-end suite: started, health-polled and removed by pytest.

The container needs the scratch environment's API key, which exists only inside the test
session, so the session starts it (tests/e2e/conftest.py) after the world is built, and
removes it before the world's environment is deleted.

`run_container_pdp` runs `PDP_IMAGE`, pinned by version and by the digest of that version's
multi-arch image index, so a new PDP release cannot change what the suite runs against.
Docker pulls by the digest; the tag only names it. PERMIT_E2E_PDP_IMAGE names another image
for a run (`pdp_image`): CI's advisory e2e-pdp-latest job runs `permitio/pdp-v2:latest`.
.github/api-specs/pdp.source.json names the pinned image too, as the source of the PDP
inventory; .github/scripts/test_api_coverage.py fails when the two differ. The key reaches
the container through docker's environment (`--env PDP_API_KEY`, the name alone), never its
command line. The port is published on 127.0.0.1 only, at a port docker picks. The PDP
answers 503 on `/healthy` until it has its first policy and data, so the block starts once
it answers 200, waiting at most `HEALTH_TIMEOUT_SECONDS` of elapsed time. Whatever happens,
the container is removed when the block ends. Every error and log it returns is scrubbed of
registered secrets, and its log is shown without the health-check noise.

`published_spec` reads the OpenAPI document a running PDP publishes at `OPENAPI_PATH`, which
needs no token: pdp-v2's server answers it by proxying the FastAPI document of the PDP's
decision service.
"""

from __future__ import annotations

import os
import subprocess
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass
from http import HTTPStatus
from typing import TYPE_CHECKING

from permit_mcp._log import get_logger, redact, scrub

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping

_LOG = get_logger(__name__)

# To move the pin, take a pdp-v2 release at least 7 days old, and its `digest` from
# https://hub.docker.com/v2/repositories/permitio/pdp-v2/tags/<tag>, and change it here and
# in .github/api-specs/pdp.source.json (CONTRIBUTING.md).
PDP_IMAGE = (
    "permitio/pdp-v2:v0.9.15"
    "@sha256:720031733fc918f053a5d4e72225324d246cbc72136ff62180cbee8f491af44e"
)
PDP_IMAGE_ENV = "PERMIT_E2E_PDP_IMAGE"
PDP_PORT = 7000
HEALTH_PATH = "/healthy"
OPENAPI_PATH = "/openapi.json"
# The PDP is healthy once the environment's first policy bundle and data arrive, which
# has taken 63 to 154 seconds in other SDKs' CI.
HEALTH_TIMEOUT_SECONDS = 300.0
HEALTH_INTERVAL_SECONDS = 1.0
PROBE_TIMEOUT_SECONDS = 5.0
# `docker run` pulls the image first.
DOCKER_TIMEOUT_SECONDS = 600.0
LOG_TAIL_LINES = 300
HEALTH_REQUEST = "GET /health"
HORIZON_DOWN = "Health check failed: horizon"
NO_SUCH_CONTAINER = "No such container"


class ContainerPdpError(Exception):
    """Docker failed, or the container PDP did not become healthy in time."""


@dataclass(frozen=True)
class ContainerPdp:
    """A running container PDP.

    Attributes:
        name: The container's name.
        url: Base URL of its PDP API on the host, for `Settings.pdp_url`.

    """

    name: str
    url: str

    def log(self) -> str:
        """Return the container's log, without health checks, or why it cannot be read."""
        try:
            return without_health_checks(docker("logs", self.name, merge_output=True))
        except ContainerPdpError as error:
            return f"The container PDP's log could not be read: {error}"


def docker(
    *args: str,
    secrets: Mapping[str, str] | None = None,
    merge_output: bool = False,
    timeout: float = DOCKER_TIMEOUT_SECONDS,
) -> str:
    """Run `docker <args>` and return its output.

    Args:
        *args: The docker command's arguments; they must not hold a secret.
        secrets: Variables added to docker's environment, such as the PDP's API key,
            which `--env NAME` hands on without writing the value on a command line.
        merge_output: Return stderr with stdout, in the order docker wrote them.
        timeout: Seconds the command may take.

    Raises:
        ContainerPdpError: docker is not on PATH, timed out or failed; scrubbed of every
            registered secret.

    """
    command = ["docker", *args]
    try:
        completed = subprocess.run(  # noqa: S603 - fixed program, arguments built here
            command,
            env={**os.environ, **(secrets or {})},
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT if merge_output else subprocess.PIPE,
            text=True,
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as error:
        msg = "docker is not on PATH; the end-to-end suite runs a container PDP with it."
        raise ContainerPdpError(msg) from error
    except subprocess.TimeoutExpired as error:
        msg = scrub(f"{' '.join(command)} did not finish within {timeout:g} seconds")
        raise ContainerPdpError(msg) from error
    if completed.returncode != 0:
        output = (completed.stderr or completed.stdout or "").strip()
        msg = f"{' '.join(command)} failed with exit status {completed.returncode}: {output}"
        raise ContainerPdpError(scrub(msg))
    return completed.stdout


def without_health_checks(log: str, keep: int = LOG_TAIL_LINES) -> str:
    """Return the last `keep` lines of a PDP log, without its health-check noise.

    Most of the log is the PDP's own health checks, which would push its startup out of
    any tail. The `GET /health...` requests are dropped, and every "Health check failed:
    horizon" line but the first, which says why the PDP was not healthy. The rest shows
    how the policy and data fetches went. The result is scrubbed of registered secrets.
    """
    kept: list[str] = []
    horizon_down_seen = False
    for line in log.splitlines():
        if HEALTH_REQUEST in line:
            continue
        if HORIZON_DOWN in line:
            if horizon_down_seen:
                continue
            horizon_down_seen = True
        kept.append(line)
    return scrub("\n".join(kept[-keep:]))


def is_healthy(url: str, timeout: float = PROBE_TIMEOUT_SECONDS) -> bool:
    """Return whether the PDP at `url` answers 200 on `HEALTH_PATH`, asked without a proxy."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"{url}{HEALTH_PATH}", timeout=timeout) as response:
            return bool(response.status == HTTPStatus.OK)
    except urllib.error.HTTPError as error:
        error.close()
        return False
    except (urllib.error.URLError, TimeoutError, ConnectionError):
        return False


def pdp_image(environ: Mapping[str, str]) -> str:
    """Return the image to run: PERMIT_E2E_PDP_IMAGE when set and not blank, else the pin."""
    return environ.get(PDP_IMAGE_ENV, "").strip() or PDP_IMAGE


def published_spec(url: str, timeout: float = PROBE_TIMEOUT_SECONDS) -> bytes:
    """Return the OpenAPI document the PDP at `url` publishes, asked without a proxy.

    Raises:
        ContainerPdpError: The PDP did not answer 200.

    """
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"{url}{OPENAPI_PATH}", timeout=timeout) as response:
            body: bytes = response.read()
    except urllib.error.HTTPError as error:
        error.close()
        msg = f"The PDP at {url} answered {error.code} on {OPENAPI_PATH}"
        raise ContainerPdpError(msg) from error
    except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
        msg = f"The PDP at {url} did not answer on {OPENAPI_PATH}: {error}"
        raise ContainerPdpError(msg) from error
    return body


def wait_until_healthy(
    pdp: ContainerPdp,
    *,
    within: float,
    interval: float = HEALTH_INTERVAL_SECONDS,
    probe: Callable[[str], bool] = is_healthy,
) -> float:
    """Probe the PDP until it is healthy, for at most `within` seconds of elapsed time.

    Returns:
        The seconds it took.

    Raises:
        ContainerPdpError: It was not healthy in time; the message holds its log.

    """
    started = time.monotonic()
    deadline = started + within
    while True:
        if probe(pdp.url):
            return time.monotonic() - started
        if time.monotonic() + interval > deadline:
            msg = (
                f"The container PDP {pdp.name} did not answer 200 on {HEALTH_PATH} within "
                f"{within:g} seconds. Its log, without health checks:\n{pdp.log()}"
            )
            raise ContainerPdpError(msg)
        time.sleep(interval)


def remove(name: str) -> None:
    """Remove the container, running or not; one that does not exist counts as removed."""
    try:
        docker("rm", "--force", name)
    except ContainerPdpError as error:
        if NO_SUCH_CONTAINER not in str(error):
            raise


@contextmanager
def run_container_pdp(
    name: str,
    api_key: str,
    control_plane: str,
    *,
    image: str = PDP_IMAGE,
    health_within: float = HEALTH_TIMEOUT_SECONDS,
) -> Iterator[ContainerPdp]:
    """Run a PDP image for one environment, wait until it is healthy, yield it, remove it.

    Args:
        name: The container's name, unique to the run.
        api_key: The environment's API key; registered for redaction.
        control_plane: The Permit API the PDP fetches its policy from.
        image: The image to run; the pinned `PDP_IMAGE` unless a run names another.
        health_within: Seconds of elapsed time the PDP has to become healthy.

    Yields:
        The healthy PDP. The container is removed when the block ends, after a failed
        start too.

    Raises:
        ContainerPdpError: docker failed, the PDP did not become healthy in time, or the
            removal failed. When the block failed and the removal failed too, the block's
            error is raised, with the removal's error logged and added to it as a note.

    """
    redact(api_key)
    try:
        docker(
            "run",
            "--detach",
            "--name",
            name,
            "--publish",
            f"127.0.0.1::{PDP_PORT}",
            "--env",
            "PDP_API_KEY",
            "--env",
            f"PDP_CONTROL_PLANE={control_plane}",
            image,
            secrets={"PDP_API_KEY": api_key},
        )
        pdp = ContainerPdp(name=name, url=_published_url(name))
        seconds = wait_until_healthy(pdp, within=health_within)
        _LOG.info("The container PDP %s (%s) was healthy after %.0f seconds", name, image, seconds)
        yield pdp
    except BaseException as failure:
        _remove_after_failure(name, failure)
        raise
    remove(name)


def _published_url(name: str) -> str:
    """Return the host URL docker published the container's PDP port at."""
    published = docker("port", name, f"{PDP_PORT}/tcp").strip().splitlines()
    if not published:
        msg = f"docker port {name} {PDP_PORT}/tcp printed no address"
        raise ContainerPdpError(msg)
    host, _, port = published[0].rpartition(":")
    return f"http://{host}:{port}"


def _remove_after_failure(name: str, failure: BaseException) -> None:
    """Remove the container after `failure`, which stays the error the run reports."""
    try:
        remove(name)
    except ContainerPdpError as remove_error:
        _LOG.error("Could not remove the container PDP %s: %s", name, remove_error)
        failure.add_note(f"Removing the container PDP {name} failed too: {remove_error}")
