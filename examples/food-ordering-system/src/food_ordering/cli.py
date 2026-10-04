"""The `food-ordering-chat` command: sign in, then chat with the backend in the terminal."""

import argparse
import asyncio
import json
import sys

import httpx
from rich.console import Console
from rich.markdown import Markdown
from rich.text import Text
from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

EXIT_WORDS = frozenset({"exit", "quit"})


def sign_in(url: str, username: str, password: str) -> str | None:
    """Return the bearer token for these credentials, or None when they are wrong."""
    response = httpx.post(f"{url}/token", json={"username": username, "password": password})
    if response.status_code == httpx.codes.UNAUTHORIZED:
        return None
    response.raise_for_status()
    token: str = response.json()["access_token"]
    return token


async def chat(url: str, token: str, console: Console) -> None:
    """Send what the user types, and show the backend's answers, until they type exit."""
    websocket_url = "ws" + url.removeprefix("http") + "/ws/chat"
    async with connect(
        websocket_url, additional_headers={"Authorization": f"Bearer {token}"}
    ) as ws:
        while True:
            message = (await asyncio.to_thread(console.input, "[bold]You:[/] ")).strip()
            if message.lower() in EXIT_WORDS:
                return
            if not message:
                continue
            await ws.send(json.dumps({"message": message}))
            while True:
                event = json.loads(await ws.recv())
                if event["type"] == "done":
                    break
                if event["type"] == "text":
                    console.print(Markdown(event["content"]))
                elif event["type"] == "tool":
                    console.print(Text(f"[calling {event['content']}]", style="dim"))
                else:
                    console.print(Text(event["content"], style="red"))


def main() -> None:
    """Sign in and chat. Exits with status 1 when sign-in or the connection fails."""
    parser = argparse.ArgumentParser(description="Chat with the family food-ordering backend.")
    parser.add_argument("--url", default="http://127.0.0.1:8000", help="the backend's URL")
    url = parser.parse_args().url.rstrip("/")
    console = Console()
    username = console.input("Username: ").strip()
    password = console.input("Password: ", password=True)
    try:
        token = sign_in(url, username, password)
        if token is None:
            console.print(Text("Incorrect username or password.", style="red"))
            sys.exit(1)
        console.print("Signed in. Type exit to leave.")
        asyncio.run(chat(url, token, console))
    except (httpx.HTTPError, WebSocketException, OSError) as exc:
        console.print(Text(f"Could not reach {url}: {exc}", style="red"))
        sys.exit(1)
