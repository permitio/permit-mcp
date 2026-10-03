"""Fixtures: a temporary database, and a local stand-in for the Permit API and PDP.

The stand-in is the HTTP boundary that both the Permit MCP tools and the Permit SDK talk
to. It answers as Permit does (the SDK parses its answers into Permit's models) and records
every request, so a test reads which Permit user each request acted as from the wire.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import re
import shutil
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote

import pytest
from food_ordering.config import permit_sdk
from food_ordering.db import Database
from pytest_httpserver import HTTPServer
from werkzeug import Request, Response

from permit_mcp import Settings

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from permit import Permit

API_KEY = "permit_key_EXAMPLETESTS"
ORGANIZATION = "00000000-0000-4000-8000-0000000000a1"
PROJECT = "00000000-0000-4000-8000-0000000000b2"
ENVIRONMENT = "00000000-0000-4000-8000-0000000000c3"
SCOPE = {"organization_id": ORGANIZATION, "project_id": PROJECT, "environment_id": ENVIRONMENT}
FACTS = f"/v2/facts/{PROJECT}/{ENVIRONMENT}"
ELEMENT_TOKEN = "element_token_EXAMPLETESTS"  # noqa: S105 - a fake value the stand-in serves
REQUEST_ID = "00000000-0000-4000-8000-000000000001"
RESOURCE = "restaurants"
TENANT = "default"

ENV_VARIABLES = (
    "PERMIT_API_KEY",
    "PERMIT_RESOURCE",
    "PERMIT_TENANT",
    "PERMIT_API_URL",
    "PERMIT_PDP_URL",
    "PERMIT_ACCESS_REQUEST_ELEMENT",
    "PERMIT_OPERATION_APPROVAL_ELEMENT",
    "PERMIT_MCP_USER",
    "FOOD_ORDERING_JWT_SECRET",
    "FOOD_ORDERING_DB",
    "GEMINI_API_KEY",
    "GEMINI_MODEL",
    "GOOGLE_API_KEY",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
    "NETRC",
)

# A role assignment: user, role, resource instance ("restaurants:pizza-palace").
Assignment = tuple[str, str, str]


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the developer's own configuration, keys and proxies out of every test."""
    for name in ENV_VARIABLES:
        monkeypatch.delenv(name, raising=False)


class FakePermit:
    """The Permit API and PDP.

    The API keeps resource instances, users and role assignments, and answers a duplicate
    with 409 and a missing role assignment with 404, as Permit does. The PDP allows exactly
    what `grants` lists; it is kept apart from the role assignments because Permit's PDP
    can still answer from before a change for a moment.
    """

    def __init__(self, api: HTTPServer, pdp: HTTPServer) -> None:
        self.api = api
        self.pdp = pdp
        self.grants: set[tuple[str, str, str]] = set()
        self.instances: dict[str, dict[str, object]] = {}
        self.users: dict[str, dict[str, object]] = {}
        self.roles: set[Assignment] = set()
        for server in (api, pdp):
            server.clear()
        api.expect_request(re.compile(".*")).respond_with_handler(self._answer_api)
        pdp.expect_request("/allowed", method="POST").respond_with_handler(self._answer_pdp)

    def grant(self, user: str, action: str, restaurant: str) -> None:
        """Make the PDP allow `user` to perform `action` on a restaurant."""
        self.grants.add((user, action, restaurant))

    def assign(self, user: str, role: str, restaurant: str) -> None:
        """Assign a role on a restaurant, as Permit's API holds it."""
        self.roles.add((user, role, f"{RESOURCE}:{restaurant}"))

    @property
    def settings(self) -> Settings:
        return Settings(
            api_key=API_KEY,
            resource=RESOURCE,
            api_url=self.api.url_for("").rstrip("/"),
            pdp_url=self.pdp.url_for("").rstrip("/"),
            access_request_element="restaurant-requests",
            operation_approval_element="dish-requests",
        )

    def requests(self) -> list[Request]:
        """Every request the API and the PDP received, the API's first."""
        return [request for request, _ in [*self.api.log, *self.pdp.log]]

    def wire_text(self) -> str:
        """Every request's path, query and body, for checking what never crossed the wire."""
        return "\n".join(
            f"{request.method} {unquote(request.full_path)} {request.get_data(as_text=True)}"
            for request in self.requests()
        )

    def acting_users(self) -> set[str]:
        """The Permit users the requests named, in a path or a body."""
        users: set[str] = set()
        for request in self.requests():
            segments = unquote(request.path).split("/")
            for index, segment in enumerate(segments[:-1]):
                if segment in {"user", "users"}:
                    users.add(segments[index + 1])
            body = request.get_json(silent=True)
            if isinstance(body, dict):
                if isinstance(body.get("user"), dict):
                    users.add(body["user"]["key"])
                if isinstance(body.get("user_id"), str):
                    users.add(body["user_id"])
        return users

    def _answer_api(self, request: Request) -> Response:
        route = (request.method, request.path)
        if route == ("GET", "/v2/api-key/scope"):
            return _json(SCOPE)
        if route == ("POST", "/v2/auth/elements_login_as"):
            return _json({"element_bearer_token": ELEMENT_TOKEN, "error": None})
        if request.path.startswith(f"{FACTS}/"):
            return self._answer_facts(request, request.path.removeprefix(f"{FACTS}/"))
        return _request_answer(request)

    # One return per route Permit serves; the stand-in answers like each of them.
    def _answer_facts(self, request: Request, path: str) -> Response:  # noqa: PLR0911
        body: Any = request.get_json(silent=True)
        match (request.method, path.split("/")):
            case ("GET", ["resource_instances"]):
                return _json([_instance(key, attrs) for key, attrs in self.instances.items()])
            case ("POST", ["resource_instances"]):
                if body["key"] in self.instances:
                    return _error(409, "DUPLICATE_ENTITY")
                self.instances[body["key"]] = body.get("attributes", {})
                return _json(_instance(body["key"], self.instances[body["key"]]))
            case ("PATCH", ["resource_instances", identity]):
                key = identity.removeprefix(f"{RESOURCE}:")
                if key not in self.instances:
                    return _error(404, "NOT_FOUND")
                self.instances[key] = body.get("attributes", {})
                return _json(_instance(key, self.instances[key]))
            case ("PUT", ["users", key]):
                self.users[key] = body
                return _json(_user(key))
            case ("POST", ["role_assignments", "bulk"]):
                wanted = {(item["user"], item["role"], item["resource_instance"]) for item in body}
                created = wanted - self.roles
                self.roles |= created
                return _json({"assignments_created": len(created)})
            case ("DELETE", ["users", user, "roles"]):
                assignment = (user, body["role"], body["resource_instance"])
                if assignment not in self.roles:
                    return _error(404, "NOT_FOUND")
                self.roles.remove(assignment)
                return Response(status=204)
            case _:
                return _request_answer(request)

    def _answer_pdp(self, request: Request) -> Response:
        body = request.get_json()
        asked = (body["user"]["key"], body["action"], body["resource"].get("key", ""))
        return _json({"allow": asked in self.grants})


