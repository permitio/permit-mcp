"""The app's own MCP tools: list_dishes and order_dish.

Like the Permit tools, they take no user argument: each call asks the session's identity
resolver who the caller is, so the model cannot order or read a menu as someone else. They
ask Permit through the Permit SDK, with the settings the MCP tools use.
"""

from collections.abc import Awaitable
from typing import Annotated, Any, Literal, TypedDict, TypeVar

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from permit import Permit, PermitError
from permit.exceptions import PermitNotFoundError
from pydantic import Field

from food_ordering.db import Database, User
from permit_mcp import IdentityError, IdentityResolver, Settings

LIST_DISHES = "list_dishes"
ORDER_DISH = "order_dish"

_T = TypeVar("_T")

# A child orders a dish above this price only with a parent's one-time approval.
CHILD_PRICE_LIMIT = 10.0
# The roles of the restaurants resource the app relies on: the role a child requests to see
# a restaurant, and the role an approved operation approval grants (permission `operate`).
CHILD_ROLE = "child-can-view"
APPROVED_ROLE = "_Approved_"

RestaurantKey = Annotated[
    str,
    Field(
        min_length=1,
        description="Key of the restaurant: the key of its instance from list_resource_instances.",
    ),
]


class MenuItem(TypedDict):
    """One dish and its price in dollars."""

    name: str
    price: float


class Menu(TypedDict):
    """What list_dishes returns."""

    restaurant: str
    dishes: list[MenuItem]


class Order(TypedDict):
    """What order_dish returns."""

    status: Literal["ordered"]
    restaurant: str
    dish: str
    price: float
    used_approval: Annotated[
        bool, Field(description="Whether the order used up a one-time approval.")
    ]


class FoodTools:
    """The ordering tools, acting as the user the identity resolver returns."""

    def __init__(
        self, db: Database, permit: Permit, settings: Settings, identity: IdentityResolver
    ) -> None:
        """Prepare the tools.

        Args:
            db: The app's database.
            permit: The Permit SDK, for permission checks and the one-time approvals.
            settings: The Permit settings: the resource type and tenant of the restaurants.
            identity: Returns the Permit user key of a tool call's caller.

        """
        self._db = db
        self._permit = permit
        self._settings = settings
        self._identity = identity

    def register(self, server: MCPServer[Any]) -> None:
        """Add list_dishes and order_dish to `server`."""
        server.add_tool(
            self._list_dishes,
            name=LIST_DISHES,
            description=(
                "List the dishes of a restaurant with their prices in dollars. Permit decides "
                "whether the signed-in user may see the menu; when they may not, they can "
                f"request the {CHILD_ROLE!r} role on the restaurant with create_access_request."
            ),
            annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
        )
        server.add_tool(
            self._order_dish,
            name=ORDER_DISH,
            description=(
                "Order a dish from a restaurant for the signed-in user. A child may order a "
                f"dish above ${CHILD_PRICE_LIMIT:.2f} only with a one-time approval: request "
                "it with create_operation_approval on the restaurant, and order again once a "
                "parent approves it. The order uses the approval up."
            ),
            annotations=ToolAnnotations(
                read_only_hint=False, destructive_hint=False, open_world_hint=False
            ),
        )

    async def _list_dishes(self, ctx: Context[Any, Any], *, restaurant: RestaurantKey) -> Menu:
        user = await self._acting_user(ctx)
        await self._require(user, "read", restaurant, self._no_menu(restaurant))
        dishes = await self._db.dishes(restaurant)
        return {
            "restaurant": restaurant,
            "dishes": [{"name": dish.name, "price": dish.price} for dish in dishes],
        }

    async def _order_dish(
        self,
        ctx: Context[Any, Any],
        *,
        restaurant: RestaurantKey,
        dish: Annotated[
            str, Field(min_length=1, description="Name of the dish, as list_dishes shows it.")
        ],
    ) -> Order:
        user = await self._acting_user(ctx)
        await self._require(user, "read", restaurant, self._no_menu(restaurant))
        found = await self._db.dish(restaurant, dish)
        if found is None:
            msg = f"Restaurant {restaurant!r} has no dish named {dish!r}; see list_dishes."
            raise ToolError(msg)
        needs_approval = user.role == "child" and found.price > CHILD_PRICE_LIMIT
        if needs_approval:
            await self._require(
                user,
                "operate",
                restaurant,
                (
                    f"{found.name} costs ${found.price:.2f}, and a child needs a parent's "
                    f"approval for dishes above ${CHILD_PRICE_LIMIT:.2f}. Request it with "
                    f"create_operation_approval on restaurant {restaurant!r}, and order again "
                    "once it is approved."
                ),
            )
            await self._use_approval(user, restaurant)
        return {
            "status": "ordered",
            "restaurant": restaurant,
            "dish": found.name,
            "price": found.price,
            "used_approval": needs_approval,
        }

    async def _acting_user(self, ctx: Context[Any, Any]) -> User:
        """Return the caller, from the identity resolver bound to this server."""
        prefix = "The caller could not be identified"
        try:
            username = await self._identity(ctx)
        except IdentityError as exc:
            msg = f"{prefix}: {exc}"
            raise ToolError(msg) from exc
        user = await self._db.user(username)
        if user is None:
            msg = f"{prefix}: {username!r} is not a family member."
            raise ToolError(msg)
        return user

    async def _require(self, user: User, action: str, restaurant: str, refusal: str) -> None:
        resource = {
            "type": self._settings.resource,
            "key": restaurant,
            "tenant": self._settings.tenant,
        }
        if not await self._call(self._permit.check(user.username, action, resource)):
            raise ToolError(refusal)

    async def _use_approval(self, user: User, restaurant: str) -> None:
        """Remove the user's one-time approval, so that it allows this one order only.

        The PDP can still allow `operate` for a moment after an approval was used, so the
        removal decides: when Permit answers that the user has no approval to remove, the
        order is refused.
        """
        unassignment = {
            "user": user.username,
            "role": APPROVED_ROLE,
            "tenant": self._settings.tenant,
            "resource_instance": f"{self._settings.resource}:{restaurant}",
        }
        try:
            await self._permit.api.users.unassign_role(unassignment)
        except PermitNotFoundError as exc:
            msg = (
                f"The one-time approval on restaurant {restaurant!r} was already used. "
                "Request a new one with create_operation_approval."
            )
            raise ToolError(msg) from exc
        except PermitError as exc:
            raise ToolError(str(exc)) from exc

    @staticmethod
    def _no_menu(restaurant: str) -> str:
        return (
            f"The signed-in user may not see restaurant {restaurant!r}. Request the "
            f"{CHILD_ROLE!r} role on it with create_access_request, with the user's reason."
        )

    @staticmethod
    async def _call(call: Awaitable[_T]) -> _T:
        try:
            return await call
        except PermitError as exc:
            raise ToolError(str(exc)) from exc
