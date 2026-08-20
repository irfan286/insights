# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""Golden tests for the QuerySpec compiler.

Pure: `StaticSchemaResolver` means no data source, no ibis, no DB round trip. The
companion `test_compiler_integration.py` feeds the same operations through the real
backend -- that is what catches the compiler drifting from `ibis_utils.py`, which
golden tests alone cannot do.

Every assertion about a literal string here (casing, key names, "asc") corresponds to a
measured backend behaviour recorded in docs/mcp-IMPLEMENTATION.md §8. Do not "tidy" one
without reading it: `direction` anything-but-"asc" means DESCENDING, `logical_operator`
is Title-Case, and a missing `limit` silently becomes LIMIT 1.
"""

from frappe.tests import UnitTestCase

from insights.mcp.compiler import StaticSchemaResolver, compile, decompile
from insights.mcp.errors import ToolError

RESOLVER = StaticSchemaResolver(
    {
        ("demo_data", "orders"): [
            ("order_id", "String"),
            ("customer_id", "String"),
            ("order_status", "String"),
            ("order_purchase_timestamp", "Datetime"),
            ("order_date_str", "String"),
            ("order_time", "Time"),
            ("qty", "Integer"),
            ("payload", "JSON"),
        ],
        ("demo_data", "orderitems"): [
            ("order_id", "String"),
            ("order_status", "String"),
            ("price", "Decimal"),
        ],
        ("other_source", "things"): [("id", "String")],
    }
)


def compile_spec(spec):
    return compile(spec, resolver=RESOLVER)


class TestCompilerGolden(UnitTestCase):
    def test_flagship_example_from_design_6_4(self):
        """The whole point of the DSL, compiled operation-for-operation."""
        ops, sym = compile_spec(
            {
                "from": {"data_source": "demo_data", "table": "orders"},
                "joins": [
                    {"table": "orderitems", "left_on": "order_id",
                     "right_on": "order_id", "select": ["price"]}
                ],
                "where": [
                    {"column": "order_status", "op": "not_in", "value": ["delivered"]},
                    {"column": "order_id", "op": "is_set"},
                ],
                "group_by": [
                    {"column": "order_purchase_timestamp", "granularity": "month",
                     "as": "order_month"}
                ],
                "aggregate": [{"column": "price", "fn": "sum"}, {"fn": "count"}],
                "having": [{"column": "count_of_rows", "op": ">", "value": 10}],
                "sort": [{"column": "order_month"}],
                "limit": 500,
            }
        )

        self.assertEqual(
            ops,
            [
                {"type": "source",
                 "table": {"type": "table", "data_source": "demo_data", "table_name": "orders"}},
                {"type": "join", "join_type": "left",
                 "table": {"type": "table", "data_source": "demo_data", "table_name": "orderitems"},
                 "select_columns": [{"type": "column", "column_name": "price"}],
                 "join_condition": {
                     "left_column": {"type": "column", "column_name": "order_id"},
                     "right_column": {"type": "column", "column_name": "order_id"}}},
                {"type": "filter_group", "logical_operator": "And", "filters": [
                    {"column": {"type": "column", "column_name": "order_status"},
                     "operator": "not_in", "value": ["delivered"]},
                    {"column": {"type": "column", "column_name": "order_id"},
                     "operator": "is_set", "value": None}]},
                {"type": "summarize",
                 "measures": [
                     {"measure_name": "sum_of_price", "column_name": "price",
                      "aggregation": "sum", "data_type": "Decimal"},
                     {"measure_name": "count_of_rows", "column_name": "count",
                      "aggregation": "count", "data_type": "Integer"}],
                 "dimensions": [
                     {"dimension_name": "order_month",
                      "column_name": "order_purchase_timestamp",
                      "data_type": "Datetime", "granularity": "month"}]},
                {"type": "filter_group", "logical_operator": "And", "filters": [
                    {"column": {"type": "column", "column_name": "count_of_rows"},
                     "operator": ">", "value": 10}]},
                {"type": "order_by",
                 "column": {"type": "column", "column_name": "order_month"},
                 "direction": "asc"},
                {"type": "limit", "limit": 500},
            ],
        )
        self.assertEqual(sym.names, ("order_month", "sum_of_price", "count_of_rows"))
        self.assertEqual(sym.spec_paths[3], "aggregate")


class TestAutoCast(UnitTestCase):
    """§6.3, all four branches."""

    def _dims(self, column, granularity):
        return {
            "from": {"data_source": "demo_data", "table": "orders"},
            "group_by": [{"column": column, "granularity": granularity}],
            "aggregate": [{"fn": "count"}],
        }

    def test_string_column_with_granularity_gets_a_cast(self):
        ops, _ = compile_spec(self._dims("order_date_str", "month"))
        casts = [o for o in ops if o["type"] == "cast"]
        self.assertEqual(len(casts), 1)
        self.assertEqual(casts[0]["data_type"], "Datetime")
        self.assertEqual(casts[0]["column"]["column_name"], "order_date_str")
        # the cast must precede the summarize
        self.assertLess(ops.index(casts[0]), [o["type"] for o in ops].index("summarize"))

    def test_datetime_column_gets_no_cast(self):
        ops, _ = compile_spec(self._dims("order_purchase_timestamp", "month"))
        self.assertEqual([o for o in ops if o["type"] == "cast"], [])

    def test_numeric_column_with_granularity_is_rejected(self):
        with self.assertRaises(ToolError) as ctx:
            compile_spec(self._dims("qty", "month"))
        self.assertIn("qty", str(ctx.exception))
        self.assertIn("Integer", str(ctx.exception))

    def test_string_dimension_without_granularity_emits_none(self):
        ops, _ = compile_spec(
            {
                "from": {"data_source": "demo_data", "table": "orders"},
                "group_by": [{"column": "order_status"}],
                "aggregate": [{"fn": "count"}],
            }
        )
        summarize = next(o for o in ops if o["type"] == "summarize")
        self.assertNotIn("granularity", summarize["dimensions"][0])
        self.assertEqual([o for o in ops if o["type"] == "cast"], [])


class TestGranularity(UnitTestCase):
    def test_time_column_rejects_a_calendar_granularity(self):
        with self.assertRaises(ToolError) as ctx:
            compile_spec({
                "from": {"data_source": "demo_data", "table": "orders"},
                "group_by": [{"column": "order_time", "granularity": "month"}],
                "aggregate": [{"fn": "count"}],
            })
        self.assertIn("Time columns", str(ctx.exception))

    def test_time_column_accepts_hour(self):
        ops, _ = compile_spec({
            "from": {"data_source": "demo_data", "table": "orders"},
            "group_by": [{"column": "order_time", "granularity": "hour"}],
            "aggregate": [{"fn": "count"}],
        })
        dim = next(o for o in ops if o["type"] == "summarize")["dimensions"][0]
        self.assertEqual(dim["granularity"], "hour")

    def test_wrong_casing_is_rejected_not_corrected(self):
        """§6.3 validation rule 4 -- the model must learn, not be silently fixed."""
        with self.assertRaises(ToolError) as ctx:
            compile_spec({
                "from": {"data_source": "demo_data", "table": "orders"},
                "group_by": [{"column": "order_purchase_timestamp", "granularity": "Month"}],
                "aggregate": [{"fn": "count"}],
            })
        self.assertIn("Casing matters", str(ctx.exception))


class TestSymbolTable(UnitTestCase):
    def test_having_against_a_source_column_is_rejected_with_the_alias_list(self):
        with self.assertRaises(ToolError) as ctx:
            compile_spec({
                "from": {"data_source": "demo_data", "table": "orders"},
                "group_by": [{"column": "order_status"}],
                "aggregate": [{"fn": "count"}],
                "having": [{"column": "order_id", "op": "is_set"}],
            })
        message = str(ctx.exception)
        self.assertIn("summarize", message)
        self.assertIn("count_of_rows", message)  # the valid alias list

    def test_sort_after_summarize_is_checked_against_output_aliases(self):
        with self.assertRaises(ToolError) as ctx:
            compile_spec({
                "from": {"data_source": "demo_data", "table": "orders"},
                "group_by": [{"column": "order_status"}],
                "aggregate": [{"column": "qty", "fn": "sum"}],
                "sort": [{"column": "qty"}],
            })
        self.assertIn("sum_of_qty", str(ctx.exception))

    def test_having_without_aggregate_is_rejected(self):
        with self.assertRaises(ToolError) as ctx:
            compile_spec({
                "from": {"data_source": "demo_data", "table": "orders"},
                "having": [{"column": "order_id", "op": "is_set"}],
            })
        self.assertIn("aggregate", str(ctx.exception))

    def test_symbol_table_round_trips_for_phase_2(self):
        _, sym = compile_spec({
            "from": {"data_source": "demo_data", "table": "orders"},
            "group_by": [{"column": "order_status"}],
            "aggregate": [{"column": "qty", "fn": "sum"}],
        })
        from insights.mcp.compiler import SymbolTable

        rehydrated = SymbolTable.from_json(sym.to_json())
        self.assertEqual(rehydrated.names, sym.names)
        self.assertEqual(
            [c.role for c in rehydrated.columns], ["dimension", "measure"]
        )


class TestJoins(UnitTestCase):
    def test_bare_join_gets_a_trailing_select_that_drops_the_junk_column(self):
        """See §8 C -- measured: the backend force-adds `orderitems_order_id`."""
        ops, sym = compile_spec({
            "from": {"data_source": "demo_data", "table": "orders"},
            "joins": [{"table": "orderitems", "left_on": "order_id",
                       "right_on": "order_id", "select": ["price"]}],
        })
        self.assertEqual(ops[-1]["type"], "select")
        self.assertIn("price", ops[-1]["column_names"])
        self.assertNotIn("orderitems_order_id", ops[-1]["column_names"])
        self.assertNotIn("orderitems_order_id", sym.names)

    def test_summarize_suppresses_the_trailing_select(self):
        ops, _ = compile_spec({
            "from": {"data_source": "demo_data", "table": "orders"},
            "joins": [{"table": "orderitems", "left_on": "order_id",
                       "right_on": "order_id", "select": ["price"]}],
            "group_by": [{"column": "order_status"}],
            "aggregate": [{"column": "price", "fn": "sum"}],
        })
        self.assertEqual([o for o in ops if o["type"] == "select"], [])

    def test_non_key_collision_is_rejected_at_compile_time(self):
        with self.assertRaises(ToolError) as ctx:
            compile_spec({
                "from": {"data_source": "demo_data", "table": "orders"},
                "joins": [{"table": "orderitems", "left_on": "order_id",
                           "right_on": "order_id", "select": ["order_status", "price"]}],
            })
        self.assertIn("order_status", str(ctx.exception))

    def test_cross_data_source_join_is_rejected_up_front(self):
        """It would otherwise fail at EXECUTE time with an Indonesian message (§8 N)."""
        with self.assertRaises(ToolError) as ctx:
            compile_spec({
                "from": {"data_source": "demo_data", "table": "orders"},
                "joins": [{"table": "things", "data_source": "other_source",
                           "left_on": "order_id", "right_on": "id"}],
            })
        self.assertIn("data source", str(ctx.exception).lower())


class TestFilters(UnitTestCase):
    def _where(self, rule):
        return compile_spec({
            "from": {"data_source": "demo_data", "table": "orders"}, "where": [rule]
        })[0][-1]["filters"][0]

    def test_spec_key_op_becomes_operation_key_operator(self):
        rule = self._where({"column": "order_id", "op": "=", "value": "x"})
        self.assertEqual(rule["operator"], "=")
        self.assertNotIn("op", rule)

    def test_comparison_operator_without_a_value_is_rejected(self):
        with self.assertRaises(ToolError) as ctx:
            self._where({"column": "order_id", "op": "="})
        self.assertIn("is_set", str(ctx.exception))  # points at the valueless operators

    def test_valueless_operator_emits_null_value(self):
        """The backend still reads `value` and resolves a dict as a column ref."""
        self.assertIsNone(self._where({"column": "order_id", "op": "is_set", "value": {"x": 1}})["value"])

    def test_in_without_a_list_is_rejected(self):
        with self.assertRaises(ToolError):
            self._where({"column": "order_status", "op": "in", "value": "delivered"})

    def test_between_needs_exactly_two_values(self):
        with self.assertRaises(ToolError):
            self._where({"column": "qty", "op": "between", "value": [1]})

    def test_contains_rejects_a_non_string(self):
        with self.assertRaises(ToolError):
            self._where({"column": "order_status", "op": "contains", "value": 5})

    def test_where_and_where_any_emit_two_groups(self):
        ops, _ = compile_spec({
            "from": {"data_source": "demo_data", "table": "orders"},
            "where": [{"column": "order_id", "op": "is_set"}],
            "where_any": [{"column": "order_status", "op": "=", "value": "a"},
                          {"column": "order_status", "op": "=", "value": "b"}],
        })
        groups = [o for o in ops if o["type"] == "filter_group"]
        self.assertEqual([g["logical_operator"] for g in groups], ["And", "Or"])


class TestAggregates(UnitTestCase):
    def test_count_with_no_column_uses_the_backend_sentinel(self):
        ops, _ = compile_spec({
            "from": {"data_source": "demo_data", "table": "orders"},
            "aggregate": [{"fn": "count"}],
        })
        measure = next(o for o in ops if o["type"] == "summarize")["measures"][0]
        self.assertEqual(measure["column_name"], "count")
        self.assertEqual(measure["aggregation"], "count")
        self.assertEqual(measure["measure_name"], "count_of_rows")

    def test_non_count_aggregate_without_a_column_is_rejected(self):
        with self.assertRaises(ToolError):
            compile_spec({
                "from": {"data_source": "demo_data", "table": "orders"},
                "aggregate": [{"fn": "sum"}],
            })

    def test_duplicate_measure_names_are_rejected(self):
        """apply_summary keys aggregates by name -- duplicates silently collapse."""
        with self.assertRaises(ToolError) as ctx:
            compile_spec({
                "from": {"data_source": "demo_data", "table": "orders"},
                "aggregate": [{"column": "qty", "fn": "sum", "as": "total"},
                              {"column": "qty", "fn": "avg", "as": "total"}],
            })
        self.assertIn("total", str(ctx.exception))

    def test_json_column_cannot_be_aggregated(self):
        with self.assertRaises(ToolError):
            compile_spec({
                "from": {"data_source": "demo_data", "table": "orders"},
                "aggregate": [{"column": "payload", "fn": "max"}],
            })

    def test_column_measure_never_carries_a_null_expression_key(self):
        """`"expression" in measure` is a key-presence test (ibis_utils.py:816):
        a null expression key takes the expression branch and AttributeErrors."""
        ops, _ = compile_spec({
            "from": {"data_source": "demo_data", "table": "orders"},
            "aggregate": [{"column": "qty", "fn": "sum"}],
        })
        measure = next(o for o in ops if o["type"] == "summarize")["measures"][0]
        self.assertNotIn("expression", measure)


class TestSortAndLimit(UnitTestCase):
    def test_direction_is_exactly_asc_or_desc(self):
        ops, _ = compile_spec({
            "from": {"data_source": "demo_data", "table": "orders"},
            "sort": [{"column": "order_id"}, {"column": "qty", "desc": True}],
        })
        directions = [o["direction"] for o in ops if o["type"] == "order_by"]
        self.assertEqual(directions, ["asc", "desc"])

    def test_limit_is_clamped_not_silently_dropped(self):
        ops, _ = compile_spec({
            "from": {"data_source": "demo_data", "table": "orders"}, "limit": 99_999_999
        })
        self.assertEqual(ops[-1]["limit"], 1_000_000)

    def test_invalid_limit_is_rejected_rather_than_becoming_limit_1(self):
        for bad in ("abc", 0, -5):
            with self.assertRaises(ToolError):
                compile_spec({"from": {"data_source": "demo_data", "table": "orders"}, "limit": bad})


class TestSpecErrors(UnitTestCase):
    def test_missing_from_is_rejected(self):
        with self.assertRaises(ToolError) as ctx:
            compile_spec({})
        self.assertEqual(ctx.exception.spec_path, "from")

    def test_unknown_column_lists_the_valid_ones(self):
        with self.assertRaises(ToolError) as ctx:
            compile_spec({
                "from": {"data_source": "demo_data", "table": "orders"},
                "sort": [{"column": "nope"}],
            })
        self.assertIn("order_id", ctx.exception.valid_columns)

    def test_rename_is_sanitized_to_the_stored_form(self):
        """sanitize_name lowercases; later references must use that form."""
        ops, sym = compile_spec({
            "from": {"data_source": "demo_data", "table": "orders"},
            "rename": [{"column": "order_id", "as": "Order Ref"}],
        })
        self.assertEqual(ops[-1]["new_name"], "order_ref")
        self.assertIn("order_ref", sym.names)
        self.assertNotIn("order_id", sym.names)


class TestExpressionValidation(UnitTestCase):
    """`derive` expressions, in both safe_exec worlds.

    `validate_expression` runs the expression through `safe_exec` for its TYPE stage.
    When server scripts are disabled that stage fails for every expression including
    valid ones, so the compiler must not trust the verdict (§8 M). When they are
    enabled, real type errors must be surfaced. Both are pinned here so a bench-config
    change cannot silently turn one of them into a no-op.
    """

    BASE = {
        "from": {"data_source": "demo_data", "table": "orders"},
        "derive": [{"name": "double_qty", "data_type": "Integer", "expression": "qty * 2"}],
    }

    def test_valid_expression_compiles_to_a_mutate(self):
        ops, sym = compile_spec(self.BASE)
        mutate = next(o for o in ops if o["type"] == "mutate")
        self.assertEqual(mutate["new_name"], "double_qty")
        self.assertEqual(mutate["data_type"], "Integer")
        self.assertEqual(mutate["expression"], {"type": "expression", "expression": "qty * 2"})
        self.assertIn("double_qty", sym.names)

    def test_unknown_column_in_an_expression_is_rejected_either_way(self):
        """Name validation runs BEFORE the safe_exec stage, so it is always reliable."""
        with self.assertRaises(ToolError) as ctx:
            compile_spec({
                **self.BASE,
                "derive": [{"name": "x", "expression": "no_such_column + 1"}],
            })
        self.assertIn("no_such_column", str(ctx.exception))

    def test_derive_name_is_sanitized_like_the_backend_does(self):
        ops, sym = compile_spec({
            **self.BASE,
            "derive": [{"name": "Double Qty", "data_type": "Integer", "expression": "qty * 2"}],
        })
        self.assertEqual(next(o for o in ops if o["type"] == "mutate")["new_name"], "double_qty")
        self.assertIn("double_qty", sym.names)

    def test_type_error_is_surfaced_when_safe_exec_is_available(self):
        from frappe.utils.safe_exec import is_safe_exec_enabled

        if not is_safe_exec_enabled():
            self.skipTest("server scripts disabled; the type stage cannot run")

        with self.assertRaises(ToolError) as ctx:
            compile_spec({
                **self.BASE,
                "derive": [{"name": "bad", "expression": "qty + 'abc'"}],
            })
        self.assertIn("Type error", str(ctx.exception))

    def test_valid_expression_survives_a_disabled_safe_exec(self):
        """The §8 M fallback: without it, EVERY derive is rejected on such a bench."""
        from unittest.mock import patch

        with patch("frappe.utils.safe_exec.is_safe_exec_enabled", return_value=False), \
             patch(
                 "insights.insights.doctype.insights_data_source_v3.ibis.utils.validate_expression",
                 return_value={
                     "is_valid": False,
                     "errors": [{"line": 1, "column": 0,
                                 "message": "Error: Server Scripts are disabled. Please enable..."}],
                 },
             ):
            ops, _ = compile_spec(self.BASE)
        self.assertTrue([o for o in ops if o["type"] == "mutate"])


class TestDecompile(UnitTestCase):
    """`operations[]` -> QuerySpec, for `get_item(include_spec=true)`.

    Eager to give up on purpose: the model edits what it is handed and submits it back, so
    an approximation is a wrong query rather than a wrong reading.
    """

    def round_trip(self, spec):
        operations, _ = compile_spec(spec)
        decompiled, reason = decompile(operations)
        self.assertIsNone(reason)
        return decompiled

    def test_anything_the_compiler_emits_comes_back(self):
        spec = {
            "from": {"data_source": "demo_data", "table": "orders"},
            "where": [{"column": "order_status", "op": "not_in", "value": ["delivered"]}],
            "group_by": [
                {"column": "order_purchase_timestamp", "granularity": "month", "as": "order_month"}
            ],
            "aggregate": [{"column": "qty", "fn": "sum"}, {"fn": "count"}],
            "having": [{"column": "count_of_rows", "op": ">", "value": 10}],
            "sort": [{"column": "order_month", "desc": False}],
            "limit": 500,
        }
        self.assertEqual(self.round_trip(spec), spec)

    def test_a_recompile_of_the_decompiled_spec_is_identical(self):
        """The property that matters: what comes back must rebuild the same query."""
        spec = {
            "from": {"data_source": "demo_data", "table": "orders"},
            "group_by": [{"column": "order_date_str", "granularity": "year"}],
            "aggregate": [{"fn": "count"}],
        }
        operations, _ = compile_spec(spec)
        again, _ = compile_spec(self.round_trip(spec))
        self.assertEqual(again, operations)

    def test_the_auto_cast_is_not_echoed_back_as_an_explicit_one(self):
        """Echoing it would make the next compile emit the cast twice."""
        spec = {
            "from": {"data_source": "demo_data", "table": "orders"},
            "group_by": [{"column": "order_date_str", "granularity": "year"}],
            "aggregate": [{"fn": "count"}],
        }
        self.assertNotIn("cast", self.round_trip(spec))

    def test_a_query_source_round_trips(self):
        operations = [
            {"type": "source", "table": {"type": "query", "query_name": "q1", "workbook": ""}}
        ]
        spec, reason = decompile(operations)
        self.assertIsNone(reason)
        self.assertEqual(spec["from"], {"query": "q1"})

    def test_a_pivot_round_trips(self):
        spec = {
            "from": {"data_source": "demo_data", "table": "orders"},
            "group_by": [{"column": "order_status"}, {"column": "customer_id"}],
            "aggregate": [{"column": "qty", "fn": "sum"}],
            "pivot_on": {"column": "customer_id", "max_values": 5},
        }
        self.assertEqual(self.round_trip(spec), spec)

    def test_an_unexpressible_operation_is_named(self):
        spec, reason = decompile([
            {"type": "source", "table": {"type": "table", "data_source": "d", "table_name": "t"}},
            {"type": "union", "table": {}},
        ])
        self.assertIsNone(spec)
        self.assertIn("union", reason)

    def test_a_nested_filter_group_is_refused(self):
        spec, reason = decompile([
            {"type": "source", "table": {"type": "table", "data_source": "d", "table_name": "t"}},
            {
                "type": "filter_group",
                "logical_operator": "And",
                "filters": [{"type": "filter_group", "logical_operator": "Or", "filters": []}],
            },
        ])
        self.assertIsNone(spec)
        self.assertIn("nested filter group", reason)

    def test_two_aggregation_steps_are_refused(self):
        summarize = {"type": "summarize", "measures": [], "dimensions": []}
        spec, reason = decompile([
            {"type": "source", "table": {"type": "table", "data_source": "d", "table_name": "t"}},
            summarize,
            summarize,
        ])
        self.assertIsNone(spec)
        self.assertIn("two aggregation steps", reason)

    def test_operations_out_of_canonical_order_are_refused(self):
        spec, reason = decompile([
            {"type": "source", "table": {"type": "table", "data_source": "d", "table_name": "t"}},
            {"type": "summarize", "measures": [], "dimensions": []},
            {"type": "join", "table": {}, "join_condition": {}},
        ])
        self.assertIsNone(spec)
        self.assertIn("order", reason)

    def test_an_expression_measure_is_refused(self):
        spec, reason = decompile([
            {"type": "source", "table": {"type": "table", "data_source": "d", "table_name": "t"}},
            {
                "type": "summarize",
                "dimensions": [],
                "measures": [
                    {
                        "measure_name": "margin",
                        "expression": {"type": "expression", "expression": "a - b"},
                        "data_type": "Decimal",
                    }
                ],
            },
        ])
        self.assertIsNone(spec)
        self.assertIn("expression measure", reason)

    def test_a_json_string_is_accepted(self):
        """`Insights Query v3.operations` is a JSON field, so it arrives as a string."""
        import json as _json

        spec, reason = decompile(
            _json.dumps([
                {"type": "source", "table": {"type": "query", "query_name": "q1", "workbook": ""}}
            ])
        )
        self.assertIsNone(reason)
        self.assertEqual(spec["from"], {"query": "q1"})

    def test_malformed_json_is_reported_not_raised(self):
        spec, reason = decompile("{not json")
        self.assertIsNone(spec)
        self.assertIn("not valid JSON", reason)

    def test_an_empty_operation_list_is_reported_not_crashed(self):
        spec, reason = decompile([])
        self.assertIsNone(spec)
        self.assertIn("no operations", reason)
