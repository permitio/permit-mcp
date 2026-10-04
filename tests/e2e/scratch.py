"""The scratch Permit environment the end-to-end tests run in, and the helpers they share.

`scratch_world` first sweeps the project: it deletes every `mcp-e2e-*` environment older than
an hour, which a run that was killed before its teardown, or whose create answer was lost,
left behind. CI runs one e2e job at a time and a job stops within 25 minutes, so an
environment that old belongs to no live run; one an hour old or younger may be a developer's
run in progress, and is left alone. Then it creates this run's environment, registers its
delete, and fills it. When the block ends, whatever happened inside, it deletes the
environment, and every object in it with it. A 404 counts as deleted. When the block failed
and the delete fails too, the block's failure is still the error raised; the delete's error is
logged and added to it as a note.

The world holds two access-request setups and one operation-approval setup:

- ReBAC: a User Management element scoped to the `document` resource type. `doc-viewer` and
  `doc-editor` are roles on `document`, assigned on one instance, `doc-<run>`. The requester
  is a `doc-viewer` there (LEVEL_3: sees and cancels their own requests), the reviewer a
  `doc-editor` (LEVEL_2: reviews requests for any role but LEVEL_1's). The `doc-` prefix keeps
  them apart from the tenant roles a new environment may come with.
- RBAC: a User Management element over tenant roles. `tenant-viewer` and `tenant-editor` are
  top-level roles, assigned in tenant `default`, with the same levels. It has its own
  requester, so a role granted in one flow does not decide a permission check in the other.
- The container PDP's checks (in tests/e2e/test_permit.py) have users of their own, so
  a role granted in the cloud PDP's tests does not decide them: `pdp-requester` holds what the
  ReBAC requester holds, `pdp-rbac-requester` what the RBAC requester holds, and
  `pdp-bystander` both, and never requests anything.
- Operation approvals: an Approval Management element. Permit lets a user review an operation
  approval on an instance when they hold the `_Reviewer_` role there, and grants `_Approved_`
  when one is approved; both are roles on `document`, and the reviewer holds `_Reviewer_` on
  the instance.

Requests go through `AdminClient` (urllib, blocking), never through the server under test.
It retries a 429 after the seconds its Retry-After asks for, within a budget. Nothing here
retries in the server: `call_tool` retries a tool call that Permit answered with 429.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import sys
import time
import urllib.error
import urllib.request
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Protocol, TypeVar

from permit_mcp import Settings
from permit_mcp._log import get_logger, redact, scrub
from tests.support import text_of

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterator, Mapping
    from contextlib import AbstractContextManager

    from mcp.client import Client
    from mcp.types import CallToolResult

_T = TypeVar("_T")
_LOG = get_logger(__name__)
# A JSON value as the API returns it; the tests index into it.
Json = Any

DEFAULT_API_URL = "https://api.permit.io"
TENANT = "default"
RESOURCE = "document"
ENV_PREFIX = "mcp-e2e-"
# An mcp-e2e-* environment older than this is a leftover; see the module docstring.
STALE_AFTER = timedelta(hours=1)
ENVS_PER_PAGE = 100
NOT_FOUND = 404
TOO_MANY_REQUESTS = 429
REQUEST_TIMEOUT_SECONDS = 30
# How long one request, or one tool call, keeps retrying a 429 before it fails.
RATE_LIMIT_BUDGET_SECONDS = 60.0
# The wait before retrying a 429 that names no usable Retry-After.
DEFAULT_RETRY_AFTER_SECONDS = 1.0
MAX_TOOL_RETRY_DELAY_SECONDS = 10.0

ACTIONS = ("read", "edit", "operate", "review", "approve", "deny")
VIEWER = "doc-viewer"
EDITOR = "doc-editor"
# Permit's operation-approval roles, which it looks up by these keys on the resource.
OA_REVIEWER_ROLE = "_Reviewer_"
OA_APPROVED_ROLE = "_Approved_"
RESOURCE_ROLES: Mapping[str, tuple[str, ...]] = {
    VIEWER: ("read",),
    EDITOR: ("read", "edit"),
    OA_REVIEWER_ROLE: ("review", "approve", "deny"),
    OA_APPROVED_ROLE: ("operate",),
}
TENANT_VIEWER = "tenant-viewer"
TENANT_EDITOR = "tenant-editor"
TENANT_ROLES: Mapping[str, tuple[str, ...]] = {
    TENANT_VIEWER: (f"{RESOURCE}:read",),
    TENANT_EDITOR: (f"{RESOURCE}:read", f"{RESOURCE}:edit"),
}
REBAC_ELEMENT = "mcp-e2e-rebac-access"
RBAC_ELEMENT = "mcp-e2e-rbac-access"
APPROVAL_ELEMENT = "mcp-e2e-operation-approvals"


class PermitAdminError(Exception):
    """A request of the scratch-world setup or teardown failed.

    Attributes:
        status: The HTTP status, or None when no HTTP answer arrived.

    """

    def __init__(self, method: str, path: str, status: int | None, detail: str) -> None:
        """Build the error; `detail` is scrubbed of every registered secret."""
        self.status = status
        answer = "no response" if status is None else f"HTTP {status}"
        # Scrubbed before it is cut, so a secret across the cut cannot leak in part.
        super().__init__(scrub(f"{method} {path} failed with {answer}: {detail}")[:2000])


class SweepError(Exception):
    """Deleting one or more leftover environments failed; every other one was deleted."""


class PollTimeoutError(AssertionError):
    """A polled value did not reach the expected one in time.

    Attributes:
        last: The last value fetched.

    """

    def __init__(self, what: str, last: object, within: float) -> None:
        """Build the error from what was polled, its last value and the bound."""
        self.last = last
        super().__init__(f"{what} was still {last!r} after {within:g} seconds")


@dataclass
class AdminClient:
    """Sends JSON requests to the Permit API with one API key.

    Attributes:
        api_url: Base URL of the Permit API, without a trailing slash.
        api_key: The API key; registered for redaction on construction.
        rate_limit_budget: Seconds one request keeps retrying a 429.
        sleep: Waits between retries; replaced in tests.

    """

    api_url: str
    api_key: str = field(repr=False)
    rate_limit_budget: float = RATE_LIMIT_BUDGET_SECONDS
    sleep: Callable[[float], None] = time.sleep

    def __post_init__(self) -> None:
        redact(self.api_key)

    def request(self, method: str, path: str, body: Mapping[str, Any] | None = None) -> Json:
        """Send one request and return its JSON, or None for an empty body.

        A 429 is retried after its Retry-After seconds (1 when absent or unreadable), until
        `rate_limit_budget` seconds have passed since the first attempt.

        Raises:
            PermitAdminError: The API answered outside 2xx or with other than JSON, or did
                not answer.

        """
        deadline = time.monotonic() + self.rate_limit_budget
        while True:
            try:
                return self._send(method, path, body)
            except _RateLimitedError as limited:
                if time.monotonic() + limited.wait > deadline:
                    raise PermitAdminError(
                        method,
                        path,
                        TOO_MANY_REQUESTS,
                        f"still rate limited after {self.rate_limit_budget:g} seconds",
                    ) from limited
                self.sleep(limited.wait)

    def delete(self, path: str) -> None:
        """Send `DELETE path`; a 404 means it is already gone, which is the goal."""
        try:
            self.request("DELETE", path)
        except PermitAdminError as error:
            if error.status != NOT_FOUND:
                raise

    def _send(self, method: str, path: str, body: Mapping[str, Any] | None) -> Json:
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(  # noqa: S310 - the URL is the configured API's
            f"{self.api_url}{path}",
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            opened = urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS)  # noqa: S310
            with opened as response:
                status = response.status
                text = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as error:
            # An HTTPError holds the open response; closed here, not left to the collector.
            with error:
                detail = error.read().decode("utf-8", errors="replace")
                if error.code == TOO_MANY_REQUESTS:
                    wait = retry_after(error.headers.get("Retry-After"))
                    raise _RateLimitedError(wait) from error
                raise PermitAdminError(method, path, error.code, detail) from error
        except (urllib.error.URLError, TimeoutError) as error:
            raise PermitAdminError(method, path, None, str(error)) from error
        if not text.strip():
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError as error:
            raise PermitAdminError(
                method, path, status, f"the answer is not JSON: {text}"
            ) from error


class _RateLimitedError(Exception):
    def __init__(self, wait: float) -> None:
        super().__init__(f"rate limited; retry after {wait:g} seconds")
        self.wait = wait


def retry_after(header: str | None) -> float:
    """Return the seconds a Retry-After header asks for; 1 when absent, unreadable or not > 0.

    Permit sends seconds; the HTTP-date form is read as unreadable.
    """
    try:
        seconds = float(header or "")
    except ValueError:
        return DEFAULT_RETRY_AFTER_SECONDS
    return seconds if seconds > 0 else DEFAULT_RETRY_AFTER_SECONDS


class Capture(Protocol):
    """The part of pytest's capture manager that `mask_in_actions` uses."""

    def global_and_fixture_disabled(self) -> AbstractContextManager[None]:
        """Stop capturing for the block."""
        ...