def _request_answer(request: Request) -> Response:
    """An access request or operation approval endpoint: a page, or the request."""
    if request.method == "GET":
        return _json({"data": [], "total_count": 0, "page_count": 0})
    return _json({"id": REQUEST_ID, "status": "pending"})


def _ids() -> dict[str, str]:
    now = datetime.datetime.now(datetime.UTC).isoformat()
    return {
        "id": REQUEST_ID,
        "organization_id": ORGANIZATION,
        "project_id": PROJECT,
        "environment_id": ENVIRONMENT,
        "created_at": now,
        "updated_at": now,
    }


def _instance(key: str, attributes: dict[str, object]) -> dict[str, object]:
    return {
        **_ids(),
        "key": key,
        "tenant": TENANT,
        "resource": RESOURCE,
        "resource_id": REQUEST_ID,
        "tenant_id": REQUEST_ID,
        "attributes": attributes,
    }


def _user(key: str) -> dict[str, object]:
    return {**_ids(), "key": key}


def _error(status: int, code: str) -> Response:
    return _json(
        {"id": REQUEST_ID, "title": code.replace("_", " ").title(), "error_code": code},
        status=status,
    )


def _json(payload: object, *, status: int = 200) -> Response:
    return Response(json.dumps(payload), status=status, content_type="application/json")


def _serve() -> Iterator[HTTPServer]:
    server = HTTPServer(host="127.0.0.1", port=0, threaded=True)
    server.start()
    yield server
    server.clear()
    server.stop()


@pytest.fixture(scope="session")
def api_server() -> Iterator[HTTPServer]:
    yield from _serve()


@pytest.fixture(scope="session")
def pdp_server() -> Iterator[HTTPServer]:
    yield from _serve()


@pytest.fixture
def permit(api_server: HTTPServer, pdp_server: HTTPServer) -> FakePermit:
    return FakePermit(api_server, pdp_server)


@pytest.fixture
def permit_client(permit: FakePermit) -> Permit:
    """The Permit SDK, talking to the stand-in."""
    return permit_sdk(permit.settings)


@pytest.fixture(scope="session")
def template_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A database with the demo data, built once: bcrypt makes building it slow."""
    path = tmp_path_factory.mktemp("template") / "food_ordering.db"
    asyncio.run(Database(path).init())
    return path


@pytest.fixture
def db_path(template_db: Path, tmp_path: Path) -> Path:
    path = tmp_path / "food_ordering.db"
    shutil.copyfile(template_db, path)
    return path


@pytest.fixture
def db(db_path: Path) -> Database:
    return Database(db_path)
