# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""Structural enforcement of the non-negotiables.

These are AST tests, not shell greps, and deliberately so. `grep '\\.execute('`
false-positives on `execute_ibis_query(`, on comments and on docstrings; an AST walk
does not. Running them inside the suite also means they cannot be skipped by editing
a CI config.

Each test corresponds to a numbered rule in docs/mcp-IMPLEMENTATION.md §3. If one
fails, read that rule before "fixing" the test -- every one of them was decided after
two adversarial reviews and cost real analysis.
"""

import ast
from pathlib import Path

import frappe
from frappe.tests import UnitTestCase

from insights.tests.base import InsightsIntegrationTestCase

MCP_ROOT = Path(frappe.get_app_path("insights")) / "mcp"


def _modules():
    for path in sorted(MCP_ROOT.rglob("*.py")):
        yield path, ast.parse(path.read_text(), filename=str(path))


def _rel(path: Path) -> str:
    return str(path.relative_to(MCP_ROOT.parent.parent))


def _decorator_names(node: ast.FunctionDef) -> list[str]:
    names = []
    for dec in node.decorator_list:
        target = dec.func if isinstance(dec, ast.Call) else dec
        if isinstance(target, ast.Attribute):
            names.append(target.attr)
        elif isinstance(target, ast.Name):
            names.append(target.id)
    return names


def _mcp_tools():
    """Yield (path, FunctionDef, decorator Call) for every @mcp.tool-decorated function."""
    for path, tree in _modules():
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            for dec in node.decorator_list:
                target = dec.func if isinstance(dec, ast.Call) else dec
                if isinstance(target, ast.Attribute) and target.attr == "tool":
                    yield path, node, dec


class TestMcpStructuralGuards(UnitTestCase):
    def test_rule_3_no_execute_outside_guards(self):
        """Rule 3: every transient .execute() goes through guards.execute_transient."""
        offenders = []
        for path, tree in _modules():
            if path.name == "guards.py":
                continue
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "execute"
                ):
                    offenders.append(f"{_rel(path)}:{node.lineno}")
        self.assertFalse(
            offenders,
            "Transient execution must route through guards.execute_transient "
            f"(non-negotiable #3). Found: {offenders}",
        )

    def test_no_ignore_permissions_anywhere(self):
        """§8.3: the MCP layer never bypasses permissions."""
        offenders = []
        for path, tree in _modules():
            for node in ast.walk(tree):
                if isinstance(node, ast.keyword) and node.arg == "ignore_permissions":
                    offenders.append(f"{_rel(path)}:{node.value.lineno}")
        self.assertFalse(offenders, f"ignore_permissions is banned under insights/mcp/: {offenders}")

    def test_rule_7_origin_check_is_wired(self):
        """Rule 7: a refactor dropping this call is how the control realistically dies."""
        source = (MCP_ROOT / "__init__.py").read_text()
        self.assertIn("origin_allowed(", source)

    def test_origin_check_does_not_trust_the_host_header(self):
        """See §8 G. get_url() defaults to allow_header_override=True, so absent a
        configured host_name it derives the origin from the REQUEST's own Host header.
        A DNS-rebinding caller controlling both Host and Origin would then match itself.

        Checked on the AST, not the text: the module's prose explains the hazard and
        would defeat a substring assertion.
        """
        tree = ast.parse((MCP_ROOT / "origin.py").read_text())
        calls = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get_url"
        ]
        self.assertTrue(calls, "origin.py no longer resolves the site's own origin")
        for call in calls:
            overrides = [
                kw for kw in call.keywords
                if kw.arg == "allow_header_override" and getattr(kw.value, "value", None) is False
            ]
            self.assertTrue(
                overrides,
                f"origin.py:{call.lineno} get_url() must pass allow_header_override=False",
            )

    def test_guest_check_is_first_statement_of_handle_mcp(self):
        """§3.6: allow_guest=True is only safe because of this ordering."""
        tree = ast.parse((MCP_ROOT / "__init__.py").read_text())
        handler = next(
            n for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name == "handle_mcp"
        )
        first = handler.body[0]
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
            first = handler.body[1]  # skip the docstring
        self.assertIsInstance(first, ast.If, "the guest check must come first")
        self.assertIn("Guest", ast.dump(first.test))

    def test_rule_2_every_tool_declares_an_explicit_input_schema(self):
        """Rule 2: inference cannot express enums, defaults or nested objects."""
        checked = 0
        for path, fn, dec in _mcp_tools():
            checked += 1
            kwargs = {k.arg for k in dec.keywords} if isinstance(dec, ast.Call) else set()
            self.assertIn(
                "input_schema", kwargs,
                f"{_rel(path)}:{fn.lineno} {fn.name} must pass input_schema= explicitly",
            )
        self.assertGreater(checked, 0, "no @mcp.tool functions found -- did the walker break?")

    def test_every_tool_validates_its_arguments(self):
        """§3.6: upstream never validates tools/call arguments (run_tool is dead code)."""
        for path, fn, _dec in _mcp_tools():
            self.assertIn(
                "tool_args", _decorator_names(fn),
                f"{_rel(path)}:{fn.lineno} {fn.name} must carry @tool_args(SCHEMA)",
            )

    def test_rule_6_write_tools_are_transactional(self):
        """Rule 6: upstream swallows tool exceptions, so Frappe never auto-rolls-back."""
        for path, fn, dec in _mcp_tools():
            read_only = False
            if isinstance(dec, ast.Call):
                for kw in dec.keywords:
                    if kw.arg == "annotations" and isinstance(kw.value, ast.Call):
                        for akw in kw.value.keywords:
                            if akw.arg == "readOnlyHint" and getattr(akw.value, "value", False) is True:
                                read_only = True
            if not read_only:
                self.assertIn(
                    "transactional", _decorator_names(fn),
                    f"{_rel(path)}:{fn.lineno} {fn.name} is a write tool and must carry "
                    "@transactional (non-negotiable #6)",
                )

    def test_rule_4_tools_return_str(self):
        """Rule 4: a dict return makes upstream emit the same JSON twice."""
        for path, fn, _dec in _mcp_tools():
            self.assertIsNotNone(
                fn.returns, f"{_rel(path)}:{fn.lineno} {fn.name} must annotate its return type"
            )
            self.assertEqual(
                getattr(fn.returns, "id", None), "str",
                f"{_rel(path)}:{fn.lineno} {fn.name} must return str, not "
                f"{ast.dump(fn.returns)} (non-negotiable #4)",
            )

    def test_rule_9_no_tool_emits_a_code_operation(self):
        """Rule 9: the safe_exec sandbox policy has not been read yet."""
        offenders = []
        for path, tree in _modules():
            for node in ast.walk(tree):
                # An emitted operation is a dict literal {"type": "code", ...}. Matching
                # the bare string "code" would also hit JSON-RPC error payloads.
                if not isinstance(node, ast.Dict):
                    continue
                for key, value in zip(node.keys, node.values):
                    if (
                        isinstance(key, ast.Constant) and key.value == "type"
                        and isinstance(value, ast.Constant) and value.value == "code"
                    ):
                        offenders.append(f"{_rel(path)}:{node.lineno}")
        self.assertFalse(
            offenders,
            "No MCP tool may emit a `code` operation until the safe_exec sandbox policy "
            f"is reviewed (non-negotiable #9, open question #16). Found: {offenders}",
        )

    def test_rule_8_no_mcp_resources(self):
        """Rule 8: upstream raises NotImplementedError for every resources/* handler."""
        for path, tree in _modules():
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute) and node.attr in {"resource", "resources"}:
                    self.fail(f"{_rel(path)}:{node.lineno} MCP resources are out of scope (rule 8)")


class TestPhase2StructuralGuards(UnitTestCase):
    """Rules the chart and dashboard layer needs. Each one pins a silent failure."""

    CHART_OPERATIONS = (
        Path(frappe.get_app_path("insights"))
        / "insights"
        / "doctype"
        / "insights_chart_v3"
        / "chart_operations.py"
    )
    CHART_CONTROLLER = CHART_OPERATIONS.parent / "insights_chart_v3.py"

    def test_mcp_never_calls_refresh_data_query(self):
        """The MCP layer renders through guards.execute_saved, never the doctype's own
        execute path -- that is what keeps rule 3 (one `.execute(` site) meaningful rather
        than merely literally true."""
        for path, tree in _modules():
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute) and node.attr == "refresh_data_query":
                    self.fail(
                        f"{_rel(path)} calls refresh_data_query. Call sync_data_query() and "
                        f"render through guards.execute_saved -- see §3 rule 3."
                    )

    def test_chart_operations_is_a_pure_port(self):
        """It must stay unit-testable and free of the render path, or the port and the
        TypeScript drift with nothing cheap enough to catch it."""
        tree = ast.parse(self.CHART_OPERATIONS.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in ("execute", "get_doc", "get_all"):
                self.fail(f"chart_operations.py must not call {node.attr}(); keep it pure.")
            if isinstance(node, ast.Attribute) and node.attr == "db":
                self.fail("chart_operations.py must not touch frappe.db; keep it pure.")

    def test_the_chart_controller_executes_in_exactly_one_place(self):
        tree = ast.parse(self.CHART_CONTROLLER.read_text())
        sites = [
            fn.name
            for fn in ast.walk(tree)
            if isinstance(fn, ast.FunctionDef)
            for call in ast.walk(fn)
            if isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and call.func.attr == "execute"
        ]
        self.assertEqual(sites, ["refresh_data_query"], f"unexpected execute sites: {sites}")

    def test_sync_data_query_propagates_use_live_connection(self):
        """One assignment, and omitting it renders every chart blank with no error
        (design §7.2). Cheap to pin, expensive to rediscover."""
        source = self.CHART_CONTROLLER.read_text()
        self.assertIn("data_query.use_live_connection = source_query.use_live_connection", source)

    def test_no_tool_supplies_a_data_query(self):
        """`data_query` is read_only and minted by set_data_query()'s raw db_insert.
        Supplying one orphans a query row that nothing will ever delete."""
        for path, tree in _modules():
            for node in ast.walk(tree):
                if isinstance(node, ast.keyword) and node.arg == "data_query":
                    self.fail(f"{_rel(path)} passes data_query=")
                if (
                    isinstance(node, ast.Assign)
                    and any(
                        isinstance(t, ast.Attribute) and t.attr == "data_query" for t in node.targets
                    )
                ):
                    self.fail(f"{_rel(path)} assigns .data_query")

    def test_no_tool_writes_linked_charts(self):
        """It is derived in before_save (`insights_dashboard_v3.py:82-86`); writing it
        corrupts the child table."""
        for path, tree in _modules():
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and node.value == "linked_charts":
                    self.fail(f"{_rel(path)} mentions linked_charts")

    def test_delete_item_cannot_target_a_workbook(self):
        """Non-negotiable #10. Insights Workbook.on_trash force-deletes everything in it."""
        from insights.mcp.schemas import DELETE_ITEM

        self.assertNotIn("workbook", DELETE_ITEM["properties"]["type"]["enum"])

    def test_every_tool_module_is_imported(self):
        """A module missing from tools/__init__.py registers nothing and fails no other
        test. This is the only thing standing between a typo and a vanished tool."""
        registration = (MCP_ROOT / "tools" / "__init__.py").read_text()
        for path in sorted((MCP_ROOT / "tools").glob("*.py")):
            if path.name == "__init__.py":
                continue
            with self.subTest(module=path.stem):
                self.assertIn(path.stem, registration)

    def test_the_registered_tool_surface_has_not_shrunk(self):
        import insights.mcp.tools  # noqa: F401
        from insights.mcp import mcp

        expected = {
            "list_data_sources", "list_tables", "describe_table", "distinct_values",
            "get_docs", "write_ai_note", "run_query",
            "save_query", "list_workbooks", "get_item", "delete_item",
            "create_chart", "update_chart", "create_dashboard", "update_dashboard",
        }
        self.assertLessEqual(expected, set(mcp._tool_registry))


class TestExecuteTransient(InsightsIntegrationTestCase):
    """Behavioural tests for the choke point.

    Read the honest-scope note at the top of insights/mcp/guards.py before adding a
    test that asserts table-level isolation: with enable_permissions = 0 there is none,
    and a test claiming otherwise would pass for the wrong reason.
    """

    OPS = [
        {"type": "source", "table": {"type": "table", "data_source": "demo_data", "table_name": "orders"}},
        {"type": "limit", "limit": 3},
    ]

    @classmethod
    def before_class(cls):
        cls.has_demo_data = bool(
            frappe.db.exists("Insights Table v3", {"data_source": "demo_data", "table": "orders"})
        )

    def test_empty_operations_is_a_tool_error(self):
        from insights.mcp.errors import ToolError
        from insights.mcp.guards import execute_transient

        with self.assertRaises(ToolError):
            execute_transient([])

    def test_resolved_tables_are_derived_from_operations(self):
        """Derived, not accepted as an argument -- a caller cannot omit a table."""
        from insights.mcp.guards import resolved_tables

        self.assertEqual(
            resolved_tables(self.OPS),
            [{"data_source": "demo_data", "table_name": "orders"}],
        )

    def test_user_without_insights_role_is_refused(self):
        from insights.mcp.errors import ToolError
        from insights.mcp.guards import execute_transient
        from insights.tests.factories import as_user, create_user

        user = create_user("mcp-guards-noroles@example.com", roles=[])
        try:
            with as_user(user.name), self.assertRaises(ToolError) as ctx:
                execute_transient(self.OPS)
            self.assertIn("access", str(ctx.exception).lower())
        finally:
            frappe.set_user("Administrator")
            frappe.delete_doc("User", user.name, force=True, ignore_permissions=True)

    def test_execution_is_attributable_in_the_query_log(self):
        """The `mcp-` prefix is the audit trail -- every MCP execution is greppable."""
        if not self.has_demo_data:
            self.skipTest("demo_data.orders not present on this bench")

        from insights.insights.doctype.insights_data_source_v3.insights_data_source_v3 import (
            db_connections,
        )
        from insights.mcp.guards import execute_transient

        with db_connections():
            execute_transient(self.OPS, page_size=3, force=True)

        logged = frappe.get_all(
            "Insights Query Execution Log",
            filters={"query": ["like", "mcp-%"]},
            fields=["query"],
            order_by="creation desc",
            limit=1,
        )
        self.assertTrue(logged, "no execution log row attributed to an mcp- query")
        self.assertTrue(logged[0]["query"].startswith("mcp-"))

    def test_build_transient_returns_an_expression_without_rows(self):
        if not self.has_demo_data:
            self.skipTest("demo_data.orders not present on this bench")

        from insights.insights.doctype.insights_data_source_v3.insights_data_source_v3 import (
            db_connections,
        )
        from insights.mcp.guards import build_transient

        with db_connections():
            expr = build_transient(self.OPS)
            self.assertIn("order_id", expr.schema().keys())
