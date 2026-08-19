# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""The compiler against the real backend.

test_compiler.py proves the emitted JSON matches what we intended. This proves what we
intended is what `IbisQueryBuilder` actually accepts -- the only test that catches the
compiler drifting from `ibis_utils.py`. Golden tests alone cannot: every trap this
layer exists to avoid (unknown op type, wrong enum casing, a granularity on the wrong
column type) is a SILENT no-op or a silently-wrong answer, so a drifted compiler still
produces green golden tests and wrong data.

Runs against `demo_data` (DuckDB, local file, no network). Skips if absent.
"""

import frappe

from insights.insights.doctype.insights_data_source_v3.insights_data_source_v3 import (
    db_connections,
)
from insights.mcp.compiler import compile
from insights.mcp.guards import build_transient, execute_transient
from insights.tests.base import InsightsIntegrationTestCase

SOURCE = "demo_data"


class TestCompilerAgainstBackend(InsightsIntegrationTestCase):
    @classmethod
    def before_class(cls):
        cls.available = bool(
            frappe.db.exists("Insights Table v3", {"data_source": SOURCE, "table": "orders"})
            and frappe.db.exists("Insights Table v3", {"data_source": SOURCE, "table": "orderitems"})
        )

    def setUp(self):
        super().setUp()
        if not self.available:
            self.skipTest(f"{SOURCE} orders/orderitems not present on this bench")

    def assert_schema_matches_symbol_table(self, spec):
        """The Phase 2 contract: the symbol table IS the query's output columns."""
        with db_connections():
            ops, sym = compile(spec)
            expr = build_transient(ops)
        self.assertEqual(list(expr.schema().keys()), list(sym.names))
        return ops, sym, expr

    def test_flagship_example_executes_and_matches_the_symbol_table(self):
        ops, sym, _ = self.assert_schema_matches_symbol_table({
            "from": {"data_source": SOURCE, "table": "orders"},
            "joins": [{"table": "orderitems", "left_on": "order_id",
                       "right_on": "order_id", "select": ["price"]}],
            "where": [{"column": "order_status", "op": "not_in", "value": ["delivered"]}],
            "group_by": [{"column": "order_purchase_timestamp", "granularity": "month",
                          "as": "order_month"}],
            "aggregate": [{"column": "price", "fn": "sum"}, {"fn": "count"}],
            "having": [{"column": "count_of_rows", "op": ">", "value": 10}],
            "sort": [{"column": "order_month", "desc": True}],
            "limit": 5,
        })
        with db_connections():
            result = execute_transient(ops, page_size=5)

        self.assertEqual([c["name"] for c in result["columns"]], list(sym.names))
        self.assertTrue(result["rows"])
        months = [r["order_month"] for r in result["rows"]]
        self.assertEqual(months, sorted(months, reverse=True), "desc sort did not apply")

    def test_trailing_select_removes_the_forced_join_key(self):
        """§8 C, measured: without it the output carries `orderitems_order_id`."""
        _, _, expr = self.assert_schema_matches_symbol_table({
            "from": {"data_source": SOURCE, "table": "orders"},
            "joins": [{"table": "orderitems", "left_on": "order_id",
                       "right_on": "order_id", "select": ["price"]}],
        })
        self.assertNotIn("orderitems_order_id", expr.schema().keys())
        self.assertIn("price", expr.schema().keys())

    def test_string_column_auto_cast_actually_groups(self):
        """The auto-cast branch has to survive a real backend, not just golden JSON."""
        spec = {
            "from": {"data_source": SOURCE, "table": "orders"},
            "derive": [{"name": "order_day_str", "data_type": "String",
                        "expression": "order_purchase_timestamp.cast('string')"}],
            "group_by": [{"column": "order_day_str", "granularity": "month",
                          "as": "order_month"}],
            "aggregate": [{"fn": "count"}],
            "limit": 3,
        }
        with db_connections():
            ops, sym = compile(spec)
            self.assertTrue(
                [o for o in ops if o["type"] == "cast"],
                "expected an auto-cast for a String group_by with granularity",
            )
            result = execute_transient(ops, page_size=3)
        self.assertEqual([c["name"] for c in result["columns"]], list(sym.names))

    def test_filters_and_valueless_operators_execute(self):
        with db_connections():
            ops, sym = compile({
                "from": {"data_source": SOURCE, "table": "orders"},
                "where": [{"column": "order_id", "op": "is_set"}],
                "where_any": [{"column": "order_status", "op": "=", "value": "delivered"},
                              {"column": "order_status", "op": "=", "value": "shipped"}],
                "select": ["order_id", "order_status"],
                "limit": 5,
            })
            result = execute_transient(ops, page_size=5)
        self.assertEqual([c["name"] for c in result["columns"]], list(sym.names))
        self.assertTrue(all(r["order_status"] in ("delivered", "shipped") for r in result["rows"]))

    def test_count_sentinel_returns_an_integer_count(self):
        with db_connections():
            ops, _ = compile({
                "from": {"data_source": SOURCE, "table": "orders"},
                "group_by": [{"column": "order_status"}],
                "aggregate": [{"fn": "count"}],
                "sort": [{"column": "count_of_rows", "desc": True}],
                "limit": 3,
            })
            result = execute_transient(ops, page_size=3)
        self.assertTrue(result["rows"])
        counts = [r["count_of_rows"] for r in result["rows"]]
        self.assertTrue(all(isinstance(c, int) for c in counts), counts)
        self.assertEqual(counts, sorted(counts, reverse=True))

    def test_rename_uses_the_sanitized_name_downstream(self):
        with db_connections():
            ops, sym = compile({
                "from": {"data_source": SOURCE, "table": "orders"},
                "rename": [{"column": "order_status", "as": "Order State"}],
                "select": ["order_state"],
                "limit": 2,
            })
            result = execute_transient(ops, page_size=2)
        self.assertEqual([c["name"] for c in result["columns"]], ["order_state"])
        self.assertEqual(list(sym.names), ["order_state"])
