"""The Permit API calls the tools make. The only module that sends HTTP requests.

The access-request and operation-approval calls both act with the permissions of the acting
user. The access-request calls name that user in the request path, and Permit applies the
user's Elements permissions: a reviewer must be allowed to review, and nobody approves their
own request. The operation-approval calls first log in to Elements as that user and send the
Elements token the login returns. Listing resource instances and looking up the requesting
users of a listing use the server's API key.
"""

import asyncio
import http
import json
import urllib.parse
import urllib.request
from collections.abc import Iterable, Mapping
from typing import Any, Literal, TypeVar
from uuid import UUID

import aiohttp

from permit_mcp._log import redact_recent, scrub
from permit_mcp.config import ENV_VARS, Settings

MAX_ERROR_BODY = 2000
REQUEST_TIMEOUT_SECONDS = 30
MAX_CONCURRENT_USER_LOOKUPS = 5

Decision = Literal["approve", "deny"]
_T = TypeVar("_T")


class PermitApiError(Exception):
    """A Permit API call failed, or was refused before it was sent.

    Attributes:
        operation: What the call was for, such as "approve access request".
        status: The HTTP status of the response, or None when there was no usable HTTP
            answer (the API was unreachable or timed out, the answer was a failed Elements
            login, or the call was refused before it was sent).
        body: The response body, or an explanation when there is none, with every
            registered secret redacted, then cut to 2000 characters.

    """

    def __init__(self, operation: str, status: int | None, body: str) -> None:
        """Build the error; `body` is redacted, then truncated, here.

        Args:
            operation: What the call was for.
            status: The HTTP status, or None.
            body: The response body or an explanation.

        """
        self.operation = operation
        self.status = status
        # Redacted before it is cut, so a secret that straddles the cut cannot leak in part.
        self.body = scrub(body)[:MAX_ERROR_BODY]
        if status is None:
            message = f"Permit API call to {operation} failed: {self.body}"
        else:
            message = f"Permit API call to {operation} returned HTTP {status}: {self.body}"
        super().__init__(message)


