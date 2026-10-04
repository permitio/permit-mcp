"""The app's SQLite database: family members, restaurants and their dishes.

A user's username is their Permit user key, and a restaurant's key is the key of its
`restaurants` resource instance in Permit.
"""

import asyncio
import dataclasses
from pathlib import Path
from typing import Literal, cast

import aiosqlite

from food_ordering.auth import check_password, hash_password

Role = Literal["parent", "child"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    username TEXT PRIMARY KEY,
    role TEXT NOT NULL CHECK (role IN ('parent', 'child')),
    password_hash BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS restaurants (
    key TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    allowed_for_children INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS dishes (
    restaurant_key TEXT NOT NULL REFERENCES restaurants (key),
    name TEXT NOT NULL,
    price REAL NOT NULL,
    PRIMARY KEY (restaurant_key, name)
);
"""

# The demo family. The passwords are documented in the README; this is a demo.
DEMO_USERS: tuple[tuple[str, Role, str], ...] = (
    ("joe", "parent", "joe_password"),
    ("jane", "parent", "jane_password"),
    ("henry", "child", "henry_password"),
    ("rose", "child", "rose_password"),
)
DEMO_RESTAURANTS: tuple[tuple[str, str, bool, tuple[tuple[str, float], ...]], ...] = (
    (
        "pizza-palace",
        "Pizza Palace",
        True,
        (("Cheese Pizza", 8.99), ("Pepperoni Pizza", 10.99), ("Veggie Pizza", 9.49)),
    ),
    (
        "burger-bonanza",
        "Burger Bonanza",
        True,
        (("Classic Burger", 7.99), ("Deluxe Burger", 12.99), ("Fries", 3.49)),
    ),
    (
        "fancy-french",
        "Fancy French",
        False,
        (("Escargot", 15.99), ("Foie Gras", 19.99), ("Truffle Pasta", 18.49)),
    ),
    (
        "sushi-world",
        "Sushi World",
        False,
        (("California Roll", 6.99), ("Sushi Platter", 22.99), ("Tempura", 9.99)),
    ),
)


@dataclasses.dataclass(frozen=True)
class User:
    """A family member. `username` is the Permit user key."""

    username: str
    role: Role


@dataclasses.dataclass(frozen=True)
class Restaurant:
    """A restaurant. `key` is the key of its Permit resource instance."""

    key: str
    name: str
    allowed_for_children: bool


@dataclasses.dataclass(frozen=True)
class Dish:
    """A dish on a restaurant's menu, with its price in dollars."""

    name: str
    price: float


class Database:
    """The app's data in one SQLite file. Each call opens its own connection."""

    def __init__(self, path: Path) -> None:
        """Use the database at `path`; `init()` creates it."""
        self._path = path

    async def init(self) -> None:
        """Create the tables, and add the demo family and restaurants to an empty database."""
        async with aiosqlite.connect(self._path) as db:
            await db.executescript(_SCHEMA)
            async with db.execute("SELECT COUNT(*) FROM users") as cursor:
                row = await cursor.fetchone()
            if row is not None and row[0] > 0:
                return
            users = [
                (username, role, await asyncio.to_thread(hash_password, password))
                for username, role, password in DEMO_USERS
            ]
            await db.executemany("INSERT INTO users VALUES (?, ?, ?)", users)
            await db.executemany(
                "INSERT INTO restaurants VALUES (?, ?, ?)",
                [(key, name, allowed) for key, name, allowed, _ in DEMO_RESTAURANTS],
            )
            await db.executemany(
                "INSERT INTO dishes VALUES (?, ?, ?)",
                [
                    (key, dish, price)
                    for key, _, _, dishes in DEMO_RESTAURANTS
                    for dish, price in dishes
                ],
            )
            await db.commit()

    async def authenticate(self, username: str, password: str) -> User | None:
        """Return the user when the password is theirs, otherwise None."""
        async with (
            aiosqlite.connect(self._path) as db,
            db.execute(
                "SELECT role, password_hash FROM users WHERE username = ?", (username,)
            ) as cursor,
        ):
            row = await cursor.fetchone()
        if row is None or not await asyncio.to_thread(check_password, password, row[1]):
            return None
        return User(username, cast("Role", row[0]))

    async def user(self, username: str) -> User | None:
        """Return the user with this username, or None."""
        async with (
            aiosqlite.connect(self._path) as db,
            db.execute("SELECT role FROM users WHERE username = ?", (username,)) as cursor,
        ):
            row = await cursor.fetchone()
        return None if row is None else User(username, cast("Role", row[0]))

    async def users(self) -> list[User]:
        """Return every family member."""
        async with (
            aiosqlite.connect(self._path) as db,
            db.execute("SELECT username, role FROM users ORDER BY username") as cursor,
        ):
            rows = await cursor.fetchall()
        return [User(row[0], cast("Role", row[1])) for row in rows]

    async def restaurants(self) -> list[Restaurant]:
        """Return every restaurant."""
        async with (
            aiosqlite.connect(self._path) as db,
            db.execute(
                "SELECT key, name, allowed_for_children FROM restaurants ORDER BY key"
            ) as cursor,
        ):
            rows = await cursor.fetchall()
        return [Restaurant(row[0], row[1], bool(row[2])) for row in rows]

    async def dishes(self, restaurant_key: str) -> list[Dish]:
        """Return the menu of a restaurant; empty for an unknown restaurant."""
        async with (
            aiosqlite.connect(self._path) as db,
            db.execute(
                "SELECT name, price FROM dishes WHERE restaurant_key = ? ORDER BY name",
                (restaurant_key,),
            ) as cursor,
        ):
            rows = await cursor.fetchall()
        return [Dish(row[0], row[1]) for row in rows]

    async def dish(self, restaurant_key: str, name: str) -> Dish | None:
        """Return one dish of a restaurant's menu, or None."""
        async with (
            aiosqlite.connect(self._path) as db,
            db.execute(
                "SELECT name, price FROM dishes WHERE restaurant_key = ? AND name = ?",
                (restaurant_key, name),
            ) as cursor,
        ):
            row = await cursor.fetchone()
        return None if row is None else Dish(row[0], row[1])
