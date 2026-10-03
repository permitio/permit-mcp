"""The chat: Gemini decides which MCP tools to call, and the session's MCP client calls them.

The system instruction says nothing about who the user is. The tools already act as the
signed-in user, so the model never needs, or gets, a user ID to pass along.
"""

from collections.abc import AsyncIterator, Sequence
from typing import Any, Literal, Protocol, TypedDict

import httpx
from google import genai
from google.genai import types
from mcp.client import Client
from mcp.types import CallToolResult, TextContent
from mcp.types import Tool as McpTool

from food_ordering.tools import CHILD_PRICE_LIMIT, CHILD_ROLE

# A turn that keeps calling tools past this many rounds is stopped.
MAX_TOOL_ROUNDS = 10

SYSTEM_INSTRUCTION = f"""\
You help a family order food. The user is already signed in, and every tool acts as them.

- Start by listing the restaurants with list_resource_instances. Each instance's key is the
  restaurant key the other tools take as `restaurant` or `resource_instance`; its attributes
  hold the restaurant's name. Always pass `resource_instance`.
- To see a restaurant's menu the user needs access. If list_dishes says they have none, offer
  to request the {CHILD_ROLE!r} role on that restaurant with create_access_request.
- A child needs a parent's one-time approval to order a dish above ${CHILD_PRICE_LIMIT:.2f}.
  If order_dish says so, offer to request it with create_operation_approval.
- Before creating a request, ask the user for the reason. Never write one yourself.
- Parents can list, approve and deny the requests.
"""


class ChatEvent(TypedDict):
    """One message to the chat client: the model's text, a tool call, or an error."""

    type: Literal["text", "tool", "error"]
    content: str


class ChatModel(Protocol):
    """The language model: given the conversation and the tools, return its next message."""

    async def generate(
        self, contents: Sequence[types.Content], tools: Sequence[types.Tool]
    ) -> types.Content:
        """Return the model's next message."""
        ...


class ChatError(Exception):
    """The model gave no usable answer."""


def gemini_client(
    api_key: str, http: httpx.AsyncClient, base_url: str | None = None
) -> genai.Client:
    """Return a Gemini client that sends its requests through `http`, which the caller closes.

    Without a client of its own, google-genai uses aiohttp whenever it is installed (the
    Permit MCP tools need it), through a subclass of `aiohttp.ClientSession` that aiohttp
    warns is deprecated.

    Args:
        api_key: The Gemini API key.
        http: The HTTP client.
        base_url: Another endpoint for the Gemini API, such as a gateway; the public one
            when None.

    """
    options = types.HttpOptions(base_url=base_url, httpx_async_client=http)
    return genai.Client(api_key=api_key, http_options=options)


class GeminiChatModel:
    """A `ChatModel` on the Gemini API."""

    def __init__(self, client: genai.Client, model: str) -> None:
        """Use `model` through `client`."""
        self._client = client
        self._model = model

    async def generate(
        self, contents: Sequence[types.Content], tools: Sequence[types.Tool]
    ) -> types.Content:
        """Return Gemini's next message, with its function calls and thought signatures.

        Raises:
            ChatError: Gemini returned no candidate.

        """
        response = await self._client.aio.models.generate_content(
            model=self._model,
            contents=list(contents),
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_INSTRUCTION,
                tools=list(tools),
                # `Chat` calls the tools itself, through the session's MCP client.
                automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            ),
        )
        if not response.candidates or response.candidates[0].content is None:
            msg = "Gemini returned no answer."
            raise ChatError(msg)
        return response.candidates[0].content


def gemini_tools(tools: Sequence[McpTool]) -> list[types.Tool]:
    """Declare the MCP tools to Gemini, with their input schemas as they are."""
    return [
        types.Tool(
            function_declarations=[
                types.FunctionDeclaration(
                    name=tool.name,
                    description=tool.description,
                    parameters_json_schema=tool.input_schema,
                )
                for tool in tools
            ]
        )
    ]


class Chat:
    """One user's conversation, kept on the server for the session's lifetime."""

    def __init__(self, model: ChatModel, client: Client, tools: list[types.Tool]) -> None:
        """Talk to `model`, and call its tools through `client`."""
        self._model = model
        self._client = client
        self._tools = tools
        self._history: list[types.Content] = []

    async def send(self, message: str) -> AsyncIterator[ChatEvent]:
        """Add the user's message, and yield what the model says and does until it is done."""
        self._history.append(types.Content(role="user", parts=[types.Part(text=message)]))
        for _ in range(MAX_TOOL_ROUNDS):
            reply = await self._model.generate(self._history, self._tools)
            # Appended as it came, so Gemini gets its thought signatures back.
            self._history.append(reply)
            parts = reply.parts or []
            text = "".join(part.text for part in parts if part.text and not part.thought)
            if text:
                yield {"type": "text", "content": text}
            calls = [part.function_call for part in parts if part.function_call is not None]
            if not calls:
                return
            responses: list[types.Part] = []
            for call in calls:
                yield {"type": "tool", "content": call.name or ""}
                responses.append(await self._call_tool(call))
            self._history.append(types.Content(role="user", parts=responses))
        yield {
            "type": "error",
            "content": f"Stopped after {MAX_TOOL_ROUNDS} rounds of tool calls. Ask again.",
        }

    async def _call_tool(self, call: types.FunctionCall) -> types.Part:
        name = call.name or ""
        result = await self._client.call_tool(name, call.args or {})
        response: dict[str, Any] = (
            {"error": _text_of(result)} if result.is_error else {"output": _output_of(result)}
        )
        return types.Part(
            function_response=types.FunctionResponse(id=call.id, name=name, response=response)
        )


def _text_of(result: CallToolResult) -> str:
    return "\n".join(item.text for item in result.content if isinstance(item, TextContent))


def _output_of(result: CallToolResult) -> object:
    if result.structured_content is not None:
        return result.structured_content
    return _text_of(result)
