import asyncio
import json
import httpx
import websockets
import sys
from typing import Dict, List, Optional

API_URL = "http://localhost:8000"  # Change if needed
WS_URL = "ws://localhost:8000"     # WebSocket URL


async def login(username: str, password: str) -> str | None:
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{API_URL}/token",
            data={
                "username": username,
                "password": password,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

        if response.status_code == 200:
            token = response.json().get("access_token")
            print("✅ Login successful!\n")
            return token
        else:
            print(f"❌ Login failed: {response.json().get('detail')}")
            return None


async def chat(token: str):
    history: List[Dict] = []
    is_processing = False  # Simple flag to track message processing state
    is_displayed_processing = False

    print("\n--- Chat session started ---")
    print("Type 'exit' to quit.\n")

    try:
        headers = {"Authorization": f"Bearer {token}"}

        async with websockets.connect(
            f"{WS_URL}/ws/chat",
            additional_headers=headers
        ) as websocket:
            # Start a background task for receiving messages
            async def receive_messages():
                nonlocal is_processing, history, is_displayed_processing

                while True:
                    try:
                        message = await websocket.recv()
                        data = json.loads(message)

                        message_type = data.get("type")
                        content = data.get("content")

                        if message_type == "text":
                            print(f"Assistant: {content}")
                        elif message_type == "status":
                            print(f"[Status] {content}")
                        elif message_type == "error":
                            print(f"⚠️ Error: {content}")
                            is_processing = False  # Unlock on error
                            is_displayed_processing = False
                        elif message_type == "history_update":
                            history = content
                            is_processing = False  # Unlock when complete
                            is_displayed_processing = False
                    except Exception as e:
                        print(f"\n⚠️ Error receiving message: {str(e)}")
                        is_processing = False
                        is_displayed_processing = False
                        break

            # Start the receiver task
            receiver_task = asyncio.create_task(receive_messages())

            # Main input loop
            while True:
                if is_processing:
                    if not is_displayed_processing:
                        print("⏳ Processing previous message. Please wait...")
                        is_displayed_processing = True

                    await asyncio.sleep(1)
                    continue

                user_input = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: input("You: ")
                )

                if user_input.lower() == "exit":
                    print("👋 Ending chat session.")
                    break

                # Set the processing flag before sending
                is_processing = True

                # Send the message
                payload = {
                    "message": user_input,
                    "history": history
                }

                try:
                    await websocket.send(json.dumps(payload))
                except Exception as e:
                    print(f"⚠️ Error sending message: {str(e)}")
                    is_processing = False
                    is_displayed_processing = False
                    break

            # Clean up
            receiver_task.cancel()

    except websockets.exceptions.WebSocketException as e:
        print(f"⚠️ WebSocket connection error: {str(e)}")
    except Exception as e:
        print(f"⚠️ Unexpected error: {str(e)}")


async def main():
    print("👋 Welcome Food Ordering CLI tool!")

    username = input("Username: ").strip()
    password = input("Password: ").strip()

    token = await login(username, password)

    if token:
        await chat(token)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 Program terminated by user.")
    except Exception as e:
        print(f"⚠️ Unexpected error: {str(e)}")
        sys.exit(1)
