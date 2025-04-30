# Permit.io MCP Server
The Permit MCP server is used to create and manage access to a resource from an AI application. It can enable authorized individuals and end users to request and manage access to a resource using natural language.

## Features 
- Create an access request.
- List access requests.
- Approve and deny access requests.
- Create an operation approval request.
- List operation approval requests.
- Approve and deny operation approval requests.
- List resource intances.

## Ways You Can Use the Server?
There are two ways the Permit MCP server can be used. 
1. **Locally**: 
It can be run locally by individuals with the required credentials (environment variables), allowing them to view, approve, or deny access requests from end-users within an AI assistant like Claude Desktop.
2. **Hosted / Production Deployment**:
The server can be used to provide tools that allow end-users to send access requests from within an AI application. In this setup, the MCP server and the LLM run in a secure environment where user queries are processed. You can find an implementation of this in the [Family Food Ordering System](https://github.com/Tammibriggs/permit-mcp/tree/main/examples/food-ordering-system) example.

## Prerequisite
- Python >= 3.10
- `uv` >= 0.6.1

## Installation
```shell
git clone <repository_url>
cd permit-mcp

# Create virtual environment, activate it and install dependencies
uv venv
source .venv/bin/activate # For Windows: .venv\Scripts\activate
uv pip install -e . 
```

## Environment Variables
To setup the server you need supply the environment variables in defined in the `.env.example` file. Create a `.env` file in the root directory and specify the following variables: 

```shell
TENANT=  # default
RESOURCE_KEY= # The key of the resource you want to manage access for.
PERMIT_PDP_URL=  # defaults to the cloud PDP https://cloudpdp.api.permit.io
PERMIT_API_KEY=
PROJECT_ID=
ENV_ID=
ACCESS_ELEMENTS_CONFIG_ID=
OPERATION_ELEMENTS_CONFIG_ID=
```

You can use the following ressources to help with that: 
- [PERMIT_PDP_URL](https://docs.permit.io/how-to/deploy/deploy-to-production/#installing-the-pdp)
- [PERMIT_API_KEY](https://docs.permit.io/overview/use-the-permit-api-and-sdk#obtain-your-api-key)
- [PROJECT_ID](https://docs.permit.io/api/examples/get-project-and-env#get-project-id-or-key)
- [ENV_ID](https://docs.permit.io/api/examples/get-project-and-env#get-environment-id-or-key)
- ACCESS_ELEMENTS_CONFIG_ID: The ID of the [user management element](https://docs.permit.io/embeddable-uis/element/user-management).
- OPERATION_ELEMENTS_CONFIG_ID: The ID of the [approval management element](https://docs.permit.io/embeddable-uis/element/approval-management).

## Run the Permit MCP Server With Claude Desktop
First, install [Claude Desktop](https://claude.ai/download), then configure Claude to use the server with the following configurations: 
 
```json
{
    "mcpServers": {
        "permit": {
            "command": "uv",
            "args": [
                "--directory",
                "/ABSOLUTE/PATH/TO/PARENT/FOLDER/src/permit_mcp",
                "run",
                "server.py"
            ]
        }
    }
}
```

## Building Custom Server with Permit MCP Server
The Permit MCP server provides an easy way to import and exclude its tools within your custom MCP server by using its class. 

```python
from permit_mcp import PermitServer

# Initialize FastMCP instance
mcp = FastMCP("sever_name")
permit_server = PermitServer(
    mcp, exclude_tools=['create_access_request', 'create_operation_approval'])
```
With this, all other tools aside from `create_access_request` and `create_operation_approval` will be available.

You can find a complete implementation in the [Family Food Ordering System](https://github.com/Tammibriggs/permit-mcp/tree/main/examples/food-ordering-system). 


## Best Practices
- Make sure to specify users' names when syncing or creating users in Permit. This will make it easier to identify which user submitted an access or approval request when reviewing the list of requests. 
```python
await permit.api.sync_user({
    "key": user_id,
    "first_name": firstname
})
```
- For ReBAC authorization models, if the resource instance key is not the same as its name, be sure to include the instance's name as an attribute—along with other necessary information—when creating it. This will make it easier for both you and the LLM to identify that instance.
```python
await permit.api.resource_instances.create({
    "resource": "restaurants",
    "key": restaurant_id,
    "tenant": TENANT,
    "attributes": {
        "name": restaurant_name,
        "allowed_for_children": bool(allowed_for_children)
    }
})
```