"""Every tool against the real Permit API and cloud PDP, in a scratch environment.

Each user talks to their own server, built with `create_server(settings,
identity=bound_user(<their key>))` and reached through the MCP client, as a host would. The
world is described in tests/e2e/scratch.py. A role granted by an approval reaches the cloud
PDP after a policy sync, so those checks poll, bounded by elapsed time.

The texts are legal but hostile: reasons in several scripts, and a 4096-character reviewer
comment (no maximum is documented; the API stores it as text), each compared with what
Permit returns.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

import pytest
from mcp.client import Client

from permit_mcp import bound_user, create_server
from tests.e2e.scratch import (
    EDITOR,
    OA_APPROVED_ROLE,
    RESOURCE,
    TENANT,
    TENANT_EDITOR,
    VIEWER,
    AdminClient,
    call_tool,
    poll,
)
from tests.support import error_text, payload

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping

    from permit_mcp import Settings
    from tests.e2e.scratch import Person, ScratchWorld

pytestmark = pytest.mark.e2e

# The cloud PDP applies a role assignment after a policy sync.
PDP_SYNC_TIMEOUT_SECONDS = 120.0
REASON = 'Need to edit «Q3 report» — für Prüfung ✅ 日本語 עברית "quoted" <b>&amp;</b> \\ end'
LONG_COMMENT = ("Reviewed — ✅ für Prüfung; 日本語のコメント; עברית; «Ωμέγα» <i>&</i> \\ " * 80)[
    :4096
]
REQUESTER_FIELDS = {
    "requesting_user_email": "email",
    "requesting_user_first_name": "first_name",
    "requesting_user_last_name": "last_name",
}


@asynccontextmanager
async def as_user(settings: Settings, person: Person) -> AsyncIterator[Client]:
    """Open an MCP client to a server bound to `person`."""
    server = create_server(settings, identity=bound_user(person.key))
    async with Client(server) as client:
        yield client


async def tool(client: Client, name: str, **arguments: Any) -> Any:  # noqa: ANN401 - JSON
    """Call a tool that must succeed and return its JSON."""
    return payload(await call_tool(client, name, arguments))


async def allowed(client: Client, action: str, instance: str | None = None) -> bool:
    """Return what check_permission answers for the client's user."""
    arguments = {"action": action}
    if instance is not None:
        arguments["resource_instance"] = instance
    answer = await tool(client, "check_permission", **arguments)
    assert set(answer) == {"allowed"}, answer
    result: bool = answer["allowed"]
    return result


def only(listing: Mapping[str, Any], request_id: str) -> dict[str, Any]:
    """Return the one item of a listing with this ID."""
    found = [item for item in listing["data"] if item["id"] == request_id]
    assert len(found) == 1, (request_id, listing)
    item: dict[str, Any] = found[0]
    return item


def assert_listed_as_created(
    item: Mapping[str, Any], created: Mapping[str, Any], requester: Person
) -> None:
    """The listed item holds every field of the created one, and names its requester."""
    assert {key: item.get(key) for key in created} == dict(created)
    for field, source in REQUESTER_FIELDS.items():
        assert item[field] == requester.created[source], field


def assert_decided(
    result: Mapping[str, Any], kind: str, status: str, created: Mapping[str, Any]
) -> dict[str, Any]:
    """The tool reports the decision, and the request is the created one in that status."""
    assert result["status"] == status
    decided: dict[str, Any] = result[kind]
    assert decided["id"] == created["id"]
    assert decided["status"] == status
    assert decided["reason"] == created["reason"]
    return decided


def assert_pending(result: Mapping[str, Any], kind: str) -> dict[str, Any]:
    assert result["status"] == "created"
    created: dict[str, Any] = result[kind]
    assert created["status"] == "pending"
    assert created["reason"] == REASON
    return created


