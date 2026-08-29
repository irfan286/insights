# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""Golden tests for the chart render port.

There are two implementations of "chart config -> data_query operations": the original in
`frontend/src2/charts/chart.ts` and the Python port in `chart_operations.py`. They will
drift unless something pins them. These are that pin -- every expected list below was read
off the TypeScript, not off the Python.

Pure unit tests: no database, no ibis, no data source. The doc-state half of the contract
(`use_live_connection` propagation) cannot be asserted here and lives in
`test_chart_render.py`.
"""

import frappe
from frappe.tests import UnitTestCase

from insights.insights.doctype.insights_chart_v3.chart_operations import (
    build_data_query_operations,
    count_measure,
    normalize_config,
    validate_config,
)

QUERY = "qry-source"

SOURCE_OP = {
    "type": "source",
    "table": {"type": "query", "workbook": "", "query_name": QUERY},
}


def dim(column_name, data_type="String", granularity=None, dimension_name=None):
    dimension = {
        "column_name": column_name,
        "data_type": data_type,
        "dimension_name": dimension_name or column_name,
    }
    if granularity:
        dimension["granularity"] = granularity
    return dimension


def measure(column_name, aggregation="sum", data_type="Decimal", measure_name=None):
    return {
        "measure_name": measure_name or f"{aggregation}_of_{column_name}",
        "column_name": column_name,
        "data_type": data_type,
        "aggregation": aggregation,
    }


def chart(chart_type, config, query=QUERY):
    return frappe._dict(chart_type=chart_type, config=config, query=query)


def build(chart_type, config, query=QUERY):
    return build_data_query_operations(chart(chart_type, config, query))


class TestAxisCharts(UnitTestCase):
    def base_config(self):
        return {
            "x_axis": {"dimension": dim("order_date", "Date", "month")},
            "y_axis": {"series": [{"measure": measure("price")}]},
        }

    def test_bar_chart_golden(self):
        ops = build("Bar", self.base_config())
        self.assertEqual(
            ops,
            [
                SOURCE_OP,
                {
                    "type": "summarize",
                    "measures": [measure("price")],
                    "dimensions": [dim("order_date", "Date", "month")],
                },
            ],
        )

    def test_line_and_row_take_the_same_branch(self):
        for chart_type in ("Line", "Row"):
            with self.subTest(chart_type=chart_type):
                self.assertEqual(build(chart_type, self.base_config()), build("Bar", self.base_config()))

    def test_no_measure_falls_back_to_count(self):
        """chart.ts:250 -- an empty series is a count, not an error."""
        config = self.base_config()
        config["y_axis"] = {"series": []}
        ops = build("Bar", config)
        self.assertEqual(ops[1]["measures"], [count_measure()])

    def test_a_series_without_a_measure_name_is_dropped(self):
        config = self.base_config()
        config["y_axis"]["series"].append({"measure": {"column_name": "qty", "measure_name": ""}})
        ops = build("Bar", config)
        self.assertEqual(ops[1]["measures"], [measure("price")])

    def test_split_by_switches_to_pivot_wider(self):
        config = self.base_config()
        config["split_by"] = {"dimension": dim("region")}
        ops = build("Bar", config)
        self.assertEqual(
            ops[1],
            {
                "type": "pivot_wider",
                "rows": [dim("order_date", "Date", "month")],
                "columns": [dim("region")],
                "values": [measure("price")],
                "max_column_values": 10,
            },
        )

    def test_max_split_values_is_honoured(self):
        config = self.base_config()
        config["split_by"] = {"dimension": dim("region"), "max_split_values": 5}
        self.assertEqual(build("Bar", config)[1]["max_column_values"], 5)


class TestNonAxisCharts(UnitTestCase):
    def test_number_without_a_date_column_has_no_dimensions(self):
        ops = build("Number", {"number_columns": [measure("price")]})
        self.assertEqual(
            ops,
            [
                SOURCE_OP,
                {"type": "summarize", "measures": [measure("price")], "dimensions": []},
            ],
        )

    def test_number_with_a_date_column(self):
        ops = build(
            "Number",
            {
                "number_columns": [measure("price")],
                "date_column": dim("order_date", "Date", "month"),
            },
        )
        self.assertEqual(ops[1]["dimensions"], [dim("order_date", "Date", "month")])

    def test_donut_appends_a_descending_sort_on_its_value(self):
        ops = build(
            "Donut",
            {"label_column": dim("region"), "value_column": measure("price")},
        )
        self.assertEqual(
            ops,
            [
                SOURCE_OP,
                {
                    "type": "summarize",
                    "measures": [measure("price")],
                    "dimensions": [dim("region")],
                },
                {
                    "type": "order_by",
                    "column": {"type": "column", "column_name": "sum_of_price"},
                    "direction": "desc",
                },
            ],
        )

    def test_funnel_shares_the_donut_branch(self):
        config = {"label_column": dim("stage"), "value_column": measure("count", "count", "Integer")}
        self.assertEqual(
            [op["type"] for op in build("Funnel", config)],
            ["source", "summarize", "order_by"],
        )

    def test_a_user_sort_on_the_same_column_replaces_the_implicit_one(self):
        """The implicit desc is emitted first precisely so this replacement happens."""
        ops = build(
            "Donut",
            {
                "label_column": dim("region"),
                "value_column": measure("price"),
                "order_by": [
                    {"column": {"type": "column", "column_name": "sum_of_price"}, "direction": "asc"}
                ],
            },
        )
        order_bys = [op for op in ops if op["type"] == "order_by"]
        self.assertEqual(len(order_bys), 1)
        self.assertEqual(order_bys[0]["direction"], "asc")

    def test_map_chart(self):
        ops = build(
            "Map",
            {"location_column": dim("country"), "value_column": measure("price")},
        )
        self.assertEqual(
            ops[1],
            {
                "type": "summarize",
                "measures": [measure("price")],
                "dimensions": [dim("country")],
            },
        )

    def test_bubble_keeps_the_x_y_size_measure_order(self):
        ops = build(
            "Bubble",
            {
                "xAxis": measure("price"),
                "yAxis": measure("qty"),
                "size_column": measure("weight"),
                "dimension": dim("product"),
                "quadrant_column": dim("region"),
            },
        )
        self.assertEqual(
            ops[1]["measures"],
            [measure("price"), measure("qty"), measure("weight")],
        )
        self.assertEqual(ops[1]["dimensions"], [dim("product"), dim("region")])

    def test_bubble_drops_the_optional_columns_when_unset(self):
        ops = build("Bubble", {"xAxis": measure("price"), "yAxis": measure("qty")})
        self.assertEqual(ops[1]["measures"], [measure("price"), measure("qty")])
        self.assertEqual(ops[1]["dimensions"], [])

    def test_sankey_emits_no_chart_operation(self):
        """addChartOperation has no Sankey branch -- its renderer aggregates client-side."""
        ops = build(
            "Sankey",
            {
                "source_column": dim("from_state"),
                "target_column": dim("to_state"),
                "value_column": measure("amount"),
            },
        )
        self.assertEqual(ops, [SOURCE_OP])


class TestTableChart(UnitTestCase):
    def test_columns_present_pivots(self):
        ops = build(
            "Table",
            {
                "rows": [dim("region")],
                "columns": [dim("year")],
                "values": [measure("price")],
            },
        )
        self.assertEqual(
            ops[1],
            {
                "type": "pivot_wider",
                "rows": [dim("region")],
                "columns": [dim("year")],
                "values": [measure("price")],
                "max_column_values": 10,
            },
        )

    def test_show_raw_rows_selects_instead_of_grouping(self):
        ops = build(
            "Table",
            {
                "rows": [dim("region"), dim("city", dimension_name="City")],
                "columns": [],
                "values": [],
                "show_raw_rows": True,
            },
        )
        self.assertEqual(
            ops[1:],
            [
                {"type": "select", "column_names": ["region", "city"]},
                {
                    "type": "rename",
                    "column": {"type": "column", "column_name": "city"},
                    "new_name": "City",
                },
            ],
        )

    def test_without_show_raw_rows_it_still_groups(self):
        ops = build("Table", {"rows": [dim("region")], "columns": [], "values": []})
        self.assertEqual(
            ops[1], {"type": "summarize", "measures": [], "dimensions": [dim("region")]}
        )

    def test_values_present_summarizes(self):
        ops = build(
            "Table",
            {
                "rows": [dim("region")],
                "columns": [],
                "values": [measure("price")],
                "show_raw_rows": True,
            },
        )
        self.assertEqual(ops[1]["type"], "summarize")


class TestPipelineInvariants(UnitTestCase):
    def config(self):
        return {
            "x_axis": {"dimension": dim("region")},
            "y_axis": {"series": [{"measure": measure("price")}]},
        }

    def test_no_chart_ever_emits_a_limit_operation(self):
        """The chart limit is page_size at execute time (chart.ts:91), not an operation."""
        cases = [
            ("Bar", self.config()),
            ("Number", {"number_columns": [measure("price")]}),
            ("Donut", {"label_column": dim("region"), "value_column": measure("price")}),
            ("Funnel", {"label_column": dim("region"), "value_column": measure("price")}),
            ("Table", {"rows": [dim("region")], "columns": [], "values": []}),
            ("Map", {"location_column": dim("country"), "value_column": measure("price")}),
            ("Bubble", {"xAxis": measure("price"), "yAxis": measure("qty")}),
            (
                "Sankey",
                {
                    "source_column": dim("a"),
                    "target_column": dim("b"),
                    "value_column": measure("amount"),
                },
            ),
        ]
        for chart_type, config in cases:
            with self.subTest(chart_type=chart_type):
                config = dict(config, limit=500)
                types = [op["type"] for op in build(chart_type, config)]
                self.assertNotIn("limit", types)

    def test_empty_filters_emit_no_filter_group(self):
        types = [op["type"] for op in build("Bar", self.config())]
        self.assertNotIn("filter_group", types)

    def test_filters_are_emitted_between_source_and_the_chart_operation(self):
        config = self.config()
        config["filters"] = {
            "logical_operator": "And",
            "filters": [
                {
                    "column": {"type": "column", "column_name": "status"},
                    "operator": "=",
                    "value": "Open",
                }
            ],
        }
        ops = build("Bar", config)
        self.assertEqual([op["type"] for op in ops], ["source", "filter_group", "summarize"])
        self.assertEqual(ops[1]["logical_operator"], "And")

    def test_order_by_is_appended_last(self):
        config = self.config()
        config["order_by"] = [
            {"column": {"type": "column", "column_name": "region"}, "direction": "asc"}
        ]
        ops = build("Bar", config)
        self.assertEqual([op["type"] for op in ops], ["source", "summarize", "order_by"])

    def test_order_by_entries_without_a_direction_are_skipped(self):
        config = self.config()
        config["order_by"] = [
            {"column": {"type": "column", "column_name": "region"}, "direction": ""},
            {"column": {"type": "column", "column_name": ""}, "direction": "asc"},
        ]
        self.assertNotIn("order_by", [op["type"] for op in build("Bar", config)])

    def test_a_duplicate_sort_is_not_emitted_twice(self):
        config = self.config()
        config["order_by"] = [
            {"column": {"type": "column", "column_name": "region"}, "direction": "asc"},
            {"column": {"type": "column", "column_name": "region"}, "direction": "asc"},
        ]
        ops = [op for op in build("Bar", config) if op["type"] == "order_by"]
        self.assertEqual(len(ops), 1)

    def test_the_source_is_always_a_query_reference(self):
        ops = build("Bar", self.config())
        self.assertEqual(ops[0]["table"]["type"], "query")
        self.assertEqual(ops[0]["table"]["workbook"], "")

    def test_an_incomplete_config_raises_rather_than_building_nonsense(self):
        with self.assertRaises(frappe.ValidationError):
            build("Bar", {"y_axis": {"series": []}})


class TestNormalizeConfig(UnitTestCase):
    """Every default `transformChartDoc` (chart.ts:567-603) injects on load."""

    def test_defaults(self):
        config = normalize_config("Bar", {})
        self.assertEqual(config["filters"], {"filters": [], "logical_operator": "And"})
        self.assertEqual(config["order_by"], [])
        self.assertEqual(config["limit"], 100)

    def test_funnel_and_donut_position_defaults(self):
        self.assertEqual(normalize_config("Funnel", {})["label_position"], "left")
        self.assertEqual(normalize_config("Donut", {})["legend_position"], "bottom")
        self.assertNotIn("legend_position", normalize_config("Funnel", {}))

    def test_existing_values_are_not_overwritten(self):
        config = normalize_config("Donut", {"limit": 20, "legend_position": "top"})
        self.assertEqual(config["limit"], 20)
        self.assertEqual(config["legend_position"], "top")

    def test_a_json_string_config_is_parsed(self):
        self.assertEqual(normalize_config("Bar", '{"limit": 7}')["limit"], 7)

    def test_old_flat_x_axis_is_wrapped(self):
        config = normalize_config("Bar", {"x_axis": {"column_name": "region"}})
        self.assertEqual(config["x_axis"]["dimension"]["column_name"], "region")

    def test_old_list_y_axis_is_wrapped_into_series(self):
        config = normalize_config("Bar", {"y_axis": [measure("price")]})
        # `stack` rides along on a Bar -- test_bar_stack_defaults_to_true covers it
        self.assertEqual(
            config["y_axis"],
            {"series": [{"measure": measure("price")}], "stack": True},
        )

    def test_bar_stack_defaults_to_true(self):
        """The default `transformChartDoc` sets on load, rather than the form on mount."""
        self.assertIs(normalize_config("Bar", {})["y_axis"]["stack"], True)

    def test_an_explicit_stack_is_kept(self):
        """Absence carries the default, so a config that said False keeps saying it."""
        self.assertIs(normalize_config("Bar", {"y_axis": {"stack": False}})["y_axis"]["stack"], False)

    def test_only_a_bar_gets_the_stack_default(self):
        self.assertNotIn("stack", normalize_config("Line", {})["y_axis"])

    def test_axis_chart_config_slots_are_seeded(self):
        """`ensureConfigSlots`: the empty containers a config form binds to."""
        config = normalize_config("Line", {})
        self.assertEqual(config["x_axis"], {"dimension": {}})
        self.assertEqual(config["y_axis"], {"series": []})

    def test_map_config_slots_are_seeded(self):
        config = normalize_config("Map", {})
        self.assertEqual(config["location_column"], {})
        self.assertEqual(config["value_column"], {})

    def test_slots_are_not_seeded_for_other_chart_types(self):
        self.assertNotIn("x_axis", normalize_config("Donut", {}))

    def test_dimension_names_are_backfilled(self):
        config = normalize_config(
            "Table",
            {
                "rows": [{"column_name": "region"}],
                "columns": [{"column_name": "year"}],
                "date_column": {"column_name": "d"},
                "label_column": {"column_name": "l"},
                "x_axis": {"dimension": {"column_name": "x"}},
                "split_by": {"dimension": {"column_name": "s"}},
            },
        )
        self.assertEqual(config["rows"][0]["dimension_name"], "region")
        self.assertEqual(config["columns"][0]["dimension_name"], "year")
        self.assertEqual(config["date_column"]["dimension_name"], "d")
        self.assertEqual(config["label_column"]["dimension_name"], "l")
        self.assertEqual(config["x_axis"]["dimension"]["dimension_name"], "x")
        self.assertEqual(config["split_by"]["dimension"]["dimension_name"], "s")

    def test_the_columns_setDimensionNames_skips_stay_skipped(self):
        """Mirroring the omission matters: a config we 'improve' stops round-tripping."""
        config = normalize_config(
            "Bubble",
            {
                "location_column": {"column_name": "country"},
                "dimension": {"column_name": "product"},
                "quadrant_column": {"column_name": "region"},
                "source_column": {"column_name": "a"},
            },
        )
        for key in ("location_column", "dimension", "quadrant_column", "source_column"):
            self.assertNotIn("dimension_name", config[key], key)

    def test_the_input_config_is_not_mutated(self):
        original = {"rows": [{"column_name": "region"}]}
        normalize_config("Table", original)
        self.assertNotIn("dimension_name", original["rows"][0])
        self.assertNotIn("limit", original)


class TestValidateConfig(UnitTestCase):
    def test_missing_query_and_chart_type(self):
        messages = validate_config("", {}, query=None)
        self.assertIn("Query is required", messages)
        self.assertIn("Chart type is required", messages)

    def test_unknown_chart_type(self):
        self.assertIn("Invalid chart type: pie", validate_config("pie", {}, query=QUERY))

    def test_axis_requires_an_x_axis(self):
        self.assertIn("X-axis is required", validate_config("Bar", {}, query=QUERY))

    def test_x_axis_and_split_by_cannot_match(self):
        config = {
            "x_axis": {"dimension": dim("region")},
            "split_by": {"dimension": dim("region")},
        }
        self.assertIn(
            "X-axis and Split by cannot be the same", validate_config("Bar", config, query=QUERY)
        )

    def test_per_type_requirements(self):
        cases = {
            "Number": "Number column is required",
            "Donut": "Label column is required",
            "Funnel": "Label column is required",
            "Table": "Rows are required",
            "Map": "Location column is required",
            "Bubble": "X-axis is required",
            "Sankey": "Source column is required",
        }
        for chart_type, message in cases.items():
            with self.subTest(chart_type=chart_type):
                self.assertIn(message, validate_config(chart_type, {}, query=QUERY))

    def test_a_complete_config_has_no_messages(self):
        config = {
            "x_axis": {"dimension": dim("region")},
            "y_axis": {"series": [{"measure": measure("price")}]},
        }
        self.assertEqual(validate_config("Bar", config, query=QUERY), [])
