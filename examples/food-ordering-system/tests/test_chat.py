"""The Gemini adapter, against a local stand-in for the Gemini API, and the chat loop's limit."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from food_ordering import chat, session
from food_ordering.chat import (
    MAX_TOOL_ROUNDS,
    Chat,
    GeminiChatModel,
    gemini_client,
    gemini_tools,
)
from food_ordering.db import User
from google.genai import types
from pytest_httpserver import HTTPServer

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator, Sequence

    from food_ordering.db import Database
    from google import genai
    from permit import Permit

    from tests.conftest import FakePermit

MODEL = "gemini-test"
ANSWER = {"candidates": [{"content": {"role": "model", "parts": [{"text": "Hello, Henry."}]}}]}


@pytest.fixture
def gemini() -> Iterator[HTTPServer]:
    server = HTTPServer(host="127.0.0.1", port=0)
    server.start()
    yield server
    server.clear()
    server.stop()


@pytest.fixture
async def client(gemini: HTTPServer) -> AsyncIterator[genai.Client]:
    async with httpx.AsyncClient() as http:
        yield gemini_client("gemini-test-key", http, base_url=gemini.url_for("/"))


async def test_gemini_gets_the_tools_schemas_and_no_user(
    gemini: HTTPServer,
    client: genai.Client,
    permit: FakePermit,
    db: Database,
    permit_client: Permit,
) -> None:
    gemini.expect_oneshot_request(
        f"/v1beta/models/{MODEL}:generateContent", method="POST"
    ).respond_with_json(ANSWER)
    async with session.connect(permit.settings, db, permit_client, User("henry", "child")) as mcp:
        listed = (await mcp.list_tools()).tools
    question = types.Content(role="user", parts=[types.Part(text="What can I order?")])

    reply = await GeminiChatModel(client, MODEL).generate([question], gemini_tools(listed))

    assert reply.parts is not None
    assert reply.parts[0].text == "Hello, Henry."
    (request, _), *_ = gemini.log
    sent: dict[str, Any] = request.get_json()
    assert request.headers["x-goog-api-key"] == "gemini-test-key"
    assert sent["systemInstruction"]["parts"][0]["text"] == chat.SYSTEM_INSTRUCTION
    declared = sent["tools"][0]["functionDeclarations"]
    # google-genai sends the proto field name; the API's JSON parser accepts either spelling.
    schemas = {
        item["name"]: item.get("parametersJsonSchema", item.get("parameters_json_schema"))
        for item in declared
    }
    assert schemas == {tool.name: tool.input_schema for tool in listed}
    assert "henry" not in json.dumps(sent).lower().replace("hello, henry", "")


async def test_an_empty_answer_is_an_error(gemini: HTTPServer, client: genai.Client) -> None:
    gemini.expect_oneshot_request(
        f"/v1beta/models/{MODEL}:generateContent", method="POST"
    ).respond_with_json({"candidates": []})
    question = types.Content(role="user", parts=[types.Part(text="Hi")])
    with pytest.raises(chat.ChatError, match="no answer"):
        await GeminiChatModel(client, MODEL).generate([question], [])


class LoopingModel:
    """Asks for a tool on every turn."""

    def __init__(self) -> None:
        self.turns = 0

    async def generate(
        self, contents: Sequence[types.Content], tools: Sequence[types.Tool]
    ) -> types.Content:
        del contents, tools
        self.turns += 1
        call = types.FunctionCall(name="list_resource_instances", args={})
        return types.Content(role="model", parts=[types.Part(function_call=call)])


async def test_a_turn_stops_after_the_tool_round_limit(
    permit: FakePermit, db: Database, permit_client: Permit
) -> None:
    model = LoopingModel()
    async with session.connect(permit.settings, db, permit_client, User("joe", "parent")) as mcp:
        events = [event async for event in Chat(model, mcp, []).send("loop")]
    assert model.turns == MAX_TOOL_ROUNDS
    assert events[-1]["type"] == "error"
    assert [event["type"] for event in events[:-1]] == ["tool"] * MAX_TOOL_ROUNDS
