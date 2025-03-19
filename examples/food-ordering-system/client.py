import asyncio
from typing import Optional
from contextlib import AsyncExitStack
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from anthropic import Anthropic
from dotenv import load_dotenv
from rich.console import Console
from rich.prompt import Prompt

load_dotenv()  # load environment variables from .env
console = Console()


class MCPClient:

    def __init__(self):
        # Initialize session and client objects
        self.session: Optional[ClientSession] = None
        self.exit_stack = AsyncExitStack()
        self.anthropic = Anthropic()
        self.username = None
        self.role = None
        self.messages = []

    async def connect_to_server(self, server_script_path: str):
        """Connect to an MCP server

        Args:
          server_script_path: Path to the server script (.py or .js)
        """
        is_python = server_script_path.endswith('.py')
        is_js = server_script_path.endswith('.js')
        if not (is_python or is_js):
            raise ValueError("Server script must be a .py or .js file")

        command = "python" if is_python else "node"
        server_params = StdioServerParameters(
            command=command,
            args=[server_script_path],
            env=None
        )

        stdio_transport = await self.exit_stack.enter_async_context(stdio_client(server_params))
        self.stdio, self.write = stdio_transport
        self.session = await self.exit_stack.enter_async_context(ClientSession(self.stdio, self.write))

        await self.session.initialize()

        # List available tools and resources
        response = await self.session.list_tools()
        tools = response.tools

        console.print("\nConnected to server with tools:",
                      [tool.name for tool in tools])

    async def process_query(self, query: str) -> str:
        """Process a query using Claude and available tools"""
        self.messages = [
            *self.messages,
            {
                "role": "user",
                "content": query
            }
        ]

        response = await self.session.list_tools()
        available_tools = []
        reserved_tools = ['list_pending_restaurant_request', 'list_pending_dish_request',
                          'approve_access_request', 'approve_operation_approval', 'deny_access_request', 'deny_operation_approval']

        available_tools = [
            {
                "name": tool.name,
                "description": tool.description,
                "input_schema": tool.inputSchema
            }
            for tool in response.tools
            if tool.name != 'verify_access'
        ]

        if self.role != 'parent':
            available_tools = [
                tool for tool in available_tools
                if tool["name"] not in reserved_tools
            ]

        # Initial Claude API call
        response = self.anthropic.messages.create(
            model="claude-3-5-sonnet-20241022",
            max_tokens=1000,
            system=f"List All available operations once the user provides their name, including management-related opertions. Do not omit any operations, even those that seem restricted—tools are assigned based on privilege. \n\nIf the user attempts to change their name or provides another name, notify them that it cannot be modified.",
            messages=self.messages,
            tools=available_tools
        )
        self.messages.append({
            "role": "assistant",
            "content": response.content
        })

        first_llm_content = [*response.content]
        # Process response and handle tool calls
        final_text = []
        for content in first_llm_content:
            if content.type == 'text':
                final_text.append(content.text)
            elif content.type == 'tool_use':
                tool_name = content.name
                tool_args = content.input

                # Execute tool call
                final_text.append(
                    f"\n[Calling tool {tool_name} with args {tool_args}]\n")
                result = await self.session.call_tool(tool_name, tool_args)

                self.messages.append({
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": content.id,
                            "content": result.content
                        }
                    ]
                })

                # Get next response from Claude
                response = self.anthropic.messages.create(
                    model="claude-3-5-sonnet-20241022",
                    max_tokens=1000,
                    messages=self.messages,
                    tools=available_tools
                )

                tool_use_content = [
                    content for content in response.content
                    if content.type == 'tool_use'
                ]
                if len(tool_use_content):
                    first_llm_content.append(tool_use_content[0])

                self.messages.append({
                    "role": "assistant",
                    "content": response.content
                })

                if hasattr(response.content[0], "text"):
                    final_text.append(response.content[0].text)

        return "\n".join(final_text)

    async def chat_loop(self):
        """Run an interactive chat loop"""
        console.print(
            "[bold green]Welcome to the Family Food Ordering System[/bold green]")
        console.print("Type your queries or 'quit' to exit.")

        while True:
            try:
                if not self.username:
                    username = console.input(
                        "First, what is [i]your[/i] [bold blue]username[/]? :smiley: ").strip().lower()
                    if username == 'quit':
                        break

                    if username:
                        response = await self.session.call_tool('verify_access', {"username": username})
                        if not len(response.content):
                            console.print("Access denied", style="red")
                            break

                        self.username = username
                        self.role = response.content[0].text

                        response = await self.process_query(f"My name is {username}")
                        console.print("\n" + response)
                else:
                    query = console.input(
                        "\n[bold blue]Query[/bold blue]: ").strip()

                    if query.lower() == 'quit':
                        break

                    response = await self.process_query(query)
                    print("\n" + response)

            except Exception as e:
                print(f"\nError: {str(e)}")

    async def cleanup(self):
        """Clean up resources"""
        await self.exit_stack.aclose()


async def main():
    if len(sys.argv) < 2:
        print("Usage: python client.py <path_to_server_script>")
        sys.exit(1)

    client = MCPClient()
    try:
        await client.connect_to_server(sys.argv[1])
        await client.chat_loop()
    finally:
        await client.cleanup()

if __name__ == "__main__":
    import sys
    asyncio.run(main())