def test_the_world_reads_back_as_created(world: ScratchWorld) -> None:
    api = AdminClient(world.api_url, world.api_key)
    resource = api.request("GET", world.env_path("schema", "resources", RESOURCE))
    sent = world.sent["resource"]
    assert set(resource["actions"]) == set(sent["actions"])
    assert {key: sorted(role["permissions"]) for key, role in resource["roles"].items()} == {
        key: sorted(role["permissions"]) for key, role in sent["roles"].items()
    }
    for person in (world.requester, world.rbac_requester, world.reviewer):
        user = api.request("GET", world.env_path("facts", "users", person.key))
        assert user["id"] == person.id
        assert {key: user.get(key) for key in person.created} == dict(person.created)
    for name, body in world.sent.items():
        if not name.startswith("element:"):
            continue
        element = api.request("GET", world.env_path("elements", "config", body["key"]))
        assert element["elements_type"] == body["elements_type"]
        assert body["settings"].items() <= element["settings"].items()
        # The levels that were set; Permit may list other roles under levels of its own.
        read_back = element["roles_to_levels"]
        assert {
            level: sorted(role["id"] for role in read_back.get(level, []))
            for level in body["roles_to_levels"]
        } == {level: sorted(ids) for level, ids in body["roles_to_levels"].items()}


async def test_list_resource_instances_lists_the_scratch_instance(world: ScratchWorld) -> None:
    async with as_user(world.settings(), world.requester) as client:
        listed = await tool(client, "list_resource_instances", per_page=100)
    items = listed["data"] if isinstance(listed, dict) else listed
    found = [item for item in items if item["key"] == world.instance]
    assert len(found) == 1, listed
    assert {key: found[0][key] for key in world.sent["instance"]} == dict(world.sent["instance"])


async def test_rebac_access_requests_are_approved_cancelled_and_denied(
    world: ScratchWorld,
) -> None:
    settings, doc = world.settings(), world.instance
    async with (
        as_user(settings, world.requester) as requester,
        as_user(settings, world.reviewer) as reviewer,
    ):
        assert await allowed(requester, "edit", doc) is False

        create = {"role": EDITOR, "reason": REASON, "resource_instance": doc}
        first = assert_pending(
            await tool(requester, "create_access_request", **create), "access_request"
        )
        listing = {"status": "pending", "resource_instance": doc}
        mine = await tool(requester, "list_access_requests", **listing)
        assert_listed_as_created(only(mine, first["id"]), first, world.requester)
        to_review = await tool(reviewer, "list_access_requests", **listing)
        assert_listed_as_created(only(to_review, first["id"]), first, world.requester)

        approved = assert_decided(
            await tool(
                reviewer,
                "approve_access_request",
                access_request_id=first["id"],
                reviewer_comment=LONG_COMMENT,
            ),
            "access_request",
            "approved",
            first,
        )
        assert approved["reviewer_comment"] == LONG_COMMENT
        assert approved["reviewer_user_id"] == world.reviewer.id
        await poll(
            "the approved requester's edit permission",
            lambda: allowed(requester, "edit", doc),
            expected=True,
            within=PDP_SYNC_TIMEOUT_SECONDS,
        )

        second = assert_pending(
            await tool(requester, "create_access_request", **create), "access_request"
        )
        assert_decided(
            await tool(requester, "cancel_access_request", access_request_id=second["id"]),
            "access_request",
            "canceled",
            second,
        )

        third = assert_pending(
            await tool(requester, "create_access_request", **create), "access_request"
        )
        denied = assert_decided(
            await tool(
                reviewer,
                "deny_access_request",
                access_request_id=third["id"],
                reviewer_comment=LONG_COMMENT,
            ),
            "access_request",
            "denied",
            third,
        )
        assert denied["reviewer_comment"] == LONG_COMMENT

        everything = await tool(reviewer, "list_access_requests", resource_instance=doc)
        statuses = {
            request["id"]: only(everything, request["id"])["status"]
            for request in (first, second, third)
        }
        assert statuses == {
            first["id"]: "approved",
            second["id"]: "canceled",
            third["id"]: "denied",
        }


