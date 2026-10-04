"""The seed command creates the restaurants, users and roles in Permit, through the SDK."""

from __future__ import annotations

import io
from typing import TYPE_CHECKING

from food_ordering.seed import seed
from rich.console import Console

from tests.conftest import FACTS, RESOURCE

if TYPE_CHECKING:
    from food_ordering.db import Database
    from permit import Permit

    from tests.conftest import FakePermit

RESTAURANTS = {
    "burger-bonanza": {"name": "Burger Bonanza", "allowed_for_children": True},
    "fancy-french": {"name": "Fancy French", "allowed_for_children": False},
    "pizza-palace": {"name": "Pizza Palace", "allowed_for_children": True},
    "sushi-world": {"name": "Sushi World", "allowed_for_children": False},
}


async def run_seed(permit: FakePermit, db: Database, permit_client: Permit) -> str:
    output = io.StringIO()
    await seed(db, permit_client, permit.settings, Console(file=output))
    return output.getvalue()


async def test_seed_creates_the_family_in_permit(
    permit: FakePermit, db: Database, permit_client: Permit
) -> None:
    output = await run_seed(permit, db, permit_client)

    assert permit.instances == RESTAURANTS
    assert {key: user["first_name"] for key, user in permit.users.items()} == {
        "henry": "Henry",
        "jane": "Jane",
        "joe": "Joe",
        "rose": "Rose",
    }
    henry = {(role, instance) for user, role, instance in permit.roles if user == "henry"}
    assert henry == {
        ("child-can-view", f"{RESOURCE}:burger-bonanza"),
        ("child-can-view", f"{RESOURCE}:pizza-palace"),
    }
    joe = {(role, instance) for user, role, instance in permit.roles if user == "joe"}
    assert joe == {
        (role, f"{RESOURCE}:{key}") for role in ("parent", "_Reviewer_") for key in RESTAURANTS
    }
    assert len(permit.roles) == 2 * 8 + 2 * 2
    assert "Roles: 20 of 20 assigned" in output
    bulk = [r for r, _ in permit.api.log if r.path == f"{FACTS}/role_assignments/bulk"]
    assert {item["tenant"] for item in bulk[0].get_json()} == {"default"}


async def test_seed_runs_again_without_changing_anything(
    permit: FakePermit, db: Database, permit_client: Permit
) -> None:
    await run_seed(permit, db, permit_client)
    instances, users, roles = dict(permit.instances), dict(permit.users), set(permit.roles)

    output = await run_seed(permit, db, permit_client)

    assert (permit.instances, permit.users, permit.roles) == (instances, users, roles)
    assert "Restaurant pizza-palace: updated" in output
    assert "Roles: 0 of 20 assigned; the others were already assigned" in output


async def test_seed_updates_the_attributes_of_existing_restaurants(
    permit: FakePermit, db: Database, permit_client: Permit
) -> None:
    permit.instances = {
        "pizza-palace": {"name": "Old name", "allowed_for_children": False},
        "sushi-world": {},
    }
    output = await run_seed(permit, db, permit_client)

    assert permit.instances == RESTAURANTS
    assert "Restaurant pizza-palace: updated" in output
    assert "Restaurant fancy-french: created" in output
    patches = [r for r, _ in permit.api.log if r.method == "PATCH"]
    assert [r.path for r in patches] == [
        f"{FACTS}/resource_instances/{RESOURCE}:pizza-palace",
        f"{FACTS}/resource_instances/{RESOURCE}:sushi-world",
    ]
