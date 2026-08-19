# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""The Insights MCP server.

Endpoint: /api/method/insights.mcp.handle_mcp

The MCP protocol layer is NOT ours -- `frappe_mcp` supplies the JSON-RPC dispatcher,
`initialize`, `tools/list`, `tools/call`, notification handling and the `isError`
mapping. Our contact surface with it is deliberately four symbols (`MCP`, `@mcp.tool`,
`ToolAnnotations`, `mcp.handle`) so that a stalled upstream costs a vendored file
rather than a rewrite.

What this module adds on top of upstream, and why each one is here:

  * an auth gate, because upstream has no auth code at all
  * an Origin gate, because upstream inspects no HTTP header of any kind (see origin.py)
  * an `Allow: POST` header, because upstream 405s without one (server.py:136-138)
  * a non-dict body guard, because upstream calls `.get()` on the parsed body and would
    raise AttributeError -- a Frappe 500 traceback -- on a JSON-RPC batch (server.py:145)
  * an `MCP-Protocol-Version` echo, because upstream never sets one
"""

import json

import frappe
from frappe_mcp import MCP, ToolAnnotations  # noqa: F401  (re-exported for tools/)
from frappe_mcp.server.handlers import handle_initialize
from werkzeug.wrappers import Response

from insights.mcp.origin import origin_allowed

mcp = MCP("insights")

# Read back the version upstream actually answers with rather than restating the
# constant. `handle_initialize` hard-codes it (server/handlers.py:9) and does not
# negotiate; if upstream bumps, our echoed header follows automatically.
PROTOCOL_VERSION = handle_initialize({}, "insights")["protocolVersion"]

INSIGHTS_ROLES = ("Insights User", "Insights Admin", "Insights Viewer")

# JSON-RPC error codes (types.py)
PARSE_ERROR = -32700
INVALID_REQUEST = -32600


@frappe.whitelist(allow_guest=True, methods=["POST", "GET"])
def handle_mcp():
    """Whitelisted entry point for the MCP Streamable HTTP transport.

    `allow_guest=True` is deliberate and the guest check below is what makes it safe.
    Omitting `allow_guest` makes `is_whitelisted` raise PermissionError, which Frappe
    renders as 403 -- but the MCP spec and OAuth discovery both key on 401, and a
    client that only probes for 401 sees a flat 403 and gives up.

    THE GUEST CHECK MUST REMAIN THE FIRST EXECUTABLE STATEMENT. It is asserted by
    insights/tests/mcp/test_guards.py::test_guest_check_is_first_statement.
    """
    if frappe.session.user == "Guest":
        # frappe/app.py:267-268 attaches WWW-Authenticate to a 401 for us.
        return Response(status=401)

    if not _has_insights_role():
        return _json_response(
            {
                "error": "insights_role_missing",
                "message": (
                    f"User {frappe.session.user} authenticated but holds none of "
                    f"{', '.join(INSIGHTS_ROLES)}. Add the role in Desk > User."
                ),
            },
            status=403,
        )

    if not origin_allowed(frappe.request.headers.get("Origin")):
        return _json_response({"error": "origin_not_allowed"}, status=403)

    if frappe.request.method != "POST":
        # Upstream 405s but sets no Allow header, which the spec requires.
        return Response(status=405, headers={"Allow": "POST"})

    body, parse_failed = _parse_body()
    if parse_failed:
        return _rpc_error(None, PARSE_ERROR, "Parse error: request body is not valid JSON")
    if not isinstance(body, dict):
        # Upstream would AttributeError here (server.py:145 calls data.get on a list),
        # surfacing as a Frappe 500 traceback rather than a JSON-RPC error.
        return _rpc_error(None, INVALID_REQUEST, "Invalid Request: expected a JSON object")

    # Importing the tools package runs every @mcp.tool decorator. Done here rather than
    # at module scope so a worker that never receives an MCP request never pays for it.
    # add_tool writes into an OrderedDict keyed by name, so this is idempotent.
    import insights.mcp.tools  # noqa: F401

    # Read by InsightsDataDoc.validate() to enforce that MCP may only ever write the
    # AI Note zone, even if a future tool forgets to hard-code the zone itself.
    frappe.flags.insights_mcp_write = True

    response = mcp.handle(frappe.request, Response())
    response.headers.setdefault("MCP-Protocol-Version", PROTOCOL_VERSION)
    return response


def _has_insights_role() -> bool:
    """Accept viewers.

    v1 of this design asserted `is_user or is_admin`, which rejects a legitimate
    read-only viewer: `get_user_info` computes is_viewer exclusively
    (insights/api/__init__.py:53) while `check_role` admits `Insights Viewer`.
    Write tools are gated separately, not here.
    """
    roles = set(frappe.get_roles())
    return bool(roles.intersection(INSIGHTS_ROLES))


def _parse_body() -> tuple[object, bool]:
    """Parse the request body once, without raising.

    Werkzeug 3.x raises BadRequest from `on_json_loading_failed`, which upstream's
    `except json.JSONDecodeError` does not catch (server.py:141-143) -- a malformed
    body would yield Frappe's HTML 400 instead of a JSON-RPC error. Werkzeug caches
    parsed JSON separately per `silent` flag, so this does not poison upstream's own
    `get_json(force=True)` call on the valid path.
    """
    try:
        body = frappe.request.get_json(force=True, silent=True)
    except Exception:
        return None, True

    if body is None:
        # Either an empty body or unparseable; both are parse errors for our purposes.
        return None, bool(frappe.request.get_data())

    return body, False


def _rpc_error(request_id, code: int, message: str) -> Response:
    return _json_response(
        {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}},
        status=400,
    )


def _json_response(payload: dict, status: int) -> Response:
    return Response(json.dumps(payload), status=status, mimetype="application/json")