async def test_rbac_access_request_is_approved_and_grants_the_tenant_role(
    world: ScratchWorld,
) -> None:
    settings = world.rbac_settings()
    async with (
        as_user(settings, world.rbac_requester) as requester,
        as_user(settings, world.reviewer) as reviewer,
    ):
        assert await allowed(requester, "edit") is False

        created = assert_pending(
            await tool(requester, "create_access_request", role=TENANT_EDITOR, reason=REASON),
            "access_request",
        )
        mine = await tool(requester, "list_access_requests", status="pending")
        assert_listed_as_created(only(mine, created["id"]), created, world.rbac_requester)
        to_review = await tool(reviewer, "list_access_requests", status="pending")
        assert_listed_as_created(only(to_review, created["id"]), created, world.rbac_requester)

        assert_decided(
            await tool(
                reviewer,
                "approve_access_request",
                access_request_id=created["id"],
                reviewer_comment=LONG_COMMENT,
            ),
            "access_request",
            "approved",
            created,
        )
        await poll(
            f"the approved requester's edit permission on the {RESOURCE} type in {TENANT}",
            lambda: allowed(requester, "edit"),
            expected=True,
            within=PDP_SYNC_TIMEOUT_SECONDS,
        )


async def test_a_reviewer_cannot_approve_their_own_access_request(world: ScratchWorld) -> None:
    async with as_user(world.settings(), world.reviewer) as reviewer:
        own = assert_pending(
            await tool(
                reviewer,
                "create_access_request",
                role=VIEWER,
                reason=REASON,
                resource_instance=world.instance,
            ),
            "access_request",
        )
        refused = await call_tool(
            reviewer,
            "approve_access_request",
            {"access_request_id": own["id"], "reviewer_comment": "self-approval"},
        )
        assert "Permit API call to approve access request returned HTTP 403" in error_text(refused)

        still = await tool(
            reviewer, "list_access_requests", status="pending", resource_instance=world.instance
        )
        assert only(still, own["id"])["status"] == "pending"
        assert_decided(
            await tool(reviewer, "cancel_access_request", access_request_id=own["id"]),
            "access_request",
            "canceled",
            own,
        )


async def test_operation_approvals_are_approved_cancelled_and_denied(
    world: ScratchWorld,
) -> None:
    settings, doc = world.settings(), world.instance
    async with (
        as_user(settings, world.requester) as requester,
        as_user(settings, world.reviewer) as reviewer,
    ):
        assert await allowed(requester, "operate", doc) is False

        create = {"reason": REASON, "resource_instance": doc}
        first = assert_pending(
            await tool(requester, "create_operation_approval", **create), "operation_approval"
        )
        to_review = await tool(
            reviewer, "list_operation_approvals", status="pending", resource_instance=doc
        )
        item = only(to_review, first["id"])
        assert_listed_as_created(item, first, world.requester)
        assert (item["resource_key"], item["resource_instance_key"]) == (RESOURCE, doc)

        approved = assert_decided(
            await tool(
                reviewer,
                "approve_operation_approval",
                operation_approval_id=first["id"],
                reviewer_comment=LONG_COMMENT,
            ),
            "operation_approval",
            "approved",
            first,
        )
        assert approved["reviewer_comment"] == LONG_COMMENT
        assert approved["reviewer_user_id"] == world.reviewer.id
        await poll(
            f"the requester's operate permission, which {OA_APPROVED_ROLE} grants",
            lambda: allowed(requester, "operate", doc),
            expected=True,
            within=PDP_SYNC_TIMEOUT_SECONDS,
        )

        second = assert_pending(
            await tool(requester, "create_operation_approval", **create), "operation_approval"
        )
        assert_decided(
            await tool(requester, "cancel_operation_approval", operation_approval_id=second["id"]),
            "operation_approval",
            "canceled",
            second,
        )

        third = assert_pending(
            await tool(requester, "create_operation_approval", **create), "operation_approval"
        )
        denied = assert_decided(
            await tool(
                reviewer,
                "deny_operation_approval",
                operation_approval_id=third["id"],
                reviewer_comment=LONG_COMMENT,
            ),
            "operation_approval",
            "denied",
            third,
        )
        assert denied["reviewer_comment"] == LONG_COMMENT