class PermitApi:
    """Calls the Permit API for one server, through one HTTP session.

    One instance belongs to one event loop: the first call opens the session on the running
    loop, and every later call must run on that loop. `aclose()` closes the session, and a
    call made after it opens a new one. Cookies are never stored, so nothing one call
    receives is sent by another. Redirects are not followed. Proxies come from the
    environment (HTTP_PROXY, HTTPS_PROXY and NO_PROXY, or the system settings), and a netrc
    file is never read. The project and environment of the API key are asked for once and
    kept.
    """

    def __init__(self, settings: Settings) -> None:
        """Prepare the calls; no request is sent until the first call.

        Args:
            settings: The validated server settings.

        """
        self._settings = settings
        self._session: aiohttp.ClientSession | None = None
        self._scope_lock = asyncio.Lock()
        self._scope: tuple[str, str] | None = None

    async def aclose(self) -> None:
        """Close the HTTP session. Calling it again, or before any call, does nothing.

        A later call opens a new session.
        """
        session, self._session = self._session, None
        if session is not None and not session.closed:
            await session.close()

    async def list_resource_instances(self, *, page: int, per_page: int) -> object:
        """List the instances of the configured resource type in the configured tenant.

        The listing uses the server's API key: it is not filtered by any user's permissions.

        Args:
            page: Page number, from 1.
            per_page: Results per page.

        Returns:
            The API's JSON as returned.

        """
        return await self._call(
            "list resource instances",
            "GET",
            await self._environment_url("facts", "resource_instances"),
            params={
                "tenant": self._settings.tenant,
                "resource": self._settings.resource,
                "page": page,
                "per_page": per_page,
            },
        )

    async def create_access_request(
        self, user: str, *, role: str, reason: str, resource_instance: str | None
    ) -> object:
        """File an access request for `user` for `role` on the configured resource.

        Args:
            user: Key of the acting user, who requests the access.
            role: Key of the requested role.
            reason: Why the access is needed.
            resource_instance: Key or ID of one resource instance, or None for the type.

        Returns:
            The created access request, as the API returns it.

        """
        details = {
            "tenant": self._settings.tenant,
            "resource": self._settings.resource,
            "role": role,
        }
        if resource_instance is not None:
            details["resource_instance"] = resource_instance
        return await self._call(
            "create access request",
            "POST",
            await self._access_requests_url(user),
            json_body={"access_request_details": details, "reason": reason},
        )

    async def list_access_requests(  # noqa: PLR0913 - keyword-only, one per API filter
        self,
        user: str,
        *,
        status: str | None,
        role: str | None,
        resource_instance: str | None,
        page: int,
        per_page: int,
    ) -> object:
        """List the access requests of the configured resource that `user` may see.

        Args:
            user: Key of the acting user.
            status: Only requests with this status, or None for all.
            role: Only requests for this role key, or None for all.
            resource_instance: Only requests on this resource instance, or None for all.
            page: Page number, from 1.
            per_page: Results per page.

        Returns:
            The API's paginated JSON as returned.

        """
        query: dict[str, str | int | None] = {
            "status": status,
            "role": role,
            "resource": self._settings.resource,
            "resource_instance_id": resource_instance,
            "page": page,
            "per_page": per_page,
        }
        return await self._call(
            "list access requests",
            "GET",
            await self._access_requests_url(user),
            params=_without_none(query),
        )

    async def decide_access_request(
        self,
        user: str,
        access_request_id: UUID,
        *,
        decision: Decision,
        reviewer_comment: str | None,
    ) -> object:
        """Approve or deny an access request as `user`; approving grants the requested role.

        Args:
            user: Key of the acting user, who reviews the request.
            access_request_id: ID of the access request.
            decision: "approve" or "deny".
            reviewer_comment: A comment stored with the decision, or None.

        Returns:
            The updated access request as the API returns it, or None for an empty body.

        """
        return await self._call(
            f"{decision} access request",
            "PUT",
            await self._access_requests_url(user, str(access_request_id), decision),
            json_body=_without_none({"reviewer_comment": reviewer_comment}),
        )

    async def create_operation_approval(
        self, user: str, *, reason: str, resource_instance: str | None
    ) -> object:
        """File an operation approval request as `user` on the configured resource.

        Args:
            user: Key of the acting user, who requests the approval.
            reason: Why the operation is needed.
            resource_instance: Key or ID of one resource instance, or None for the type.

        Returns:
            The created operation approval, as the API returns it.

        """
        details = {"tenant": self._settings.tenant, "resource": self._settings.resource}
        if resource_instance is not None:
            details["resource_instance"] = resource_instance
        return await self._elements_call(
            user,
            "create operation approval",
            "POST",
            await self._operation_approvals_url(),
            json_body={"access_request_details": details, "reason": reason},
        )

    async def list_operation_approvals(
        self,
        user: str,
        *,
        status: str | None,
        resource_instance: str | None,
        page: int,
        per_page: int,
    ) -> object:
        """List the operation approvals of the configured resource that `user` may see.

        Args:
            user: Key of the acting user.
            status: Only approvals with this status, or None for all.
            resource_instance: Only approvals on this resource instance, or None for all.
            page: Page number, from 1.
            per_page: Results per page.

        Returns:
            The API's paginated JSON as returned.

        """
        query: dict[str, str | int | None] = {
            "resource": self._settings.resource,
            "status": status,
            "resource_instance": resource_instance,
            "page": page,
            "per_page": per_page,
        }
        return await self._elements_call(
            user,
            "list operation approvals",
            "GET",
            await self._operation_approvals_url(),
            params=_without_none(query),
        )

    async def decide_operation_approval(
        self,
        user: str,
        operation_approval_id: UUID,
        *,
        decision: Decision,
        reviewer_comment: str | None,
    ) -> object:
        """Approve or deny an operation approval request as `user`.

        Args:
            user: Key of the acting user, who reviews the request.
            operation_approval_id: ID of the operation approval.
            decision: "approve" or "deny".
            reviewer_comment: A comment stored with the decision, or None.

        Returns:
            The updated operation approval as the API returns it, or None for an empty body.

        """
        return await self._elements_call(
            user,
            f"{decision} operation approval",
            "PUT",
            await self._operation_approvals_url(str(operation_approval_id), decision),
            json_body=_without_none({"reviewer_comment": reviewer_comment}),
        )

    async def get_users(self, ids: Iterable[str]) -> dict[str, object]:
        """Fetch users by ID or key: one request per distinct ID, at most 5 at a time.

        Args:
            ids: IDs or keys of users; repeats are fetched once.

        Returns:
            Each ID mapped to the user as the API returns it. An ID the API answers with
            404 (such as a deleted user) is left out.

        Raises:
            PermitApiError: A lookup failed with anything other than 404.

        """
        limit = asyncio.Semaphore(MAX_CONCURRENT_USER_LOOKUPS)

        async def fetch(user_id: str) -> tuple[str, object]:
            async with limit:
                url = await self._environment_url("facts", "users", user_id)
                try:
                    return user_id, await self._call("get user", "GET", url)
                except PermitApiError as exc:
                    if exc.status == http.HTTPStatus.NOT_FOUND:
                        return user_id, None
                    raise

        distinct = list(dict.fromkeys(ids))
        results = await asyncio.gather(*(fetch(user_id) for user_id in distinct))
        return {user_id: user for user_id, user in results if user is not None}

    async def _key_scope(self) -> tuple[str, str]:
        """Return the project and environment IDs of the API key, asked for once."""
        if self._scope is not None:
            return self._scope
        async with self._scope_lock:
            if self._scope is None:
                self._scope = await self._fetch_key_scope()
            return self._scope

    async def _fetch_key_scope(self) -> tuple[str, str]:
        operation = "resolve the API key's environment"
        scope = await self._call(operation, "GET", self._url("v2", "api-key", "scope"))
        if not isinstance(scope, dict):
            raise PermitApiError(operation, None, "the response is not a JSON object")
        project, environment = scope.get("project_id"), scope.get("environment_id")
        if not (isinstance(project, str) and project):
            level = "an organization"
        elif not (isinstance(environment, str) and environment):
            level = "a project"
        else:
            return project, environment
        raise PermitApiError(
            operation,
            None,
            f"{ENV_VARS['api_key']} is {level}-level API key; this server needs an "
            "environment-level API key.",
        )

    async def _access_requests_url(self, user: str, *extra: str) -> str:
        element = _element(self._settings.access_request_element, "access_request_element")
        return await self._environment_url(
            "facts", "access_requests", element, "user", user,
            "tenant", self._settings.tenant, *extra,
        )  # fmt: skip

    async def _operation_approvals_url(self, *extra: str) -> str:
        element = _element(self._settings.operation_approval_element, "operation_approval_element")
        return await self._environment_url(
            "elements", "config", element, "operation_approval", *extra
        )

    async def _environment_url(self, api: str, *segments: str) -> str:
        """Return the URL of `segments` under `api`, in the API key's project and environment.

        The segments are checked before the project and environment are looked up, so a
        refused segment fails before any request is sent.
        """
        _check_segments(segments)
        project, environment = await self._key_scope()
        return self._url("v2", api, project, environment, *segments)

    def _url(self, *segments: str) -> str:
        """Return the API URL of the path of `segments`, each escaped as one path segment."""
        _check_segments(segments)
        path = "/".join(urllib.parse.quote(segment, safe="") for segment in segments)
        return f"{self._settings.api_url}/{path}"

    async def _elements_call(  # noqa: PLR0913 - the request parts are keyword-only
        self,
        user: str,
        operation: str,
        method: str,
        url: str,
        *,
        params: Mapping[str, str | int] | None = None,
        json_body: Mapping[str, Any] | None = None,
    ) -> object:
        """Log in to Elements as `user`, then make the call with that login's token.

        The token is fetched for this call alone, never shared between calls or users,
        and is redacted from logs and errors from the moment it arrives.
        """
        token = await self._elements_token(user)
        return await self._call(
            operation, method, url, token=token, params=params, json_body=json_body
        )

    async def _elements_token(self, user: str) -> str:
        """Return the Elements token from elements_login_as, for `user` in the tenant."""
        operation = "log in to Elements as the acting user"
        login = await self._call(
            operation,
            "POST",
            self._url("v2", "auth", "elements_login_as"),
            json_body={"user_id": user, "tenant_id": self._settings.tenant},
        )
        if not isinstance(login, dict):
            raise PermitApiError(operation, None, "the response is not a JSON object")
        token = login.get("element_bearer_token")
        error = login.get("error")
        if error or not isinstance(token, str) or not token:
            # A failed login comes back as HTTP 200 with the reason in "error".
            reason = error or "the response has no Elements token"
            code = login.get("error_code")
            detail = f" (error code {code})" if code is not None else ""
            raise PermitApiError(operation, None, f"{reason}{detail}")
        redact_recent(token)
        return token

    async def _call(  # noqa: PLR0913 - the request parts are keyword-only
        self,
        operation: str,
        method: str,
        url: str,
        *,
        token: str | None = None,
        params: Mapping[str, str | int] | None = None,
        json_body: Mapping[str, Any] | None = None,
    ) -> object:
        """Send one request and return its JSON, or None when the body is empty.

        Args:
            operation: What the call is for, used in errors.
            method: The HTTP method.
            url: The full URL.
            token: The Bearer token; the API key when None.
            params: Query parameters.
            json_body: The JSON body; none when None.

        Raises:
            PermitApiError: The API was unreachable or timed out, answered with a status
                outside 2xx (a redirect included), or answered with a body that is not JSON.

        """
        headers = {"Authorization": f"Bearer {token or self._settings.api_key}"}
        host = _host_of(url)
        try:
            proxy = await asyncio.to_thread(_proxy_for, url)
            async with self._current_session().request(
                method,
                url,
                headers=headers,
                params=params,
                json=json_body,
                proxy=proxy,
                allow_redirects=False,
            ) as response:
                status = response.status
                raw = await response.read()
        except TimeoutError as exc:
            raise PermitApiError(
                operation,
                None,
                f"no response from {host} within {REQUEST_TIMEOUT_SECONDS} seconds",
            ) from exc
        except aiohttp.ClientError as exc:
            raise PermitApiError(
                operation, None, f"could not reach {host}: {type(exc).__name__}: {exc}"
            ) from exc
        text = raw.decode("utf-8", errors="replace")
        if not http.HTTPStatus.OK <= status < http.HTTPStatus.MULTIPLE_CHOICES:
            raise PermitApiError(operation, status, text)
        if not text.strip():
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise PermitApiError(operation, status, f"the response is not JSON: {text}") from exc

    def _current_session(self) -> aiohttp.ClientSession:
        """Return the HTTP session, opened on first use."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS),
                cookie_jar=aiohttp.DummyCookieJar(),
                # Proxies are chosen per request by _proxy_for; this also keeps netrc unread,
                # whose credentials would clash with the Authorization header.
                trust_env=False,
            )
        return self._session


def _element(value: str | None, setting: str) -> str:
    """Return the configured element `value` of `setting`.

    The tools of an element are registered only when it is set, so this fails only for a
    direct call that bypasses them.

    Raises:
        PermitApiError: The element is not set.

    """
    if value is None:
        operation = f"use the {setting.replace('_', ' ')}"
        raise PermitApiError(operation, None, f"{ENV_VARS[setting]} is not set.")
    return value


def _check_segments(segments: Iterable[str]) -> None:
    r"""Refuse a path segment that would change the path even when escaped.

    Raises:
        PermitApiError: A segment is empty, "." or "..", or contains "/" or "\".

    """
    operation = "build the request path"
    for segment in segments:
        if segment in {"", ".", ".."} or "/" in segment or "\\" in segment:
            raise PermitApiError(
                operation,
                None,
                f"{segment!r} cannot be used as an ID or key in a request path (empty, '.', "
                "'..', and values containing '/' or '\\' are refused); nothing was sent.",
            )


def _host_of(url: str) -> str:
    """Return the host of `url` and its port, if it has one, for error messages."""
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    return f"{host}:{parts.port}" if parts.port is not None else host


def _proxy_for(url: str) -> str | None:
    """Return the proxy the environment or system settings name for `url`, or None.

    It blocks: on some systems the bypass check resolves host names.
    """
    parts = urllib.parse.urlsplit(url)
    proxy = urllib.request.getproxies().get(parts.scheme)
    if proxy is None or (parts.hostname and urllib.request.proxy_bypass(parts.hostname)):
        return None
    return proxy


def _without_none(values: Mapping[str, _T | None]) -> dict[str, _T]:
    return {key: value for key, value in values.items() if value is not None}
