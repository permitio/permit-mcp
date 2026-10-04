"""The backend: sign-in, and a chat websocket whose MCP tools act as the signed-in user.

`POST /token` checks a username and password and returns a JWT whose subject is the user's
Permit user key. `/ws/chat` verifies that JWT before it accepts the connection, gives the
session an MCP server bound to that user (`food_ordering.session`), and closes the
connection when the JWT expires.
"""

import argparse
import asyncio
import contextlib
import datetime
import json
import sys
from collections.abc import AsyncIterator, Sequence
from typing import Annotated, Literal

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, status
from google.genai import errors as genai_errors
from pydantic import BaseModel, Field, ValidationError

import permit_mcp
from food_ordering import session
from food_ordering.auth import issue_token, verify_token
from food_ordering.chat import (
    Chat,
    ChatError,
    ChatModel,
    GeminiChatModel,
    gemini_client,
    gemini_tools,
)
from food_ordering.config import Config, ConfigError, permit_sdk
from food_ordering.db import Database

CONFIG_ERROR_EXIT_CODE = 2
GEMINI_TIMEOUT_SECONDS = 120


class Credentials(BaseModel):
    """A sign-in request."""

    username: Annotated[str, Field(min_length=1)]
    password: Annotated[str, Field(min_length=1)]


class Token(BaseModel):
    """A signed-in user's bearer token."""

    access_token: str
    token_type: Literal["bearer"] = "bearer"  # noqa: S105 - the OAuth 2 token type, not a secret


class ChatMessage(BaseModel):
    """A message the user sends on the chat websocket."""

    message: Annotated[str, Field(min_length=1)]


def create_app(config: Config, model: ChatModel) -> FastAPI:
    """Build the backend.

    Args:
        config: The validated configuration.
        model: The language model the chat uses.

    Returns:
        The app. Its lifespan creates the database.

    """
    db = Database(config.db_path)
    permit = permit_sdk(config.permit)

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        await db.init()
        yield

    app = FastAPI(title="Family food ordering", lifespan=lifespan)

    @app.post("/token")
    async def sign_in(credentials: Credentials) -> Token:
        user = await db.authenticate(credentials.username, credentials.password)
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Incorrect username or password",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return Token(access_token=issue_token(user.username, config.jwt_secret))

    @app.websocket("/ws/chat")
    async def chat(websocket: WebSocket) -> None:
        scheme, _, token = websocket.headers.get("authorization", "").partition(" ")
        claims = verify_token(token, config.jwt_secret) if scheme.lower() == "bearer" else None
        user = None if claims is None else await db.user(claims.subject)
        if claims is None or user is None:
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Not signed in")
            return
        await websocket.accept()
        remaining = claims.expires - datetime.datetime.now(datetime.UTC)
        async with session.connect(config.permit, db, permit, user) as client:
            listed = await client.list_tools()
            conversation = Chat(model, client, gemini_tools(listed.tools))
            try:
                async with asyncio.timeout(remaining.total_seconds()):
                    await _converse(websocket, conversation)
            except WebSocketDisconnect:
                return
            except TimeoutError:
                await websocket.close(
                    code=status.WS_1008_POLICY_VIOLATION, reason="Sign-in expired; sign in again"
                )

    return app


async def _converse(websocket: WebSocket, conversation: Chat) -> None:
    """Answer each message with the chat's events, then `done`, until the client leaves."""
    while True:
        try:
            received = ChatMessage.model_validate(await websocket.receive_json())
            async for event in conversation.send(received.message):
                await websocket.send_json(event)
        except (json.JSONDecodeError, ValidationError):
            await websocket.send_json({"type": "error", "content": 'Send {"message": "<text>"}.'})
        except (ChatError, genai_errors.APIError) as exc:
            await websocket.send_json({"type": "error", "content": str(exc)})
        await websocket.send_json({"type": "done", "content": ""})


def main(argv: Sequence[str] | None = None) -> None:
    """Run the backend: the `food-ordering-server` command.

    Exits with status 2, before listening, when the configuration is missing or invalid,
    such as when FOOD_ORDERING_JWT_SECRET is unset.
    """
    parser = argparse.ArgumentParser(description="Run the family food-ordering backend.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)
    try:
        config = Config.from_env()
    except (ConfigError, permit_mcp.ConfigError) as exc:
        sys.stderr.write(f"food-ordering-server: configuration error: {exc}\n")
        sys.exit(CONFIG_ERROR_EXIT_CODE)
    # Lives as long as the process.
    http = httpx.AsyncClient(timeout=GEMINI_TIMEOUT_SECONDS)
    model = GeminiChatModel(gemini_client(config.gemini_api_key, http), config.gemini_model)
    uvicorn.run(create_app(config, model), host=args.host, port=args.port)
