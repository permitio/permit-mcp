"""The backend over HTTP: sign-in, the chat websocket's authentication, and a chat turn.

The language model is a scripted stand-in; the Permit API and PDP are the local stand-ins.
"""

from __future__ import annotations

import datetime
import time
from typing import TYPE_CHECKING, Any

import jwt
import pytest
from fastapi.testclient import TestClient
from food_ordering.app import create_app
from food_ordering.auth import ALGORITHM, TOKEN_LIFETIME, issue_token
from food_ordering.config import Config
from food_ordering.session import CHILD_TOOLS
from food_ordering.tools import APPROVED_ROLE
from google.genai import types
from starlette.websockets import WebSocketDisconnect

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence
    from pathlib import Path

    from tests.conftest import FakePermit

SECRET = "s" * 64  # long enough for HS512, so the algorithm cases sign with it
OTHER_SECRET = "o" * 64
CLOSED_BY_POLICY = 1008


class ScriptedModel:
    """Answers with the next of its replies, and keeps what it was sent."""

    def __init__(self, *replies: types.Content) -> None:
        self.replies = list(replies)
        self.sent: list[list[types.Content]] = []
        self.declared: list[str] = []

    async def generate(
        self, contents: Sequence[types.Content], tools: Sequence[types.Tool]
    ) -> types.Content:
        self.sent.append(list(contents))
        self.declared = [
            declaration.name or ""
            for tool in tools
            for declaration in tool.function_declarations or []
        ]
        return self.replies.pop(0)


def function_call(name: str, args: dict[str, Any]) -> types.Content:
    call = types.FunctionCall(id="call-1", name=name, args=args)
    return types.Content(role="model", parts=[types.Part(function_call=call)])


def text(reply: str) -> types.Content:
    return types.Content(role="model", parts=[types.Part(text=reply)])


@pytest.fixture
def config(permit: FakePermit, db_path: Path) -> Config:
    return Config(
        permit=permit.settings,
        jwt_secret=SECRET,
        db_path=db_path,
        gemini_api_key="unused",
        gemini_model="unused",
    )


@pytest.fixture
def model() -> ScriptedModel:
    return ScriptedModel()


@pytest.fixture
def client(config: Config, model: ScriptedModel) -> Iterator[TestClient]:
    with TestClient(create_app(config, model)) as test_client:
        yield test_client


