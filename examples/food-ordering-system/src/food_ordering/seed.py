"""The `food-ordering-seed` command: create the database and the matching objects in Permit.

Through the Permit SDK, it creates each restaurant as a `restaurants` resource instance (its
name and whether children may see it as attributes), each family member as a Permit user,
and their roles: a parent gets `parent` and `_Reviewer_` on every restaurant, a child gets
`child-can-view` on the restaurants open to children. Run again, it updates the attributes
of the restaurants that exist to match the database, and assigns only the roles that are
missing.
"""

import asyncio
import sys

from permit import Permit, PermitError
from permit.exceptions import PermitAlreadyExistsError
from rich.console import Console

import permit_mcp
from food_ordering.config import db_path_from_env, permit_sdk
from food_ordering.db import Database
from food_ordering.tools import CHILD_ROLE
from permit_mcp import Settings

PARENT_ROLE = "parent"
REVIEWER_ROLE = "_Reviewer_"


async def seed(db: Database, permit: Permit, settings: Settings, console: Console) -> None:
    """Create the database, then the restaurants, users and roles in Permit."""
    await db.init()
    restaurants = await db.restaurants()
    for restaurant in restaurants:
        attributes = {
            "name": restaurant.name,
            "allowed_for_children": restaurant.allowed_for_children,
        }
        try:
            await permit.api.resource_instances.create(
                {
                    "key": restaurant.key,
                    "resource": settings.resource,
                    "tenant": settings.tenant,
                    "attributes": attributes,
                }
            )
        except PermitAlreadyExistsError:
            await permit.api.resource_instances.update(
                f"{settings.resource}:{restaurant.key}", {"attributes": attributes}
            )
            console.print(f"Restaurant {restaurant.key}: updated")
        else:
            console.print(f"Restaurant {restaurant.key}: created")
    assignments: list[dict[str, str]] = []
    for user in await db.users():
        await permit.api.users.sync({"key": user.username, "first_name": user.username.title()})
        console.print(f"User {user.username} ({user.role}): synced")
        if user.role == "parent":
            roles = [(PARENT_ROLE, r.key) for r in restaurants]
            roles += [(REVIEWER_ROLE, r.key) for r in restaurants]
        else:
            roles = [(CHILD_ROLE, r.key) for r in restaurants if r.allowed_for_children]
        assignments += [
            {
                "user": user.username,
                "role": role,
                "tenant": settings.tenant,
                "resource_instance": f"{settings.resource}:{key}",
            }
            for role, key in roles
        ]
    report = await permit.api.role_assignments.bulk_assign(assignments)
    console.print(
        f"Roles: {report.assignments_created or 0} of {len(assignments)} assigned; "
        "the others were already assigned"
    )


def main() -> None:
    """Run the seed command with the `PERMIT_*` and `FOOD_ORDERING_DB` variables.

    Exits with status 2 on a configuration error and 1 when a Permit call fails.
    """
    console = Console(stderr=True)
    try:
        settings = Settings.from_env()
    except permit_mcp.ConfigError as exc:
        console.print(f"food-ordering-seed: configuration error: {exc}", markup=False)
        sys.exit(2)
    try:
        asyncio.run(seed(Database(db_path_from_env()), permit_sdk(settings), settings, console))
    except PermitError as exc:
        console.print(f"food-ordering-seed: {exc}", markup=False)
        sys.exit(1)
