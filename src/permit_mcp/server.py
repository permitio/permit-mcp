from typing import List, Dict, Optional, Callable, Union
import httpx
import json
import os
import ssl

import aiohttp.connector
import certifi
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


# CVE-2026-111943 (PER-17672): the permit SDK opens aiohttp sessions with no SSL context, so
# aiohttp verifies with its module default, built by ssl.create_default_context(). That loads
# the platform's trust store (OpenSSL's default file and directory, and on Windows the system
# stores too), which can still trust the compromised eMudhra intermediate CAs. Replace it with a
# context built the way httpx builds its own: SSL_CERT_FILE, else SSL_CERT_DIR, else certifi.
# aiohttp reads the default on every request, so it doesn't matter what imported aiohttp first.
def _verified_context() -> ssl.SSLContext:
    if os.environ.get("SSL_CERT_FILE"):
        context = ssl.create_default_context(cafile=os.environ["SSL_CERT_FILE"])
    elif os.environ.get("SSL_CERT_DIR"):
        context = ssl.create_default_context(capath=os.environ["SSL_CERT_DIR"])
    else:
        context = ssl.create_default_context(cafile=certifi.where())
    context.set_alpn_protocols(("http/1.1",))  # as aiohttp sets on its own default
    return context


def _pin_aiohttp_trust_store() -> None:
    # A private name: fail loudly if an aiohttp upgrade drops it, rather than silently verify
    # against the platform's store again.
    if not hasattr(aiohttp.connector, "_SSL_CONTEXT_VERIFIED"):
        raise RuntimeError(
            "aiohttp.connector._SSL_CONTEXT_VERIFIED is gone, so the permit SDK's calls would "
            "verify against the platform trust store (CVE-2026-111943, PER-17672)"
        )
    aiohttp.connector._SSL_CONTEXT_VERIFIED = _verified_context()


# After load_dotenv(), so an SSL_CERT_FILE or SSL_CERT_DIR from .env applies here as it does to
# httpx, which reads them when each client is built.
_pin_aiohttp_trust_store()

# Environment variables
PERMIT_PDP_URL = os.getenv("PERMIT_PDP_URL", 'https://cloudpdp.api.permit.io')
TENANT = os.getenv("TENANT", 'default')

RESOURCE_KEY = os.getenv('RESOURCE_KEY')
PERMIT_API_KEY = os.getenv("PERMIT_API_KEY")
PROJECT_ID = os.getenv("PROJECT_ID")
ENV_ID = os.getenv("ENV_ID")
OPERATION_ELEMENTS_CONFIG_ID = os.getenv('OPERATION_ELEMENTS_CONFIG_ID')
ACCESS_ELEMENTS_CONFIG_ID = os.getenv("ACCESS_ELEMENTS_CONFIG_ID")

API_URL = "https://api.permit.io/v2"


async def _api_get(path: str, params: Optional[Dict] = None):
    """GET a Permit API path, dropping None params; raise ToolError on non-2xx."""
    headers = {"authorization": f"Bearer {PERMIT_API_KEY}"}
    params = {k: v for k, v in (params or {}).items() if v is not None}
    async with httpx.AsyncClient() as client:
        response = await client.get(f"{API_URL}{path}", headers=headers, params=params)
    if 200 <= response.status_code < 300:
        return response.json()
    raise ToolError(
        f"Request failed with status code {response.status_code}: {response.text}")


