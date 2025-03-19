from typing import List, Dict, Optional, Callable
import httpx
import json
import os
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from permit import Permit
from dotenv import load_dotenv
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
)

logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()

# Environment variables
LOCAL_PDP_URL = os.getenv("LOCAL_PDP_URL")
PERMIT_API_KEY = os.getenv("PERMIT_API_KEY")
PROJECT_ID = os.getenv("PROJECT_ID")
ENV_ID = os.getenv("ENV_ID")
OPERATION_ELEMENTS_CONFIG_ID = os.getenv('OPERATION_ELEMENTS_CONFIG_ID')
ACCESS_ELEMENTS_CONFIG_ID = os.getenv("ACCESS_ELEMENTS_CONFIG_ID")
TENANT = os.getenv("TENANT")


class PermitServer:
    def __init__(self, mcp: FastMCP, exclude_tools=None):
        self.mcp = mcp
        self.permit = Permit(
            pdp=LOCAL_PDP_URL,
            token=PERMIT_API_KEY,
        )
        self.exclude_tools = exclude_tools if exclude_tools else []
        self.register_tools()

    def _register_tool(self, tool_name: str, func: Callable) -> None:
        """
        Helper that conditionally wraps a tool with the @mcp.tool() decorator.
        """
        if tool_name not in self.exclude_tools:
            func = self.mcp.tool()(func)
        setattr(self, tool_name, func)

    def register_tools(self):
        async def create_access_request(user_id: str, resource: str, resource_instance: Optional[str], role: str, reason: str) -> str:
            """
            Create a new access request.

            Args:
                user_id: Either the unique id of the user, or the URL-friendly key of the user requesting access.
                resource: Resource id or key that the user is requesting access to.
                resource_instance: The id or key of the specific resource instance that the user is requesting access (optional).
                role: Role id or key that the user is requesting access to (e.g 'editor').
                reason: The reason for the access request.
            """

            url = f"https://api.permit.io/v2/facts/{PROJECT_ID}/{ENV_ID}/access_requests/{ACCESS_ELEMENTS_CONFIG_ID}/user/{user_id}/tenant/{TENANT}"
            access_request_details = {
                "tenant": TENANT, "resource": resource, "role": role}
            if resource_instance is not None:
                access_request_details["resource_instance"] = resource_instance

            payload = {
                "access_request_details": access_request_details, "reason": reason}

            headers = {"authorization": f"Bearer {PERMIT_API_KEY}",
                       "Content-Type": "application/json"}

            async with httpx.AsyncClient() as client:
                response = await client.post(url, json=payload, headers=headers)
                if 200 <= response.status_code < 300:
                    return "Your request has been successfully sent"
                else:
                    raise ToolError(
                        f"Request failed with status code {response.status_code}: {response.text}")

        self._register_tool("create_access_request", create_access_request)

        async def list_access_requests(
            user_id: str,
            resource: str,
            status: Optional[str] = None,
            role: Optional[str] = None,
            resource_instance: Optional[str] = None,
            page: Optional[int] = 1,
            per_page: Optional[int] = 30,
        ) -> List[Dict]:
            """
            List access requests.

            Args:
                user_id: The ID or URL-friendly key of the user requesting the list.
                status: Optional filter by status (e.g., "pending", "approved", "denied", "canceled").
                resource: Resource id or key that the user whats to list it's access requests.
                role: Optional filter by role.
                resource_instance: Optional filter by resource instance key or ID.
                page: Page number of the results to fetch (default: 1).
                per_page: The number of results per page (max 100, default: 30).
            """
            url = f"https://api.permit.io/v2/facts/{PROJECT_ID}/{ENV_ID}/access_requests/{ACCESS_ELEMENTS_CONFIG_ID}/user/{user_id}/tenant/{TENANT}"
            headers = {
                "authorization": f"Bearer {PERMIT_API_KEY}",
                "Content-Type": "application/json",
            }

            params = {k: v for k, v in {
                "status": status,
                "role": role,
                "resource": resource,
                "resource_instance_id": resource_instance,
                "page": page,
                "per_page": per_page,
            }.items() if v is not None}

            async with httpx.AsyncClient() as client:
                response = await client.get(url, headers=headers, params=params)
                if 200 <= response.status_code < 300:
                    return response.json().get("data", [])
                else:
                    raise ToolError(
                        f"Request failed with status code {response.status_code}: {response.text}")

        self._register_tool("list_access_requests", list_access_requests)

        async def approve_access_request(user_id: str, access_request_id: str, reviewer_comment: Optional[str] = None) -> str:
            """
            Approve an access request.

            Args:
                user_id: The ID or URL-friendly key of the user approving the request.
                access_request_id: The ID of the access request to approve.
                reviewer_comment: Optinoal comment from the reviewer.
            """
            url = f"https://api.permit.io/v2/facts/{PROJECT_ID}/{ENV_ID}/access_requests/{ACCESS_ELEMENTS_CONFIG_ID}/user/{user_id}/tenant/{TENANT}/{access_request_id}/approve"

            payload = {}

            if reviewer_comment:
                payload["reviewer_comment"] = reviewer_comment

            headers = {
                "authorization": f"Bearer {PERMIT_API_KEY}",
                "Content-Type": "application/json",
            }
            async with httpx.AsyncClient() as client:
                response = await client.put(url, json=payload, headers=headers)
                if 200 <= response.status_code < 300:
                    return "Access request approved successfully."
                else:
                    raise ToolError(
                        f"Request failed with status code {response.status_code}: {response.text}")

        self._register_tool("approve_access_request", approve_access_request)

        async def deny_access_request(user_id: str, access_request_id: str, reviewer_comment: Optional[str] = None) -> str:
            """
            Deny an access request.

            Args:
                user_id: The ID or URL-friendly key of the user denying the request.
                access_request_id: The ID or URL-friendly key of the access request to deny.
                reviewer_comment: Optional comment from the reviewer.
            """
            url = f"https://api.permit.io/v2/facts/{PROJECT_ID}/{ENV_ID}/access_requests/{ACCESS_ELEMENTS_CONFIG_ID}/user/{user_id}/tenant/{TENANT}/{access_request_id}/deny"

            payload = {}
            if reviewer_comment:
                payload["reviewer_comment"] = reviewer_comment

            headers = {
                "authorization": f"Bearer {PERMIT_API_KEY}",
                "Content-Type": "application/json",
            }
            async with httpx.AsyncClient() as client:
                response = await client.put(url, json=payload, headers=headers)
                if response.status_code >= 200 and response.status_code < 300:
                    return "Access request denied successfully."
                else:
                    raise ToolError(
                        f"Request failed with status code {response.status_code}: {response.text}")

        self._register_tool("deny_access_request", deny_access_request)

        # Operation Approval Tools
        async def create_operation_approval(user_id: str, resource: str, reason: str, resource_instance: Optional[str] = None) -> str:
            """
            Create a new operation approval request.

            Args:
                user_id: The ID or URL-friendly key of the user requesting the approval.
                resource: The resource for which the approval is requested.
                resource_instance: The specific instance of the resource (optional).
                reason: The reason for the approval request.
            """
            login = await self.permit.elements.login_as(user_id, TENANT)
            url = f"https://api.permit.io/v2/elements/{PROJECT_ID}/{ENV_ID}/config/{OPERATION_ELEMENTS_CONFIG_ID}/operation_approval"

            access_request_details = {
                "tenant": TENANT,
                "resource": resource,
            }
            if resource_instance is not None:
                access_request_details["resource_instance"] = resource_instance

            payload = {
                "access_request_details": access_request_details,
                "reason": reason
            }
            headers = {
                "authorization": f"Bearer {login.element_bearer_token}",
                "Content-Type": "application/json",
            }

            async with httpx.AsyncClient() as client:
                response = await client.post(url, json=payload, headers=headers)
                if response.status_code >= 200 and response.status_code < 300:
                    return "Operation approval request created successfully."
                else:
                    raise ToolError(
                        f"Request failed with status code {response.status_code}: {response.text}")

        self._register_tool("create_operation_approval",
                            create_operation_approval)

        async def list_operation_approvals(
            user_id: str,
            resource: str,
            status: Optional[str] = None,
            resource_instance: Optional[str] = None,
            page: Optional[int] = 1,
            per_page: Optional[int] = 30,
        ) -> List[Dict]:
            """
            List operation approvals,

            Args:
                user_id: The ID or URL-friendly key of the user requesting the list.
                status: Optional filter by status (e.g., "pending", "approved", "denied", "canceled").
                resource: Resource id or key that the user whats to list it's operation approval requests.
                resource_instance: Optional filter by resource instance key or ID.
                page: Page number of the results to fetch (default: 1).
                per_page: The number of results per page (max 100, default: 30).
            """
            login = await self.permit.elements.login_as(user_id, TENANT)
            url = f"https://api.permit.io/v2/elements/{PROJECT_ID}/{ENV_ID}/config/{OPERATION_ELEMENTS_CONFIG_ID}/operation_approval"

            headers = {
                "authorization": f"Bearer {login.element_bearer_token}",
                "Content-Type": "application/json",
            }
            params = {
                "element_id": OPERATION_ELEMENTS_CONFIG_ID,
                "resource": resource
            }
            if status:
                params["status"] = status
            if resource_instance:
                params["resource_instance"] = resource_instance
            if page:
                params["page"] = page
            if per_page:
                params["per_page"] = per_page

            async with httpx.AsyncClient() as client:
                response = await client.get(url, headers=headers, params=params)
                if response.status_code >= 200 and response.status_code < 300:
                    string_data = response.content.decode('utf-8')
                    data = json.loads(string_data)
                    return data.get("data", [])
                else:
                    raise ToolError(
                        f"Request failed with status code {response.status_code}: {response.text}")

        self._register_tool("list_operation_approvals",
                            list_operation_approvals)

        async def approve_operation_approval(user_id: str, operation_approval_id: str, reviewer_comment: Optional[str] = None) -> str:
            """
            Approve an operation approval request.

            Args:
                user_id: The ID or URL-friendly key of the user approving the request.
                operation_approval_id: The ID or URL-friendly key the operation approval.
                reviewer_comment: Optional comment from the reviewer.
            """
            login = await self.permit.elements.login_as(user_id, TENANT)
            url = f"https://api.permit.io/v2/elements/{PROJECT_ID}/{ENV_ID}/config/{OPERATION_ELEMENTS_CONFIG_ID}/operation_approval/{operation_approval_id}/approve"

            payload = {}
            if reviewer_comment:
                payload["reviewer_comment"] = reviewer_comment

            headers = {
                "authorization": f"Bearer {login.element_bearer_token}",
                "Content-Type": "application/json",
            }
            async with httpx.AsyncClient() as client:
                response = await client.put(url, json=payload, headers=headers)
                if response.status_code >= 200 and response.status_code < 300:
                    return "Operation approval request approved successfully."
                else:
                    raise ToolError(
                        f"Request failed with status code {response.status_code}: {response.text}")

        self._register_tool("approve_operation_approval",
                            approve_operation_approval)

        async def deny_operation_approval(user_id: str, operation_approval_id: str, reviewer_comment: Optional[str] = None) -> str:
            """
            Deny an operation approval request.

            Args:
                user_id: The ID or URL-friendly key of the user denying the request.
                operation_approval_id: The ID or URL-friendly key of the operation approval.
                reviewer_comment: Optional comment from the reviewer.
            """
            login = await self.permit.elements.login_as(user_id, TENANT)
            url = f"https://api.permit.io/v2/elements/{PROJECT_ID}/{ENV_ID}/config/{OPERATION_ELEMENTS_CONFIG_ID}/operation_approval/{operation_approval_id}/deny"

            payload = {}
            if reviewer_comment:
                payload["reviewer_comment"] = reviewer_comment

            headers = {
                "authorization": f"Bearer {login.element_bearer_token}",
                "Content-Type": "application/json",
            }

            async with httpx.AsyncClient() as client:
                response = await client.put(url, json=payload, headers=headers)
                if response.status_code >= 200 and response.status_code < 300:
                    return "Operation approval request denied successfully."
                else:
                    raise ToolError(
                        f"Request failed with status code {response.status_code}: {response.text}")

        self._register_tool("deny_operation_approval", deny_operation_approval)


def main():
    """Main entry point"""
    mcp = FastMCP("permit_mcp_server")
    server = PermitServer(mcp)

    logger.info("Starting Permit MCP server...")
    server.mcp.run(transport='stdio')


if __name__ == "__main__":
    main()