def mask_in_actions(secret: str, environ: Mapping[str, str], capture: Capture | None) -> None:
    """Have GitHub Actions mask `secret` in the log, when running in Actions.

    The command goes to the real stdout, past pytest's capture when there is one, so the
    runner reads it before anything could print the secret.
    """
    if environ.get("GITHUB_ACTIONS") != "true":
        return
    with nullcontext() if capture is None else capture.global_and_fixture_disabled():
        sys.stdout.write(f"::add-mask::{secret}\n")
        sys.stdout.flush()


@dataclass(frozen=True)
class Person:
    """A Permit user of the world: its key, its ID, and what it was created with."""

    key: str
    id: str
    created: Mapping[str, Any]


@dataclass(frozen=True)
class ScratchWorld:
    """What `scratch_world` built, for the tests to act in and to read back.

    Attributes:
        sent: The body of each create, by a name such as "resource" or "element:<key>",
            for comparing with what the API reads back.

    """

    api_url: str
    api_key: str = field(repr=False)
    project_id: str
    environment_id: str
    environment_key: str
    instance: str
    requester: Person
    rbac_requester: Person
    reviewer: Person
    pdp_requester: Person
    pdp_rbac_requester: Person
    pdp_bystander: Person
    sent: Mapping[str, Mapping[str, Any]]

    def settings(self) -> Settings:
        """Settings for the server under test, over the ReBAC and approval elements."""
        return Settings(
            api_key=self.api_key,
            resource=RESOURCE,
            tenant=TENANT,
            api_url=self.api_url,
            access_request_element=REBAC_ELEMENT,
            operation_approval_element=APPROVAL_ELEMENT,
        )

    def rbac_settings(self) -> Settings:
        """Settings for the server under test, over the RBAC element."""
        return Settings(
            api_key=self.api_key,
            resource=RESOURCE,
            tenant=TENANT,
            api_url=self.api_url,
            access_request_element=RBAC_ELEMENT,
        )

    def env_path(self, api: str, *segments: str) -> str:
        """Return `/v2/<api>/<project>/<environment>/<segments...>`."""
        return "/".join(("/v2", api, self.project_id, self.environment_id, *segments))