class PermitServer:
    def __init__(self, mcp: FastMCP, exclude_tools=None):
        self.mcp = mcp
        self.permit = Permit(
            pdp=PERMIT_PDP_URL,
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

        async def list_resource_instances(
            page: int = 1,
            per_page: int = 100,
            env: Optional[str] = None,
            tenant: Optional[str] = None,
            resource: Optional[str] = None,
        ):
            """
                Lists resource instances along with their ID and key which can be used as a parameter for tools that required it.
                It can be used to verify the existeance of a resource instance.

                Args:
                    page: Optional page number of the results to fetch, starting at page 1.
                    per_page: Optional number of results per page (maximum of 100).
                    env: Optional environment key or ID (see list_environments). Defaults to the configured environment.
                    tenant: Optional tenant key (see list_tenants). Defaults to the configured tenant.
                    resource: Optional resource type key (see list_resources). Defaults to the configured resource.
            """

            url = f"https://api.permit.io/v2/facts/{PROJECT_ID}/{env or ENV_ID}/resource_instances"

            params = {k: v for k, v in {
                "tenant": tenant or TENANT,
                "resource": resource or RESOURCE_KEY,
                "page": page,
                "per_page": per_page,
            }.items() if v is not None}

            headers = {"authorization": f"Bearer {PERMIT_API_KEY}",
                       "Content-Type": "application/json"}

            async with httpx.AsyncClient() as client:
                response = await client.get(url, headers=headers, params=params)
                if 200 <= response.status_code < 300:
                    resources_instances = response.json()
                    return resources_instances

                else:
                    raise ToolError(
                        f"Request failed with status code {response.status_code}: {response.text}")

        self._register_tool("list_resource_instances",
                            list_resource_instances)

        # Read-only schema tools. With a project- or organization-level API key, `env`
        # selects any environment of the configured project; with an environment-level
        # key only that environment answers.
        async def list_environments() -> List[Dict]:
            """
            Lists the environments of the configured project. Pass an environment's key
            or ID as the `env` argument of other tools.
            """
            envs = await _api_get(f"/projects/{PROJECT_ID}/envs")
            return [{"key": e["key"], "id": e["id"], "name": e["name"]} for e in envs]

        self._register_tool("list_environments", list_environments)

        async def list_tenants(env: Optional[str] = None, page: int = 1, per_page: int = 100) -> List[Dict]:
            """
            Lists the tenants of an environment.

            Args:
                env: Optional environment key or ID. Defaults to the configured environment.
                page: Page number of the results to fetch, starting at page 1.
                per_page: Number of results per page (maximum of 100).
            """
            tenants = await _api_get(f"/facts/{PROJECT_ID}/{env or ENV_ID}/tenants",
                                     {"page": page, "per_page": per_page})
            return [{"key": t["key"], "id": t["id"], "name": t["name"]} for t in tenants]

        self._register_tool("list_tenants", list_tenants)

        async def list_resources(
            env: Optional[str] = None,
            key_contains: Optional[str] = None,
            page: int = 1,
            per_page: int = 100,
        ) -> List[Dict]:
            """
            Lists resource types with their actions and resource-level (ReBAC) roles.
            Use it to check whether a resource type exists and which actions it defines.

            Args:
                env: Optional environment key or ID. Defaults to the configured environment.
                key_contains: Optional substring the resource key must contain (applied to the fetched page).
                page: Page number of the results to fetch, starting at page 1.
                per_page: Number of results per page (maximum of 100).
            """
            resources = await _api_get(f"/schema/{PROJECT_ID}/{env or ENV_ID}/resources",
                                       {"page": page, "per_page": per_page})
            return [
                {
                    "key": r["key"],
                    "name": r["name"],
                    "actions": sorted(r.get("actions") or {}),
                    "roles": sorted(r.get("roles") or {}),
                }
                for r in resources
                if key_contains is None or key_contains in r["key"]
            ]

        self._register_tool("list_resources", list_resources)

        async def list_roles(env: Optional[str] = None, resource: Optional[str] = None) -> List[Dict]:
            """
            Lists roles and their permissions ("resource:action").
            With `resource`, returns only the top-level roles granting something on that
            resource type, plus that resource's own ReBAC roles (scope = the resource key).

            Args:
                env: Optional environment key or ID. Defaults to the configured environment.
                resource: Optional resource type key to filter permissions by.
            """
            env = env or ENV_ID
            roles = await _api_get(f"/schema/{PROJECT_ID}/{env}/roles", {"per_page": 100})
            result = []
            for role in roles:
                permissions = role.get("permissions") or []
                if resource:
                    permissions = [p for p in permissions if p.split(":", 1)[0] == resource]
                    if not permissions:
                        continue
                result.append({"key": role["key"], "name": role["name"],
                               "scope": "top-level", "permissions": permissions})
            if resource:
                resource_roles = await _api_get(
                    f"/schema/{PROJECT_ID}/{env}/resources/{resource}/roles", {"per_page": 100})
                result += [{"key": r["key"], "name": r["name"], "scope": resource,
                            "permissions": r.get("permissions") or []} for r in resource_roles]
            return result

        self._register_tool("list_roles", list_roles)

        async def create_access_request(user_id: str,  role: str, reason: str, resource_instance: Optional[Union[str, int]] = None) -> str:
            """
            Create a new access request.

            Args:
                user_id: The ID or URL-friendly key of the user requesting access.
                resource_instance: The id or key of the specific resource that the user is requesting access. This parameter is required for ReBAC authorization.
                role: Role id or key that the user is requesting access to.
                reason: The reason for the access request.
            """

            url = f"https://api.permit.io/v2/facts/{PROJECT_ID}/{ENV_ID}/access_requests/{ACCESS_ELEMENTS_CONFIG_ID}/user/{user_id}/tenant/{TENANT}"
            access_request_details = {
                "tenant": TENANT, "resource": RESOURCE_KEY, "role": role}
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
            status: Optional[str] = None,
            role: Optional[str] = None,
            resource_instance: Optional[Union[str, int]] = None,
            page: Optional[int] = 1,
            per_page: Optional[int] = 30,
        ) -> List[Dict]:
            """
            List access requests.

            Args:
                user_id: The ID or URL-friendly key of the user requesting to view the list.
                status: Optional filter by status (e.g., "pending", "approved", "denied", "canceled").
                role: Optional filter by role.
                resource_instance: Filter by resource instance key or ID. This parameter is required for ReBAC authorization.
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
                "resource": RESOURCE_KEY,
                "resource_instance_id": resource_instance,
                "page": page,
                "per_page": per_page,
            }.items() if v is not None}

            async with httpx.AsyncClient() as client:
                response = await client.get(url, headers=headers, params=params)
                if 200 <= response.status_code < 300:
                    access_requests = response.json().get("data", [])
                    for item in access_requests:
                        requesting_user_id = item.get("requesting_user_id")
                        if requesting_user_id:
                            user = await self.permit.api.users.get_by_id(requesting_user_id)
                            item["requesting_user"] = user

                    return access_requests
                else:
                    raise ToolError(
                        f"Request failed with status code {response.status_code}: {response.text}")

        self._register_tool("list_access_requests", list_access_requests)

        async def approve_access_request(user_id: str, access_request_id: str, reviewer_comment: Optional[str] = None) -> str:
            """
            Approve an access request.

            Args:
                user_id: The ID or URL-friendly key of the user approving the request.
                access_request_id: The ID of the access request to approve, which can be obtained by first listing access requests.
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
                access_request_id: The ID or URL-friendly key of the access request to deny, which can be obtained by first listing access requests.
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
        async def create_operation_approval(user_id: str, reason: str, resource_instance: Optional[Union[str, int]] = None) -> str:
            """
            Create a new operation approval request.

            Args:
                user_id: The ID or URL-friendly key of the user requesting the approval.
                resource_instance: The specific instance of the resource. This parameter is required for ReBAC authorization.
                reason: The reason for the approval request.
            """
            login = await self.permit.elements.login_as(user_id, TENANT)
            url = f"https://api.permit.io/v2/elements/{PROJECT_ID}/{ENV_ID}/config/{OPERATION_ELEMENTS_CONFIG_ID}/operation_approval"

            access_request_details = {
                "tenant": TENANT,
                "resource": RESOURCE_KEY,
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
            status: Optional[str] = None,
            resource_instance: Optional[Union[str, int]] = None,
            page: Optional[int] = 1,
            per_page: Optional[int] = 30,
        ) -> List[Dict]:
            """
            List one-time operation approval requests.

            Args:
                user_id: The ID or URL-friendly key of the user requesting the list.
                status: Optional filter by status (e.g., "pending", "approved", "denied", "canceled").
                resource_instance: Filter by resource instance key or ID. This parameter is required for ReBAC authorization.
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
                "resource": RESOURCE_KEY
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
                    operation_approvals = data.get("data", [])
                    logger.info(operation_approvals)
                    for item in operation_approvals:
                        requesting_user_id = item.get("requesting_user_id")
                        if requesting_user_id:
                            user = await self.permit.api.users.get_by_id(requesting_user_id)
                            item["requesting_user"] = user
                    return operation_approvals
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
                operation_approval_id: The ID or URL-friendly key of the operation approval, which can be obtained by first listing operation approvals.
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
                operation_approval_id: The ID or URL-friendly key of the operation approval to deny, which can be obtained by first listing operation approvals.
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
