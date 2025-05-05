from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from datetime import timedelta
from typing import Dict
from utils import *
from fastapi import Depends, FastAPI, HTTPException, status, WebSocket, WebSocketDisconnect
from fastapi.security import OAuth2PasswordRequestForm
import os
from contextlib import asynccontextmanager
from google import genai
from google.genai import types
import asyncio
from contextlib import AsyncExitStack
import json

ACCESS_TOKEN_EXPIRE_MINUTES = 30
DB_NAME = os.getenv("DB_NAME")
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')

server_params = StdioServerParameters(
    command="python",
    args=["food_ordering_mcp.py", DB_NAME],
    env=None,
)

genai_client = genai.Client(api_key=GEMINI_API_KEY)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield

# Create the app with lifespan
app = FastAPI(lifespan=lifespan)


class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[str, WebSocket] = {}

    async def connect(self, websocket: WebSocket, client_id: str):
        await websocket.accept()
        self.active_connections[client_id] = websocket

    def disconnect(self, client_id: str):
        if client_id in self.active_connections:
            del self.active_connections[client_id]

    async def send_message(self, message: str, client_id: str):
        if client_id in self.active_connections:
            await self.active_connections[client_id].send_text(message)


manager = ConnectionManager()


@app.post("/token")
async def login_for_access_token(form_data: OAuth2PasswordRequestForm = Depends()):
    user = get_user(form_data.username)
    if not user or not verify_password(form_data.password, user["hashed_password"]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(
        data={"sub": user["username"]}, expires_delta=access_token_expires
    )
    return {"access_token": access_token, "token_type": "bearer"}


@app.websocket("/ws/chat")
async def websocket_chat(websocket: WebSocket):
    current_user = await get_current_websocket_user(websocket)

    if not current_user:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Not authenticated")
        return

    # Check if user has a role
    if not current_user.get('role'):
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Insufficient permissions")
        return

    client_id = current_user.get('id')

    await manager.connect(websocket, client_id)
    try:
        exit_stack = AsyncExitStack()
        stdio_transport = await exit_stack.enter_async_context(stdio_client(server_params))
        stdio, write = stdio_transport
        session = await exit_stack.enter_async_context(ClientSession(stdio, write))

        await session.initialize()

        # Get MCP client and tools
        tools_result = await session.list_tools()

        filtered_mcp_tools = filter_tools_by_role(
            tools_result.tools,
            current_user['role']
        )

        mcp_tools = [
            {"function_declarations": convert_mcp_tools_to_gemini(
                filtered_mcp_tools)}
        ]

        contents = []

        while True:
            # Wait for messages from the client
            data = await websocket.receive_text()
            data = json.loads(data)

            message = data.get('message')
            history = data.get('history')

            # Use provided history or continue with existing conversation
            if history:
                contents = history

            # Add the new user message
            contents.append({
                "role": "user",
                "parts": [{"text": message}]
            })

            # Process messages and handle function calls
            has_more_function_calls = True

            while has_more_function_calls:
                # Call Gemini API
                response = genai_client.models.generate_content(
                    model="gemini-2.5-flash-preview-04-17",
                    contents=contents,
                    config=types.GenerateContentConfig(
                        tools=mcp_tools,
                        system_instruction=f"""
                        - **current_user_role**: {current_user.get('role')}
                        - **user_id**: "{current_user.get('id')}". This is the ID to be used for tool calls.
                        - **role**: "child-can-view". This is the role to requet for if a users wants to create an access request.
                        - **resource_instance** is required. Always specify this parameter as the ReBAC authorization model is been used in this system.
                        - **reason**: Ask the user to provide a value for the reason parameter directly, without generating one yourself.
                        NOTE: The only assignable role is **child-can-view**. Therefore, please do not prompt the user to specify a role—this role should be applied automatically when needed.
                        
                        ALWAYS begin by listing the available resource instances. These contain the list of restaurants users can order from, along with the corresponding IDs and keys needed for tool calls—since the `resource_instance` parameter is required for all tools.

                        Starting with this list allows you to:
                        - Show users the restaurants they can choose from before ordering a dish.
                        - Ensure you have access to the correct IDs and keys for any subsequent tool calls.
                        
                        NOTE: ALWAYS begin by listing the available resource instances using the list_resource_instances tool. 
                        """
                    )
                )

                # Store and send the model's text response
                if hasattr(response, 'text') and response.text:
                    # Add the model's response to the conversation history
                    contents.append({
                        "role": "model",
                        "parts": [{"text": response.text}]
                    })

                    # Send the text response immediately to the client
                    await manager.send_message(json.dumps({
                        "type": "text",
                        "content": response.text
                    }), client_id)

                function_calls = getattr(response, 'function_calls', None)
                if function_calls and len(function_calls) > 0:
                    # Inform client that function calls are being processed
                    await manager.send_message(json.dumps({
                        "type": "status",
                        "content": "Processing function calls..."
                    }), client_id)

                    contents.append({
                        "role": "model",
                        "parts": [{"function_call": {
                                    "id": fc.id,
                                    "name": fc.name,
                                    "args": fc.args
                                    }
                                   } for fc in function_calls]
                    })

                # Check if there are function calls to process
                if not function_calls or len(function_calls) == 0:
                    # No more function calls, exit the loop
                    has_more_function_calls = False
                    continue

                # Process all function calls in parallel
                async def process_function_call(function_call):
                    name = function_call.name
                    args = function_call.args

                    try:
                        print(name, args)
                        tool_result = await retry_tool_call(session, name, args)
                        converted_content = []
                        for text_content in tool_result.content:
                            # Assuming TextContent has a 'text' attribute that holds the message content
                            converted_content.append(
                                {"text": text_content.text})
                        return {
                            "name": name,
                            "response": {"result": {"content": converted_content, "is_error": tool_result.isError}}
                        }
                    except Exception as error:
                        error_message = getattr(error, 'detail', str(error)) if hasattr(
                            error, 'detail') else str(error)
                        return {
                            "name": name,
                            "response": {
                                "result": {
                                    "error": error_message or "Tool execution failed after multiple attempts"
                                }
                            }
                        }

                function_call_tasks = [
                    process_function_call(fc) for fc in function_calls]
                results = await asyncio.gather(*function_call_tasks)

                # Add function responses to conversation
                contents.append({
                    "role": "user",
                    "parts": [{"function_response": result} for result in results]
                })

            # Send the full updated history to the client
            await manager.send_message(json.dumps({
                "type": "history_update",
                "content": contents
            }), client_id)

    except WebSocketDisconnect:
        manager.disconnect(client_id)
    except Exception as err:
        print(err)
        # Send error message to client
        await manager.send_message(json.dumps({
            "type": "error",
            "content": "An error occurred: " + str(err)
        }), client_id)
    finally:
        manager.disconnect(client_id)
        await exit_stack.aclose()
