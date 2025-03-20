# Permit.io MCP Server
A Model Context Protocol (MCP) server for Permit.io  access request and operation approval creation and management.

## Features 
- Create an access and operation approval request.
- List access and operation approval requests.
- Approve access and operation approval request.
- Deny access and operation approval request.
- List resources and resource intances.
- Validate the existence of a resource.

## What Can You Do with It?
When connected to an AI chatbot like Claude or a custom MCP client, you can fire off queries like these to create and manage access for a specific resource in a tenant.

- `My user id is dev, create an operation approval request with reason 'scheduled maintenance update'`
- **Approving Requests (Two-Step Process):**
  1. `My user id is admin, list pending access requests`
  2. `Approve access request 'req_123' with comment 'credentials verified'`
- **Denying Requests (Two-Step Process):**
  1. `My user id is security, list all access requests`
  2. `Deny access request 'req_456' with comment 'needs additional clearance'`


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
RESOURCE_KEY=
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