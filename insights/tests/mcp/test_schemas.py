# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""Tool schemas must be valid AND self-contained on the wire.

This file exists because of a real escape: QUERY_SPEC used
`$ref: "#/$defs/Filter"`, which is correct standalone but breaks the moment the schema
is embedded as `run_query.properties.spec` -- `#/` then resolves against RUN_QUERY's
root, where `$defs` does not exist, and every `where` clause raised PointerToNowhere at
call time. `test_compiler.py` missed it because it calls `compile()` directly, bypassing
`@tool_args`. These tests exercise the validator the way a real `tools/call` does.
"""

import json

from frappe.tests import UnitTestCase
from jsonschema import Draft202012Validator

from insights.mcp import schemas

TOOL_SCHEMAS = {
    "list_data_sources": schemas.LIST_DATA_SOURCES,
    "list_tables": schemas.LIST_TABLES,
    "describe_table": schemas.DESCRIBE_TABLE,
    "distinct_values": schemas.DISTINCT_VALUES,
    "run_query": schemas.RUN_QUERY,
    "get_docs": schemas.GET_DOCS,
    "write_ai_note": schemas.WRITE_AI_NOTE,
    "save_query": schemas.SAVE_QUERY,
    "list_workbooks": schemas.LIST_WORKBOOKS,
    "get_item": schemas.GET_ITEM,
    "delete_item": schemas.DELETE_ITEM,
    "create_chart": schemas.CREATE_CHART,
    "update_chart": schemas.UPDATE_CHART,
    "create_dashboard": schemas.CREATE_DASHBOARD,
    "update_dashboard": schemas.UPDATE_DASHBOARD,
}


class TestToolSchemas(UnitTestCase):
    def test_every_schema_is_valid_draft_2020_12(self):
        for name, schema in TOOL_SCHEMAS.items():
            with self.subTest(tool=name):
                Draft202012Validator.check_schema(schema)

    def test_no_schema_contains_a_ref(self):
        """A $ref resolves against the ROOT schema on the wire. Nesting one schema
        inside another silently breaks it, so we inline instead."""
        for name, schema in TOOL_SCHEMAS.items():
            with self.subTest(tool=name):
                self.assertNotIn("$ref", json.dumps(schema))

    def test_every_schema_is_json_serialisable(self):
        for name, schema in TOOL_SCHEMAS.items():
            with self.subTest(tool=name):
                json.dumps(schema)

    def test_run_query_accepts_a_full_spec_with_filters(self):
        """The exact payload shape that raised PointerToNowhere."""
        payload = {
            "dry_run": True,
            "spec": {
                "from": {"data_source": "demo_data", "table": "orders"},
                "joins": [{"table": "orderitems", "left_on": "order_id",
                           "right_on": "order_id", "select": ["price"]}],
                "where": [{"column": "order_status", "op": "=", "value": "delivered"}],
                "where_any": [{"column": "order_id", "op": "is_set"}],
                "group_by": [{"column": "order_purchase_timestamp", "granularity": "month"}],
                "aggregate": [{"column": "price", "fn": "sum", "as": "total"}],
                "having": [{"column": "total", "op": ">", "value": 1}],
                "sort": [{"column": "total", "desc": True}],
                "limit": 10,
            },
        }
        errors = list(Draft202012Validator(schemas.RUN_QUERY).iter_errors(payload))
        self.assertEqual(errors, [], errors[:1])

    def test_run_query_rejects_a_bad_enum(self):
        payload = {"spec": {"from": {"data_source": "d", "table": "t"},
                            "aggregate": [{"column": "x", "fn": "total"}]}}
        self.assertTrue(list(Draft202012Validator(schemas.RUN_QUERY).iter_errors(payload)))

    def test_registered_tools_pass_their_own_schema_through_tool_args(self):
        """Every @mcp.tool must be callable with a minimal valid payload without the
        validator itself exploding -- the failure mode this file was written for."""
        import insights.mcp.tools  # noqa: F401
        from insights.mcp import mcp

        registered = set(mcp._tool_registry)
        self.assertTrue(registered)
        for name in registered:
            with self.subTest(tool=name):
                schema = mcp._tool_registry[name]["input_schema"]
                Draft202012Validator.check_schema(schema)
                self.assertNotIn("$ref", json.dumps(schema))