def new_run_id(environ: Mapping[str, str]) -> str:
    """Return a run ID for keys: GitHub's run ID, attempt and job, or a random local one.

    The job keeps apart the environments of CI's two e2e jobs, which run one after the
    other in the same workflow run.
    """
    run, attempt = environ.get("GITHUB_RUN_ID"), environ.get("GITHUB_RUN_ATTEMPT")
    job = environ.get("GITHUB_JOB")
    if run and attempt and job:
        return f"{run}-{attempt}-{job}"
    return f"local-{secrets.token_hex(4)}"


def sweep(project: AdminClient, project_id: str) -> list[str]:
    """Delete every `mcp-e2e-*` environment of the project older than `STALE_AFTER`.

    Returns:
        The keys of the environments deleted (or already gone).

    Raises:
        SweepError: A delete failed; its message names each environment to delete by hand.
            Every other one was deleted.

    """
    cutoff = datetime.now(UTC) - STALE_AFTER
    stale: list[Mapping[str, Any]] = []
    page = 1
    while True:
        listed = project.request(
            "GET", f"/v2/projects/{project_id}/envs?page={page}&per_page={ENVS_PER_PAGE}"
        )
        stale += [
            env
            for env in listed
            if str(env["key"]).startswith(ENV_PREFIX) and _created(env) < cutoff
        ]
        if len(listed) < ENVS_PER_PAGE:
            break
        page += 1
    failures: list[str] = []
    for env in stale:
        try:
            project.delete(f"/v2/projects/{project_id}/envs/{env['id']}")
        except PermitAdminError as error:
            failures.append(f"{env['key']}: {error}")
    if failures:
        raise SweepError("Could not delete leftover environments:\n" + "\n".join(failures))
    return [str(env["key"]) for env in stale]


def _created(env: Mapping[str, Any]) -> datetime:
    """Return when the environment was created; a time without a zone is read as UTC."""
    created = datetime.fromisoformat(str(env["created_at"]))
    return created if created.tzinfo is not None else created.replace(tzinfo=UTC)


