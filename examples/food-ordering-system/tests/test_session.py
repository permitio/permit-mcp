"""Every tool of a session's MCP server acts as the session's user, on the Permit wire."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import aiosqlite
import pytest
from food_ordering import session
from food_ordering.db import User
from food_ordering.session import CHILD_TOOLS
from food_ordering.tools import APPROVED_ROLE, LIST_DISHES, ORDER_DISH

from permit_mcp import TOOL_NAMES
from tests.conftest import FACTS, REQUEST_ID, RESOURCE

if TYPE_CHECKING:
    from collections.abc import Callable
    from contextlib import AbstractAsyncContextManager
    from pathlib import Path

    from food_ordering.db import Database
    from mcp.client import Client
    from mcp.types import CallToolResult
    from permit import Permit

    from tests.conftest import FakePermit

    OpenSession = Callable[[User], AbstractAsyncContextManager[Client]]

PARENT = User("joe", "parent")
CHILD = User("henry", "child")
OTHERS = {PARENT.username: CHILD.username, CHILD.username: PARENT.username}
RESTAURANT = "pizza-palace"
EXPENSIVE_DISH = "Pepperoni Pizza"  # 10.99, above the children's limit
CHEAP_DISH = "Cheese Pizza"  # 8.99

# The arguments of each tool, and whether its requests name the acting user.
# list_resource_instances lists with the server's API key, so it names no user.
CASES: dict[str, tuple[dict[str, Any], bool]] = {
    "list_resource_instances": ({}, False),
    "check_permission": ({"action": "read", "resource_instance": RESTAURANT}, True),
    "create_access_request": (
        {"role": "child-can-view", "reason": "I am hungry", "resource_instance": RESTAURANT},
        True,
    ),
    "list_access_requests": ({}, True),
    "approve_access_request": ({"access_request_id": REQUEST_ID}, True),
    "deny_access_request": ({"access_request_id": REQUEST_ID}, True),
    "cancel_access_request": ({"access_request_id": REQUEST_ID}, True),
    "create_operation_approval": ({"reason": "A treat", "resource_instance": RESTAURANT}, True),
    "list_operation_approvals": ({}, True),
    "approve_operation_approval": ({"operation_approval_id": REQUEST_ID}, True),
    "deny_operation_approval": ({"operation_approval_id": REQUEST_ID}, True),
    "cancel_operation_approval": ({"operation_approval_id": REQUEST_ID}, True),
    LIST_DISHES: ({"restaurant": RESTAURANT}, True),
    ORDER_DISH: ({"restaurant": RESTAURANT, "dish": EXPENSIVE_DISH}, True),
}
SESSION_TOOLS = [
    pytest.param(user, tool, id=f"{user.username}-{tool}")
    for user in (PARENT, CHILD)
    for tool in CASES
    if user.role == "parent" or tool in CHILD_TOOLS
]
# User arguments a model might add to a call. No tool declares them.
USER_ARGUMENT_KEYS = ("user_id", "user", "user_key", "username", "requester", "subject")


@pytest.fixture
def open_session(permit: FakePermit, db: Database, permit_client: Permit) -> OpenSession:
    """Open an in-process MCP client on a session server bound to a user."""

    def connect(user: User) -> AbstractAsyncContextManager[Client]:
        return session.connect(permit.settings, db, permit_client, user)

    return connect


async def call_granted(
    permit: FakePermit, open_session: OpenSession, user: User, tool: str, arguments: dict[str, Any]
) -> CallToolResult:
    """Call a tool as `user`, who may read the restaurant and holds a one-time approval."""
    for action in ("read", "operate"):
        permit.grant(user.username, action, RESTAURANT)
    permit.assign(user.username, APPROVED_ROLE, RESTAURANT)
    async with open_session(user) as client:
        return await client.call_tool(tool, arguments)


def error_text(result: CallToolResult) -> str:
    assert result.is_error
    return " ".join(getattr(item, "text", "") for item in result.content)


def test_every_tool_has_a_case() -> None:
    assert set(CASES) == {*TOOL_NAMES, LIST_DISHES, ORDER_DISH}


@pytest.mark.parametrize(("user", "tool"), SESSION_TOOLS)
async def test_each_tool_acts_as_the_session_user(
    permit: FakePermit, open_session: OpenSession, user: User, tool: str
) -> None:
    arguments, names_user = CASES[tool]
    result = await call_granted(permit, open_session, user, tool, arguments)
    assert not result.is_error, result.content
    assert permit.acting_users() == ({user.username} if names_user else set())


@pytest.mark.parametrize(("user", "tool"), SESSION_TOOLS)
async def test_a_model_supplied_user_does_not_change_the_acting_user(
    permit: FakePermit, open_session: OpenSession, user: User, tool: str
) -> None:
    other = OTHERS[user.username]
    arguments, names_user = CASES[tool]
    with_user_arguments = {**arguments, **dict.fromkeys(USER_ARGUMENT_KEYS, other)}
    result = await call_granted(permit, open_session, user, tool, with_user_arguments)
    assert not result.is_error, result.content
    assert permit.acting_users() == ({user.username} if names_user else set())
    assert other not in permit.wire_text()


@pytest.mark.parametrize("user", [PARENT, CHILD], ids=["parent", "child"])
async def test_no_tool_takes_a_user(open_session: OpenSession, user: User) -> None:
    async with open_session(user) as client:
        listed = await client.list_tools()
    for tool in listed.tools:
        assert not any("user" in name for name in tool.input_schema["properties"]), tool.name


async def test_a_child_s_session_offers_the_child_tools_only(
    permit: FakePermit, open_session: OpenSession
) -> None:
    async with open_session(CHILD) as client:
        listed = await client.list_tools()
        refused = await client.call_tool(
            "approve_access_request", {"access_request_id": REQUEST_ID}
        )
    assert {tool.name for tool in listed.tools} == CHILD_TOOLS
    assert refused.is_error
    assert permit.requests() == []


async def test_a_parent_s_session_offers_every_tool(open_session: OpenSession) -> None:
    async with open_session(PARENT) as client:
        listed = await client.list_tools()
    assert {tool.name for tool in listed.tools} == set(CASES)


async def test_list_dishes_refuses_a_restaurant_the_user_may_not_see(
    open_session: OpenSession,
) -> None:
    async with open_session(CHILD) as client:
        result = await client.call_tool(LIST_DISHES, {"restaurant": "fancy-french"})
    assert "create_access_request" in error_text(result)


async def test_list_dishes_returns_the_menu(permit: FakePermit, open_session: OpenSession) -> None:
    permit.grant(CHILD.username, "read", RESTAURANT)
    async with open_session(CHILD) as client:
        result = await client.call_tool(LIST_DISHES, {"restaurant": RESTAURANT})
    assert result.structured_content == {
        "restaurant": RESTAURANT,
        "dishes": [
            {"name": "Cheese Pizza", "price": 8.99},
            {"name": "Pepperoni Pizza", "price": 10.99},
            {"name": "Veggie Pizza", "price": 9.49},
        ],
    }


async def test_a_child_needs_an_approval_for_an_expensive_dish(
    permit: FakePermit, open_session: OpenSession
) -> None:
    permit.grant(CHILD.username, "read", RESTAURANT)
    async with open_session(CHILD) as client:
        result = await client.call_tool(
            ORDER_DISH, {"restaurant": RESTAURANT, "dish": EXPENSIVE_DISH}
        )
    assert "create_operation_approval" in error_text(result)
    assert not [request for request in permit.requests() if request.method == "DELETE"]


async def test_an_approved_order_uses_the_approval_up(
    permit: FakePermit, open_session: OpenSession
) -> None:
    arguments = {"restaurant": RESTAURANT, "dish": EXPENSIVE_DISH}
    result = await call_granted(permit, open_session, CHILD, ORDER_DISH, arguments)
    assert result.structured_content == {
        "status": "ordered",
        "restaurant": RESTAURANT,
        "dish": EXPENSIVE_DISH,
        "price": 10.99,
        "used_approval": True,
    }
    deletes = [request for request in permit.requests() if request.method == "DELETE"]
    assert [(request.path, request.get_json()) for request in deletes] == [
        (
            f"{FACTS}/users/henry/roles",
            {
                "role": "_Approved_",
                "tenant": "default",
                "resource_instance": f"restaurants:{RESTAURANT}",
            },
        )
    ]


@pytest.mark.parametrize(("user", "dish"), [(CHILD, CHEAP_DISH), (PARENT, EXPENSIVE_DISH)])
async def test_an_order_within_the_limit_keeps_any_approval(
    permit: FakePermit, open_session: OpenSession, user: User, dish: str
) -> None:
    result = await call_granted(
        permit, open_session, user, ORDER_DISH, {"restaurant": RESTAURANT, "dish": dish}
    )
    assert result.structured_content is not None
    assert result.structured_content["used_approval"] is False
    assert [request.method for request in permit.requests() if request.path != "/allowed"] == []


async def test_order_dish_refuses_an_unknown_dish(
    permit: FakePermit, open_session: OpenSession
) -> None:
    permit.grant(PARENT.username, "read", RESTAURANT)
    async with open_session(PARENT) as client:
        result = await client.call_tool(ORDER_DISH, {"restaurant": RESTAURANT, "dish": "Tacos"})
    assert "has no dish named 'Tacos'" in error_text(result)


async def order_twice(open_session: OpenSession) -> list[CallToolResult]:
    """Order two expensive dishes as the child, in one session."""
    arguments = {"restaurant": RESTAURANT, "dish": EXPENSIVE_DISH}
    async with open_session(CHILD) as client:
        return [await client.call_tool(ORDER_DISH, arguments) for _ in range(2)]


async def test_one_approval_allows_one_order(permit: FakePermit, open_session: OpenSession) -> None:
    # The PDP still allows `operate` after the first order: it can answer from before the
    # approval was removed.
    permit.grant(CHILD.username, "read", RESTAURANT)
    permit.grant(CHILD.username, "operate", RESTAURANT)
    permit.assign(CHILD.username, APPROVED_ROLE, RESTAURANT)

    first, second = await order_twice(open_session)

    assert first.structured_content is not None
    assert first.structured_content["status"] == "ordered"
    assert "already used" in error_text(second)
    assert second.structured_content is None
    assert (CHILD.username, APPROVED_ROLE, f"{RESOURCE}:{RESTAURANT}") not in permit.roles


async def test_an_order_after_the_approval_was_used_is_refused(
    permit: FakePermit, open_session: OpenSession
) -> None:
    permit.grant(CHILD.username, "read", RESTAURANT)
    permit.grant(CHILD.username, "operate", RESTAURANT)
    async with open_session(CHILD) as client:
        result = await client.call_tool(
            ORDER_DISH, {"restaurant": RESTAURANT, "dish": EXPENSIVE_DISH}
        )
    assert "already used" in error_text(result)
    assert result.structured_content is None


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        (LIST_DISHES, {"restaurant": RESTAURANT}),
        (ORDER_DISH, {"restaurant": RESTAURANT, "dish": CHEAP_DISH}),
    ],
)
async def test_a_user_removed_mid_session_can_no_longer_use_the_tools(
    permit: FakePermit,
    open_session: OpenSession,
    db_path: Path,
    tool: str,
    arguments: dict[str, Any],
) -> None:
    permit.grant(CHILD.username, "read", RESTAURANT)
    async with open_session(CHILD) as client:
        before = await client.call_tool(tool, arguments)
        async with aiosqlite.connect(db_path) as connection:
            await connection.execute("DELETE FROM users WHERE username = ?", (CHILD.username,))
            await connection.commit()
        after = await client.call_tool(tool, arguments)
    assert not before.is_error
    assert "'henry' is not a family member" in error_text(after)
    assert after.structured_content is None
