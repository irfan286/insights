# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""Wire-level acceptance tests for the MCP transport.

These are driven through Frappe's WSGI test client, NOT by calling `handle_mcp()`
directly, and that is load-bearing: `WWW-Authenticate` is attached by
`frappe/app.py:267-268` in the application, not by our handler. A direct call can
assert the 401 but can never assert the header, and the header is half the
acceptance criterion.

Because the WSGI app opens its own DB connection, every fixture these tests depend on
must be COMMITTED before the request runs -- hence `before_class`, which the base
class commits (COMMIT_AFTER_CLASS_SETUP = True), never `before_test`.
"""

import json
from threading import Thread
from unittest.mock import patch

import frappe
from frappe.utils import get_test_client

from insights.tests.base import InsightsIntegrationTestCase
from insights.tests.factories import create_user

MCP_USER = "mcp-transport@example.com"
ALLOWED_ORIGIN = "https://good.example"
ENDPOINT = "/api/method/insights.mcp.handle_mcp"

INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-03-26",
        "capabilities": {},
        "clientInfo": {"name": "insights-tests", "version": "0"},
    },
}


class _RequestThread(Thread):
    """Run a WSGI call on its own connection, with the site name pinned.

    Mirrors frappe.tests.test_api.ThreadWithReturnValue without importing it, so a
    refactor of Frappe's own test helpers cannot silently break this suite.
    """

    def __init__(self, target, kwargs, site):
        super().__init__(target=target, kwargs=kwargs)
        self._target_fn = target
        self._kwargs = kwargs
        self._site = site
        self.result = None

    def run(self):
        with patch("frappe.app.get_site_name", return_value=self._site):
            self.result = self._target_fn(**self._kwargs)


class TestMcpTransport(InsightsIntegrationTestCase):
    @classmethod
    def before_class(cls):
        cls.client = get_test_client()
        cls.site = frappe.local.site

        user = create_user(MCP_USER, roles=["Insights User"])
        from frappe.core.doctype.user.user import generate_keys

        cls.api_secret = generate_keys(user.name)["api_secret"]
        cls.api_key = frappe.db.get_value("User", user.name, "api_key")

        cls.settings_before = frappe.db.get_single_value(
            "Insights Settings", "mcp_allowed_origins"
        )
        frappe.db.set_single_value("Insights Settings", "mcp_allowed_origins", ALLOWED_ORIGIN)

    @classmethod
    def after_class(cls):
        frappe.db.set_single_value(
            "Insights Settings", "mcp_allowed_origins", cls.settings_before or ""
        )
        frappe.delete_doc("User", MCP_USER, force=True, ignore_permissions=True)

    # ---- helpers ---------------------------------------------------------

    def call(self, body=None, *, method="POST", auth=True, origin=None, raw=None, host=None):
        headers = {}
        if auth:
            headers["Authorization"] = f"token {self.api_key}:{self.api_secret}"
        if origin:
            headers["Origin"] = origin
        if host:
            headers["Host"] = host

        data = raw if raw is not None else json.dumps(INITIALIZE if body is None else body)

        thread = _RequestThread(
            target=self.client.open,
            kwargs=dict(
                path=ENDPOINT,
                method=method,
                headers=headers,
                data=data,
                content_type="application/json",
            ),
            site=self.site,
        )
        thread.start()
        thread.join()
        return thread.result

    @staticmethod
    def body(response) -> dict:
        return json.loads(response.get_data(as_text=True))

    # ---- auth ------------------------------------------------------------

    def test_unauthenticated_gets_401_with_challenge(self):
        """allow_guest=True is only safe because of the first-statement guest check."""
        response = self.call(auth=False)
        self.assertEqual(response.status_code, 401)
        self.assertIn("WWW-Authenticate", response.headers)
        self.assertTrue(response.headers["WWW-Authenticate"].startswith("Bearer resource_metadata="))
        self.assertIn("/.well-known/oauth-protected-resource", response.headers["WWW-Authenticate"])

    def test_authenticated_user_without_insights_role_gets_403(self):
        plain = create_user("mcp-noroles@example.com", roles=[])
        secret = None
        try:
            from frappe.core.doctype.user.user import generate_keys

            secret = generate_keys(plain.name)["api_secret"]
            key = frappe.db.get_value("User", plain.name, "api_key")
            frappe.db.commit()  # the WSGI request reads on its own connection

            saved_key, saved_secret = self.api_key, self.api_secret
            self.api_key, self.api_secret = key, secret
            try:
                response = self.call()
            finally:
                self.api_key, self.api_secret = saved_key, saved_secret

            self.assertEqual(response.status_code, 403)
            self.assertEqual(self.body(response)["error"], "insights_role_missing")
        finally:
            frappe.delete_doc("User", plain.name, force=True, ignore_permissions=True)
            frappe.db.commit()

    # ---- method ----------------------------------------------------------

    def test_get_is_405_with_allow_header(self):
        """Upstream 405s but sets no Allow header (server.py:136-138); we add it."""
        response = self.call(method="GET", raw="")
        self.assertEqual(response.status_code, 405)
        self.assertEqual(response.headers.get("Allow"), "POST")

    # ---- origin ----------------------------------------------------------

    def test_absent_origin_is_allowed(self):
        """Every real MCP client sends no Origin; refusing them would buy nothing."""
        response = self.call()
        self.assertEqual(response.status_code, 200)

    def test_allowlisted_origin_is_allowed(self):
        response = self.call(origin=ALLOWED_ORIGIN)
        self.assertEqual(response.status_code, 200)

    def test_foreign_origin_is_refused_and_no_tool_runs(self):
        response = self.call(
            body={
                "jsonrpc": "2.0",
                "id": 9,
                "method": "tools/call",
                "params": {"name": "ping_insights", "arguments": {}},
            },
            origin="https://evil.example",
        )
        self.assertEqual(response.status_code, 403)
        payload = self.body(response)
        self.assertEqual(payload["error"], "origin_not_allowed")
        # The refusal is transport-level: no JSON-RPC envelope, so no tool result.
        self.assertNotIn("result", payload)

    def test_origin_differing_only_in_scheme_is_refused(self):
        """Catches a sloppy startswith/substring regression."""
        response = self.call(origin=ALLOWED_ORIGIN.replace("https://", "http://"))
        self.assertEqual(response.status_code, 403)

    def test_origin_differing_only_in_port_is_refused(self):
        response = self.call(origin=f"{ALLOWED_ORIGIN}:8443")
        self.assertEqual(response.status_code, 403)

    def test_origin_suffix_attack_is_refused(self):
        response = self.call(origin=f"{ALLOWED_ORIGIN}.evil.com")
        self.assertEqual(response.status_code, 403)

    def test_spoofed_host_header_cannot_self_authorize(self):
        """Regression test for the DNS-rebinding hole (see §8 G of the handoff doc).

        frappe.utils.get_url() defaults to allow_header_override=True and, absent a
        configured host_name, derives the origin from the request's own Host header.
        If origin.py ever drops allow_header_override=False, an attacker who controls
        both Host and Origin matches themselves and walks through the gate.
        """
        response = self.call(host="evil.example", origin="http://evil.example")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.body(response)["error"], "origin_not_allowed")

    # ---- protocol --------------------------------------------------------

    def test_initialize_round_trip(self):
        response = self.call()
        self.assertEqual(response.status_code, 200)
        result = self.body(response)["result"]
        self.assertEqual(result["protocolVersion"], "2025-03-26")
        self.assertEqual(result["serverInfo"]["name"], "insights")
        self.assertIn("tools", result["capabilities"])
        # Upstream advertises no resources capability, so a conformant client never
        # asks for one -- which is why we do not build them (non-negotiable #8).
        self.assertNotIn("resources", result["capabilities"])

    def test_protocol_version_header_is_echoed(self):
        from insights.mcp import PROTOCOL_VERSION

        response = self.call()
        self.assertEqual(response.headers.get("MCP-Protocol-Version"), PROTOCOL_VERSION)

    def test_notification_returns_202_with_empty_body(self):
        response = self.call(body={"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.get_data(as_text=True), "")

    def test_array_body_returns_invalid_request_not_500(self):
        """Upstream calls .get() on the parsed body (server.py:145); a list would
        AttributeError into a Frappe 500 traceback rather than a JSON-RPC error."""
        response = self.call(raw=json.dumps([INITIALIZE]))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.body(response)["error"]["code"], -32600)

    def test_scalar_body_is_rejected(self):
        """A scalar JSON body never reaches us -- Frappe rejects it first.

        `make_form_dict` (frappe/app.py:338-345) routes a dict to form_dict, stashes a
        list under form_dict["data"] and lets both through, but `frappe.throw`s on
        anything else. So the list case is ours to handle (and is the one that matters,
        being the JSON-RPC batch shape upstream would AttributeError on) while scalars
        are pre-empted at 417. Asserting the exact code would couple this suite to a
        Frappe internal; asserting "rejected, and no tool ran" is the real contract."""
        response = self.call(raw=json.dumps("just a string"))
        self.assertIn(response.status_code, (400, 417))
        self.assertNotIn("result", self.body(response))

    def test_tools_list_exposes_explicit_input_schema(self):
        response = self.call(body={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        self.assertEqual(response.status_code, 200)
        tools = self.body(response)["result"]["tools"]
        self.assertTrue(tools, "no tools registered")
        for tool in tools:
            self.assertIn("inputSchema", tool)
            self.assertEqual(tool["inputSchema"].get("type"), "object")