@contextmanager
def scratch_world(
    project: AdminClient,
    project_id: str,
    run_id: str,
    on_env_key: Callable[[str], None] = redact,
) -> Iterator[ScratchWorld]:
    """Sweep leftovers, build the scratch world in `project_id`, yield it, then delete it.

    Args:
        project: A client with a project-level API key of the project.
        project_id: ID or key of the project.
        run_id: Unique to the run; part of the environment's key and of the users' keys.
        on_env_key: Called with the scratch environment's API key as soon as it is read,
            before any request uses it, such as to mask it in a CI log.

    Yields:
        The world. Its environment is deleted when the block ends, after a failed setup too.

    Raises:
        SweepError: A leftover environment could not be deleted; nothing was created.
        PermitAdminError: A setup request, or the environment's delete, failed. When the
            block failed and the delete failed too, the block's error is raised, with the
            delete's error logged and added to it as a note.

    """
    sweep(project, project_id)
    environment_key = f"{ENV_PREFIX}{run_id}"
    created = project.request(
        "POST",
        f"/v2/projects/{project_id}/envs",
        {
            "key": environment_key,
            "name": environment_key,
            "description": "permit-mcp end-to-end tests; deleted when the run ends.",
        },
    )
    environment_id = str(created["id"])
    environment_path = f"/v2/projects/{project_id}/envs/{environment_id}"
    try:
        yield _fill(project, project_id, environment_id, run_id, on_env_key)
    except BaseException as failure:
        _delete_after_failure(project, environment_path, environment_key, failure)
        raise
    project.delete(environment_path)


def _delete_after_failure(
    project: AdminClient, environment_path: str, environment_key: str, failure: BaseException
) -> None:
    """Delete the environment after `failure`, which stays the error the run reports.

    A failed delete is logged, and added to `failure` as a note, rather than raised in its
    place: the test failure is what the run must show.
    """
    try:
        project.delete(environment_path)
    except PermitAdminError as delete_error:
        _LOG.error(
            "Could not delete the scratch environment %s after a failure; the next run's sweep"
            " deletes it once it is an hour old: %s",
            environment_key,
            delete_error,
        )
        failure.add_note(
            f"Deleting the scratch environment {environment_key} failed too: {delete_error}"
        )


