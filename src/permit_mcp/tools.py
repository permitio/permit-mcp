"""The MCP tools for Permit access requests, operation approvals and permission checks.

No tool takes a user argument. Every call asks the server's identity resolver for the
caller's Permit user, and fails before any Permit request when there is none. The
access-request and operation-approval tools act as that user, check_permission asks the PDP
about that user, and list_resource_instances lists with the server's credentials.
"""

import json
from collections.abc import Awaitable, Callable, Collection
from typing import Annotated, Any, Literal, TypedDict, TypeVar, cast
from uuid import UUID

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import ConfigDict, Field, with_config

from permit_mcp._log import get_logger
from permit_mcp.config import Settings
from permit_mcp.identity import IdentityError, IdentityResolver
from permit_mcp.permit_api import PermitApi, PermitApiError

logger = get_logger(__name__)

_T = TypeVar("_T")

LIST_RESOURCE_INSTANCES = "list_resource_instances"
CHECK_PERMISSION = "check_permission"
ACCESS_REQUEST_TOOLS = (
    "create_access_request",
    "list_access_requests",
    "approve_access_request",
    "deny_access_request",
    "cancel_access_request",
)
OPERATION_APPROVAL_TOOLS = (
    "create_operation_approval",
    "list_operation_approvals",
    "approve_operation_approval",
    "deny_operation_approval",
    "cancel_operation_approval",
)
TOOL_NAMES: tuple[str, ...] = (
    LIST_RESOURCE_INSTANCES,
    CHECK_PERMISSION,
    *ACCESS_REQUEST_TOOLS,
    *OPERATION_APPROVAL_TOOLS,
)
"""The names of all tools, in the order `PermitTools.register` registers them."""

RequestStatus = Literal["pending", "approved", "denied", "canceled"]

Page = Annotated[int, Field(ge=1, description="Page number of the results, starting at 1.")]
PerPage = Annotated[int, Field(ge=1, le=100, description="Results per page, from 1 to 100.")]
ResourceInstance = Annotated[
    str | None,
    Field(
        min_length=1,
        description=(
            "Key or ID of one instance of the resource type. Optional. If Permit answers that "
            "an instance is required, get one from list_resource_instances."
        ),
    ),
]
ResourceInstanceFilter = Annotated[
    str | None,
    Field(min_length=1, description="Only requests on this resource instance (key or ID)."),
]
StatusFilter = Annotated[RequestStatus | None, Field(description="Only requests in this status.")]
Reason = Annotated[str, Field(min_length=1, description="Why it is needed; reviewers see it.")]
ReviewerComment = Annotated[str | None, Field(description="Stored with the decision.")]
AccessRequestId = Annotated[
    UUID, Field(description="ID (a UUID) of the access request, from list_access_requests.")
]
OperationApprovalId = Annotated[
    UUID,
    Field(description="ID (a UUID) of the operation approval, from list_operation_approvals."),
]

_READ = ToolAnnotations(read_only_hint=True, open_world_hint=False)
_WRITE = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
    open_world_hint=False,
)


class _JsonResult(TypedDict):
    result: Any


AccessRequest = Annotated[Any, Field(description="The access request, as Permit returns it.")]
OperationApproval = Annotated[
    Any, Field(description="The operation approval request, as Permit returns it.")
]


class PermissionResult(TypedDict):
    """What check_permission returns."""

    allowed: Annotated[bool, Field(description="Whether the acting user may do it.")]


class AccessRequestCreated(TypedDict):
    """What create_access_request returns."""

    status: Literal["created"]
    access_request: AccessRequest


class AccessRequestApproved(TypedDict):
    """What approve_access_request returns."""

    status: Literal["approved"]
    access_request: AccessRequest


class AccessRequestDenied(TypedDict):
    """What deny_access_request returns."""

    status: Literal["denied"]
    access_request: AccessRequest


class AccessRequestCanceled(TypedDict):
    """What cancel_access_request returns."""

    status: Literal["canceled"]
    access_request: AccessRequest


class OperationApprovalCreated(TypedDict):
    """What create_operation_approval returns."""

    status: Literal["created"]
    operation_approval: OperationApproval


class OperationApprovalApproved(TypedDict):
    """What approve_operation_approval returns."""

    status: Literal["approved"]
    operation_approval: OperationApproval


class OperationApprovalDenied(TypedDict):
    """What deny_operation_approval returns."""

    status: Literal["denied"]
    operation_approval: OperationApproval


class OperationApprovalCanceled(TypedDict):
    """What cancel_operation_approval returns."""

    status: Literal["canceled"]
    operation_approval: OperationApproval


