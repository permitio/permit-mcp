# Permit.io MCP Server
A Model Context Protocol (MCP) server for Permit access request operation approval management.

## Features 
- Create an access request
- List access requests
- Approve access request
- Deny access request
- Create operation approval request
- List operation approval requests
- Approve operation approval request 
- Deny operation approval request

## Installation
```shell
git clone <repository_url>
cd permit-mcp

# Create virtual environment, activate it and install dependencies
uv venv
source .venv/bin/activate # For Windows: .venv\Scripts\activate
uv pip install -e .
```

## Prerequisite
- Python >= 3.10
- `uv` >= 0.6.1
- Running instance of [Permit PDP](https://docs.permit.io/how-to/deploy/deploy-to-production/#installing-the-pdp)


## Environment Variables
To setup the server you need supply the environment variables in defined in the `.env.example` file. Create a `.env` file in the root directory and specify the following variables: 

```shell
TENANT= # e.g default
LOCAL_PDP_URL= 
PERMIT_API_KEY=
PROJECT_ID=
ENV_ID=
ACCESS_ELEMENTS_CONFIG_ID=
OPERATION_ELEMENTS_CONFIG_ID=
```

You can use the following ressources to help with that: 
- [LOCAL_PDP_URL](https://docs.permit.io/how-to/deploy/deploy-to-production/#installing-the-pdp)
- [PERMIT_API_KEY](https://docs.permit.io/overview/use-the-permit-api-and-sdk#obtain-your-api-key)
- [PROJECT_ID](https://docs.permit.io/api/examples/get-project-and-env#get-project-id-or-key)
- [ENV_ID](https://docs.permit.io/api/examples/get-project-and-env#get-environment-id-or-key)
- ACCESS_ELEMENTS_CONFIG_ID: The ID of the [user management element](https://docs.permit.io/embeddable-uis/element/user-management).
- OPERATION_ELEMENTS_CONFIG_ID: The ID of the [approval management element](https://docs.permit.io/embeddable-uis/element/approval-management).

## Runing the MCP Client
Enter the following command in your terminal:

```shell
uv run -m src.permit_mcp
```