def _fill(
    project: AdminClient,
    project_id: str,
    environment_id: str,
    run_id: str,
    on_env_key: Callable[[str], None],
) -> ScratchWorld:
    env_key = str(project.request("GET", f"/v2/api-key/{project_id}/{environment_id}")["secret"])
    on_env_key(env_key)
    env = AdminClient(project.api_url, env_key, project.rate_limit_budget, project.sleep)

    def path(api: str, *segments: str) -> str:
        return "/".join(("/v2", api, project_id, environment_id, *segments))

    sent: dict[str, Mapping[str, Any]] = {}
    try:
        env.request("GET", path("facts", "tenants", TENANT))
    except PermitAdminError as error:
        if error.status != NOT_FOUND:
            raise
        env.request("POST", path("facts", "tenants"), {"key": TENANT, "name": "Default Tenant"})

    resource_body = {
        "key": RESOURCE,
        "name": "Document",
        "actions": {action: {} for action in ACTIONS},
        "roles": {
            key: {"name": key.strip("_").title(), "permissions": list(permissions)}
            for key, permissions in RESOURCE_ROLES.items()
        },
    }
    resource = env.request("POST", path("schema", "resources"), resource_body)
    sent["resource"] = resource_body
    role_ids = {key: str(role["id"]) for key, role in resource["roles"].items()}
    for key, permissions in TENANT_ROLES.items():
        role_body = {"key": key, "name": key, "permissions": list(permissions)}
        role_ids[key] = str(env.request("POST", path("schema", "roles"), role_body)["id"])
        sent[f"role:{key}"] = role_body

    instance = f"doc-{run_id}"
    sent["instance"] = {"key": instance, "tenant": TENANT, "resource": RESOURCE}
    env.request("POST", path("facts", "resource_instances"), sent["instance"])

    people = {
        name: _create_user(env, path("facts", "users"), f"{name}-{run_id}")
        for name in (
            "requester",
            "rbac-requester",
            "reviewer",
            "pdp-requester",
            "pdp-rbac-requester",
            "pdp-bystander",
        )
    }

    rebac_levels = {"LEVEL_2": [role_ids[EDITOR]], "LEVEL_3": [role_ids[VIEWER]]}
    rbac_levels = {"LEVEL_2": [role_ids[TENANT_EDITOR]], "LEVEL_3": [role_ids[TENANT_VIEWER]]}
    elements = {
        REBAC_ELEMENT: (
            "user_management",
            {"configureType": "REBAC", "resourceTypeSelected": str(resource["id"])},
            rebac_levels,
        ),
        RBAC_ELEMENT: ("user_management", {"configureType": "RBAC"}, rbac_levels),
        APPROVAL_ELEMENT: ("approval_management", {}, rebac_levels),
    }
    for key, (elements_type, settings, levels) in elements.items():
        sent[f"element:{key}"] = {
            "key": key,
            "name": f"permit-mcp e2e {key}",
            "elements_type": elements_type,
            "settings": settings,
            "roles_to_levels": levels,
            "email_notifications": False,
        }
        env.request("POST", path("elements", "config"), sent[f"element:{key}"])

    on_instance = f"{RESOURCE}:{instance}"
    assignments = (
        (people["requester"], VIEWER, on_instance),
        (people["reviewer"], EDITOR, on_instance),
        (people["reviewer"], OA_REVIEWER_ROLE, on_instance),
        (people["reviewer"], TENANT_EDITOR, None),
        (people["rbac-requester"], TENANT_VIEWER, None),
        (people["pdp-requester"], VIEWER, on_instance),
        (people["pdp-rbac-requester"], TENANT_VIEWER, None),
        (people["pdp-bystander"], VIEWER, on_instance),
        (people["pdp-bystander"], TENANT_VIEWER, None),
    )
    for person, role, resource_instance in assignments:
        assignment: dict[str, str] = {"user": person.key, "role": role, "tenant": TENANT}
        if resource_instance is not None:
            assignment["resource_instance"] = resource_instance
        env.request("POST", path("facts", "role_assignments"), assignment)

    return ScratchWorld(
        api_url=project.api_url,
        api_key=env_key,
        project_id=project_id,
        environment_id=environment_id,
        environment_key=f"{ENV_PREFIX}{run_id}",
        instance=instance,
        requester=people["requester"],
        rbac_requester=people["rbac-requester"],
        reviewer=people["reviewer"],
        pdp_requester=people["pdp-requester"],
        pdp_rbac_requester=people["pdp-rbac-requester"],
        pdp_bystander=people["pdp-bystander"],
        sent=sent,
    )


def _create_user(env: AdminClient, users: str, key: str) -> Person:
    """Create a user with a name in several scripts."""
    body = {
        "key": key,
        "email": f"{key}@example.com",
        "first_name": "Zoë 🔐",
        "last_name": "Ünïcødé-测试 «Ωμέγα»",
    }
    created = env.request("POST", users, body)
    return Person(key=key, id=str(created["id"]), created=body)


async def poll(
    what: str,
    fetch: Callable[[], Awaitable[_T]],
    expected: _T,
    *,
    within: float,
    interval: float = 2.0,
) -> _T:
    """Fetch until the value equals `expected`, for at most `within` seconds.

    The bound is elapsed time, measured from the first fetch; a slow fetch counts
    against it.

    Raises:
        PollTimeoutError: The value never equalled `expected`; it carries the last one.

    """
    deadline = time.monotonic() + within
    while True:
        value = await fetch()
        if value == expected:
            return value
        if time.monotonic() + interval > deadline:
            raise PollTimeoutError(what, value, within)
        await asyncio.sleep(interval)


async def call_tool(
    client: Client,
    name: str,
    arguments: Mapping[str, Any],
    *,
    budget: float = RATE_LIMIT_BUDGET_SECONDS,
) -> CallToolResult:
    """Call a tool, retrying while Permit answers it with 429, for up to `budget` seconds.

    The server reports a 429 as a tool error and does not keep the Retry-After header, so
    the waits double from 1 second, up to 10.
    """
    deadline = time.monotonic() + budget
    delay = DEFAULT_RETRY_AFTER_SECONDS
    while True:
        result = await client.call_tool(name, dict(arguments))
        limited = result.is_error and f"returned HTTP {TOO_MANY_REQUESTS}" in text_of(result)
        if not limited or time.monotonic() + delay > deadline:
            return result
        await asyncio.sleep(delay)
        delay = min(delay * 2, MAX_TOOL_RETRY_DELAY_SECONDS)