# Permit's page object as it is; its other keys, such as total_count and page_count, pass
# through.
@with_config(ConfigDict(extra="allow"))
class RequestPage(TypedDict):
    """What list_access_requests and list_operation_approvals return."""

    data: Annotated[
        list[Any],
        Field(
            description=(
                "The requests as Permit returns them. Each holds the requesting user's email "
                "and name in requesting_user_email, requesting_user_first_name and "
                "requesting_user_last_name."
            )
        ),
    ]


class PermitTools:
    """The Permit tools, ready to register on an `MCPServer`.

    Every tool asks `identity` who the caller is; the access-request and operation-approval
    tools act in Permit as that user, and check_permission asks the PDP about that user. All
    calls, to the API and to the PDP, share one HTTP session, which `aclose()` closes. One
    instance belongs to one event loop.
    """

    def __init__(self, settings: Settings, identity: IdentityResolver) -> None:
        """Prepare the tools; nothing is sent to Permit until a tool is called.

        Args:
            settings: The validated server settings.
            identity: Returns the Permit user key of a tool call's caller.

        """
        self._settings = settings
        self._identity = identity
        self._api = PermitApi(settings)

    def register(self, server: MCPServer[Any], *, exclude: Collection[str] = ()) -> list[str]:
        """Register the tools on `server`, leaving out those in `exclude`.

        The access-request tools are registered only when `access_request_element` is set,
        and the operation-approval tools only when `operation_approval_element` is set.
        list_resource_instances and check_permission need no element and are always
        registered.

        Args:
            server: The server to register on, such as one a host application built.
            exclude: Names of tools to leave out.

        Returns:
            The names of the registered tools, in `TOOL_NAMES` order.

        Raises:
            ValueError: `exclude` names a tool that does not exist.

        """
        unknown = sorted(set(exclude) - set(TOOL_NAMES))
        if unknown:
            msg = (
                f"Unknown tool name(s) to exclude: {', '.join(unknown)}. "
                f"Valid names: {', '.join(TOOL_NAMES)}."
            )
            raise ValueError(msg)
        available = {LIST_RESOURCE_INSTANCES, CHECK_PERMISSION}
        if self._settings.access_request_element is not None:
            available.update(ACCESS_REQUEST_TOOLS)
        if self._settings.operation_approval_element is not None:
            available.update(OPERATION_APPROVAL_TOOLS)
        registered: list[str] = []
        for name, (fn, description, annotations) in self._tools().items():
            if name in available and name not in exclude:
                server.add_tool(fn, name=name, description=description, annotations=annotations)
                registered.append(name)
        return registered

    async def aclose(self) -> None:
        """Close the HTTP session to Permit now; a later call opens a new one.

        Hosts call it when their server shuts down.
        """
        await self._api.aclose()

    def _tools(self) -> dict[str, tuple[Callable[..., Any], str, ToolAnnotations]]:
        target = (
            f"the '{self._settings.resource}' resource type in tenant '{self._settings.tenant}'"
        )
        acting = "Acts as the caller's Permit user."
        with_requester = "each with the requesting user's email and name"
        return {
            LIST_RESOURCE_INSTANCES: (
                self._list_resource_instances,
                (
                    f"List the instances of {target}, with the ID and key of each. Use an "
                    "instance key as resource_instance in the other tools. The listing uses "
                    "the server's credentials and is not filtered by the caller's "
                    "permissions; the call still requires an identified caller."
                ),
                _READ,
            ),
            CHECK_PERMISSION: (
                self._check_permission,
                (
                    "Ask Permit whether the acting user may perform an action on "
                    f"{target}, or on one instance of it (the instance key, not the ID). "
                    "Returns allowed: true or false. On the cloud PDP, RBAC and ReBAC policies "
                    "are evaluated; a permission that only an ABAC policy grants comes back "
                    "false. Asks as the caller's Permit user."
                ),
                _READ,
            ),
            "create_access_request": (
                self._create_access_request,
                (
                    f"Request a role on {target}, or on one instance of it. The request is "
                    "filed for the acting user and waits for a reviewer to approve or deny it. "
                    f"{acting}"
                ),
                _WRITE,
            ),
            "list_access_requests": (
                self._list_access_requests,
                (
                    "List the access requests of this server's access request element that "
                    f"Permit shows the acting user, {with_requester}. {acting}"
                ),
                _READ,
            ),
            "approve_access_request": (
                self._approve_access_request,
                (
                    "Approve an access request, which grants the requested role. Permit "
                    f"refuses when the acting user may not review access requests. {acting}"
                ),
                _WRITE,
            ),
            "deny_access_request": (
                self._deny_access_request,
                (
                    "Deny an access request. Permit refuses when the acting user may not "
                    f"review access requests. {acting}"
                ),
                _WRITE,
            ),
            "cancel_access_request": (
                self._cancel_access_request,
                (
                    "Cancel an access request. Withdraws a pending request the acting user "
                    f"filed; Permit refuses otherwise. {acting}"
                ),
                _WRITE,
            ),
            "create_operation_approval": (
                self._create_operation_approval,
                (
                    f"Request one-time approval for an operation on {target}, or on one "
                    "instance of it. The request is filed for the acting user and waits for a "
                    f"reviewer to approve or deny it. {acting}"
                ),
                _WRITE,
            ),
            "list_operation_approvals": (
                self._list_operation_approvals,
                (
                    f"List the operation approval requests on {target} that Permit shows the "
                    f"acting user, {with_requester}. {acting}"
                ),
                _READ,
            ),
            "approve_operation_approval": (
                self._approve_operation_approval,
                (
                    "Approve an operation approval request. Permit refuses when the acting "
                    f"user may not review operation approvals. {acting}"
                ),
                _WRITE,
            ),
            "deny_operation_approval": (
                self._deny_operation_approval,
                (
                    "Deny an operation approval request. Permit refuses when the acting user "
                    f"may not review operation approvals. {acting}"
                ),
                _WRITE,
            ),
            "cancel_operation_approval": (
                self._cancel_operation_approval,
                (
                    "Cancel an operation approval request. Withdraws a pending request the "
                    f"acting user filed; Permit refuses otherwise. {acting}"
                ),
                _WRITE,
            ),
        }

    async def _list_resource_instances(
        self, ctx: Context[Any, Any], *, page: Page = 1, per_page: PerPage = 30
    ) -> Annotated[CallToolResult, _JsonResult]:
        await self._acting_user(ctx)
        instances = await _translate(
            self._api.list_resource_instances(page=page, per_page=per_page)
        )
        # The API answers with a list or an envelope. Returned as one JSON text block, as
        # MCPServer would split a returned list into one block per item, and none when empty.
        return CallToolResult(
            content=[TextContent(type="text", text=json.dumps(instances, indent=2))],
            structured_content={"result": instances},
        )

    async def _check_permission(
        self,
        ctx: Context[Any, Any],
        *,
        action: Annotated[
            str, Field(min_length=1, description="Key of the action, such as read or edit.")
        ],
        resource_instance: Annotated[
            str | None,
            Field(
                min_length=1,
                description=(
                    "Instance key (not the ID). Optional; without it the check is on the "
                    "resource type."
                ),
            ),
        ] = None,
    ) -> PermissionResult:
        user = await self._acting_user(ctx)
        allowed = await _translate(
            self._api.check_permission(user, action=action, resource_instance=resource_instance)
        )
        return {"allowed": allowed}

    async def _create_access_request(
        self,
        ctx: Context[Any, Any],
        *,
        role: Annotated[str, Field(min_length=1, description="Key of the role to request.")],
        reason: Reason,
        resource_instance: ResourceInstance = None,
    ) -> AccessRequestCreated:
        user = await self._acting_user(ctx)
        created = await _translate(
            self._api.create_access_request(
                user, role=role, reason=reason, resource_instance=resource_instance
            )
        )
        return {"status": "created", "access_request": created}

    async def _list_access_requests(  # noqa: PLR0913 - keyword-only, the tool's arguments
        self,
        ctx: Context[Any, Any],
        *,
        status: StatusFilter = None,
        role: Annotated[
            str | None,
            Field(min_length=1, description="Only requests for this role key."),
        ] = None,
        resource_instance: ResourceInstanceFilter = None,
        page: Page = 1,
        per_page: PerPage = 30,
    ) -> RequestPage:
        user = await self._acting_user(ctx)
        listed = await _translate(
            self._api.list_access_requests(
                user,
                status=status,
                role=role,
                resource_instance=resource_instance,
                page=page,
                per_page=per_page,
            )
        )
        return _request_page("list access requests", listed)

    async def _approve_access_request(
        self,
        ctx: Context[Any, Any],
        *,
        access_request_id: AccessRequestId,
        reviewer_comment: ReviewerComment = None,
    ) -> AccessRequestApproved:
        user = await self._acting_user(ctx)
        decided = await _translate(
            self._api.decide_access_request(
                user,
                access_request_id,
                decision="approve",
                reviewer_comment=reviewer_comment,
            )
        )
        return {"status": "approved", "access_request": decided}

    async def _deny_access_request(
        self,
        ctx: Context[Any, Any],
        *,
        access_request_id: AccessRequestId,
        reviewer_comment: ReviewerComment = None,
    ) -> AccessRequestDenied:
        user = await self._acting_user(ctx)
        decided = await _translate(
            self._api.decide_access_request(
                user,
                access_request_id,
                decision="deny",
                reviewer_comment=reviewer_comment,
            )
        )
        return {"status": "denied", "access_request": decided}

    async def _cancel_access_request(
        self, ctx: Context[Any, Any], *, access_request_id: AccessRequestId
    ) -> AccessRequestCanceled:
        user = await self._acting_user(ctx)
        canceled = await _translate(self._api.cancel_access_request(user, access_request_id))
        return {"status": "canceled", "access_request": canceled}

    async def _create_operation_approval(
        self,
        ctx: Context[Any, Any],
        *,
        reason: Reason,
        resource_instance: ResourceInstance = None,
    ) -> OperationApprovalCreated:
        user = await self._acting_user(ctx)
        created = await _translate(
            self._api.create_operation_approval(
                user, reason=reason, resource_instance=resource_instance
            )
        )
        return {"status": "created", "operation_approval": created}

    async def _list_operation_approvals(
        self,
        ctx: Context[Any, Any],
        *,
        status: StatusFilter = None,
        resource_instance: ResourceInstanceFilter = None,
        page: Page = 1,
        per_page: PerPage = 30,
    ) -> RequestPage:
        user = await self._acting_user(ctx)
        listed = await _translate(
            self._api.list_operation_approvals(
                user,
                status=status,
                resource_instance=resource_instance,
                page=page,
                per_page=per_page,
            )
        )
        return _request_page("list operation approvals", listed)

    async def _approve_operation_approval(
        self,
        ctx: Context[Any, Any],
        *,
        operation_approval_id: OperationApprovalId,
        reviewer_comment: ReviewerComment = None,
    ) -> OperationApprovalApproved:
        user = await self._acting_user(ctx)
        decided = await _translate(
            self._api.decide_operation_approval(
                user,
                operation_approval_id,
                decision="approve",
                reviewer_comment=reviewer_comment,
            )
        )
        return {"status": "approved", "operation_approval": decided}

    async def _deny_operation_approval(
        self,
        ctx: Context[Any, Any],
        *,
        operation_approval_id: OperationApprovalId,
        reviewer_comment: ReviewerComment = None,
    ) -> OperationApprovalDenied:
        user = await self._acting_user(ctx)
        decided = await _translate(
            self._api.decide_operation_approval(
                user,
                operation_approval_id,
                decision="deny",
                reviewer_comment=reviewer_comment,
            )
        )
        return {"status": "denied", "operation_approval": decided}

    async def _cancel_operation_approval(
        self, ctx: Context[Any, Any], *, operation_approval_id: OperationApprovalId
    ) -> OperationApprovalCanceled:
        user = await self._acting_user(ctx)
        canceled = await _translate(
            self._api.cancel_operation_approval(user, operation_approval_id)
        )
        return {"status": "canceled", "operation_approval": canceled}

    async def _acting_user(self, ctx: Context[Any, Any]) -> str:
        """Return the Permit user key of the caller, or fail before anything is sent.

        Raises:
            ToolError: The resolver raised, or returned no user key.

        """
        prefix = "The caller could not be identified, so nothing was sent to Permit"
        try:
            user: object = await self._identity(ctx)
        except IdentityError as exc:
            msg = f"{prefix}: {exc}"
            raise ToolError(msg) from exc
        except Exception as exc:
            # Host code: whatever it raises, the call must not go ahead.
            logger.warning("The identity resolver raised %s", type(exc).__name__, exc_info=True)
            msg = f"{prefix}: the identity resolver raised {type(exc).__name__}."
            raise ToolError(msg) from exc
        if not isinstance(user, str) or not user.strip():
            msg = f"{prefix}: the identity resolver returned no user key."
            raise ToolError(msg)
        return user


def _request_page(operation: str, listed: object) -> RequestPage:
    """Return `listed`, Permit's paginated object, as it is.

    Raises:
        ToolError: `listed` is not an object with a data list.

    """
    if not isinstance(listed, dict) or not isinstance(listed.get("data"), list):
        msg = (
            f"Permit API call to {operation} returned an unexpected shape: expected an "
            "object with a data list."
        )
        raise ToolError(msg)
    return cast("RequestPage", listed)  # pragma: no mutate - cast() does nothing at run time


async def _translate(call: Awaitable[_T]) -> _T:
    """Await a Permit API call, turning its failure into a `ToolError` for the model."""
    try:
        return await call
    except PermitApiError as exc:
        raise ToolError(str(exc)) from exc