def sign_in(client: TestClient, username: str, password: str) -> str:
    response = client.post("/token", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    token: str = response.json()["access_token"]
    return token


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# --- sign-in ----------------------------------------------------------------------


def test_sign_in_returns_a_token_for_the_user(client: TestClient) -> None:
    response = client.post("/token", json={"username": "henry", "password": "henry_password"})
    assert response.status_code == 200
    assert response.json()["token_type"] == "bearer"  # noqa: S105 - the OAuth 2 token type
    claims = jwt.decode(response.json()["access_token"], SECRET, algorithms=[ALGORITHM])
    assert claims["sub"] == "henry"
    assert claims["exp"] - claims["iat"] == TOKEN_LIFETIME.total_seconds()


@pytest.mark.parametrize(
    ("username", "password"),
    [("henry", "joe_password"), ("ghost", "henry_password"), ("henry", "x" * 100)],
    ids=["wrong password", "unknown user", "over bcrypt's limit"],
)
def test_sign_in_refuses_wrong_credentials(
    client: TestClient, username: str, password: str
) -> None:
    response = client.post("/token", json={"username": username, "password": password})
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert "access_token" not in response.json()


def test_sign_in_needs_a_username_and_password(client: TestClient) -> None:
    assert client.post("/token", json={"username": "henry"}).status_code == 422


# --- the chat websocket's authentication ------------------------------------------


def token_with(algorithm: str, key: str) -> str:
    """A token with valid claims for henry, signed with another algorithm, or none."""
    now = datetime.datetime.now(datetime.UTC)
    claims = {"sub": "henry", "iat": now, "exp": now + TOKEN_LIFETIME}
    return jwt.encode(claims, key, algorithm=algorithm)


def expired_token() -> str:
    long_ago = datetime.datetime.now(datetime.UTC) - TOKEN_LIFETIME - datetime.timedelta(minutes=1)
    return issue_token("henry", SECRET, now=long_ago)


@pytest.mark.parametrize(
    "headers",
    [
        pytest.param(dict, id="no token"),
        pytest.param(lambda: {"Authorization": "Basic aGVucnk6aGVucnk="}, id="not bearer"),
        pytest.param(lambda: bearer("not-a-jwt"), id="not a JWT"),
        pytest.param(lambda: bearer(issue_token("henry", OTHER_SECRET)), id="another key"),
        pytest.param(lambda: bearer(expired_token()), id="expired"),
        pytest.param(lambda: bearer(issue_token("ghost", SECRET)), id="unknown user"),
        pytest.param(
            lambda: bearer(jwt.encode({"sub": "henry"}, SECRET, algorithm=ALGORITHM)),
            id="no expiry",
        ),
        pytest.param(lambda: bearer(token_with("HS384", SECRET)), id="HS384"),
        pytest.param(lambda: bearer(token_with("HS512", SECRET)), id="HS512"),
        pytest.param(lambda: bearer(token_with("none", "")), id="alg none"),
    ],
)
def test_the_chat_refuses_a_connection_without_a_valid_token(
    client: TestClient, permit: FakePermit, headers: Callable[[], dict[str, str]]
) -> None:
    with (
        pytest.raises(WebSocketDisconnect) as refused,
        client.websocket_connect("/ws/chat", headers=headers()),
    ):
        pass
    assert refused.value.code == CLOSED_BY_POLICY
    assert permit.requests() == []


# --- a chat turn --------------------------------------------------------------------


def converse(client: TestClient, token: str, message: object) -> list[dict[str, str]]:
    events: list[dict[str, str]] = []
    with client.websocket_connect("/ws/chat", headers=bearer(token)) as websocket:
        websocket.send_json(message)
        while not events or events[-1]["type"] != "done":
            events.append(websocket.receive_json())
    return events


def test_a_chat_turn_orders_as_the_signed_in_user_whatever_the_model_says(
    client: TestClient, permit: FakePermit, model: ScriptedModel
) -> None:
    permit.grant("henry", "read", "pizza-palace")
    permit.grant("henry", "operate", "pizza-palace")
    permit.assign("henry", APPROVED_ROLE, "pizza-palace")
    arguments = {"restaurant": "pizza-palace", "dish": "Pepperoni Pizza", "user_id": "joe"}
    model.replies = [function_call("order_dish", arguments), text("Your pizza is on its way.")]
    token = sign_in(client, "henry", "henry_password")

    events = converse(client, token, {"message": "Order a pepperoni pizza for joe"})

    assert events == [
        {"type": "tool", "content": "order_dish"},
        {"type": "text", "content": "Your pizza is on its way."},
        {"type": "done", "content": ""},
    ]
    assert permit.acting_users() == {"henry"}
    assert "joe" not in permit.wire_text()
    answered = model.sent[1][-1].parts or []
    response = answered[0].function_response
    assert response is not None
    assert response.id == "call-1"
    assert response.response == {
        "output": {
            "status": "ordered",
            "restaurant": "pizza-palace",
            "dish": "Pepperoni Pizza",
            "price": 10.99,
            "used_approval": True,
        }
    }


def test_the_model_is_offered_the_session_s_tools(client: TestClient, model: ScriptedModel) -> None:
    model.replies = [text("Hello")]
    converse(client, sign_in(client, "rose", "rose_password"), {"message": "hi"})
    assert set(model.declared) == CHILD_TOOLS


def test_a_tool_error_goes_back_to_the_model(
    client: TestClient, permit: FakePermit, model: ScriptedModel
) -> None:
    model.replies = [
        function_call("approve_access_request", {"access_request_id": "x", "user_id": "joe"}),
        text("Only a parent can approve requests."),
    ]
    events = converse(client, sign_in(client, "henry", "henry_password"), {"message": "approve"})
    assert events[-2:] == [
        {"type": "text", "content": "Only a parent can approve requests."},
        {"type": "done", "content": ""},
    ]
    response = (model.sent[1][-1].parts or [])[0].function_response
    assert response is not None
    assert response.response is not None
    assert "error" in response.response
    assert permit.requests() == []


def test_a_malformed_message_gets_an_error_and_the_chat_goes_on(
    client: TestClient, model: ScriptedModel
) -> None:
    token = sign_in(client, "joe", "joe_password")
    assert converse(client, token, {"text": "hi"}) == [
        {"type": "error", "content": 'Send {"message": "<text>"}.'},
        {"type": "done", "content": ""},
    ]
    assert model.sent == []


def test_the_chat_closes_when_the_sign_in_expires(client: TestClient, model: ScriptedModel) -> None:
    # A chat still open after the expiry would answer this message instead of closing.
    model.replies = [text("Still here")]
    almost_expired = (
        datetime.datetime.now(datetime.UTC) - TOKEN_LIFETIME + datetime.timedelta(seconds=1)
    )
    token = issue_token("henry", SECRET, now=almost_expired)
    with client.websocket_connect("/ws/chat", headers=bearer(token)) as websocket:
        time.sleep(2)
        websocket.send_json({"message": "Are you there?"})
        with pytest.raises(WebSocketDisconnect) as closed:
            websocket.receive_json()
    assert closed.value.code == CLOSED_BY_POLICY
    assert closed.value.reason == "Sign-in expired; sign in again"
    assert model.sent == []
