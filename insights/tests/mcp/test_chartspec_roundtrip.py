# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""ChartSpec -> config -> ChartSpec, and config -> the UI's normalization -> no diff.

Two properties are being pinned here and they are not the same one:

1. **Fixed point.** A config `chartspec.resolve` produces must be unchanged by
   `normalize_config` -- our port of `transformChartDoc` (`chart.ts:567-603`). If it is
   not, the first human to open an MCP-written chart silently rewrites it and the two
   authoring paths stop comparing equal. This property is proved against our own port, so
   on its own it is circular: it says the port agrees with itself.

2. **The port agrees with the TypeScript.** `NORMALIZATION_RULES` and the two
   `*_DIMENSION_NAME_TARGETS` lists below are a SECOND, independent transcription of
   `chart.ts:567-603` and `charts/helpers.ts:1349-1381`, written in a different shape from
   the port. Two transcriptions of the same 40 lines disagreeing is a real signal. It is
   not a proof of parity -- `test_chart_ts_drift.py` covers the case where the TypeScript
   itself moves -- but it breaks the circularity in (1).
"""

import json
from typing import ClassVar

from frappe.tests import UnitTestCase

from insights.insights.doctype.insights_chart_v3.chart_operations import (
    CHARTS,
    normalize_config,
)
from insights.mcp import chartspec, schemas
from insights.mcp.compiler import SymbolTable
from insights.mcp.errors import ToolError

COLUMNS = [
    {"name": "order_date", "data_type": "Date"},
    {"name": "clock_in", "data_type": "Time"},
    {"name": "region", "data_type": "String"},
    {"name": "country", "data_type": "String"},
    {"name": "stage", "data_type": "String"},
    {"name": "product", "data_type": "String"},
    {"name": "price", "data_type": "Decimal"},
    {"name": "qty", "data_type": "Integer"},
    {"name": "weight", "data_type": "Decimal"},
    {"name": "sum_of_amount", "data_type": "Decimal", "role": "measure"},
]

SYMBOLS = SymbolTable.from_json(COLUMNS)

SPECS = {
    "Bar": {
        "chart_type": "Bar",
        "x": {"column": "order_date", "granularity": "month"},
        "y": [{"column": "price", "fn": "sum"}],
    },
    "Line": {
        "chart_type": "Line",
        "x": {"column": "order_date"},
        "y": [{"column": "qty", "fn": "sum"}],
        "split_by": {"column": "region", "max_values": 5},
    },
    "Row": {
        "chart_type": "Row",
        "x": {"column": "region"},
        "y": [{"fn": "count"}],
    },
    "Number": {
        "chart_type": "Number",
        "y": [{"column": "price", "fn": "sum"}, {"fn": "count"}],
        "x": {"column": "order_date", "granularity": "day"},
    },
    "Donut": {
        "chart_type": "Donut",
        "x": {"column": "region"},
        "y": [{"column": "price", "fn": "sum"}],
    },
    "Funnel": {
        "chart_type": "Funnel",
        "x": {"column": "stage"},
        "y": [{"column": "qty", "fn": "count"}],
    },
    "Table": {
        "chart_type": "Table",
        "rows": [{"column": "region"}],
        "columns": [{"column": "product"}],
        "values": [{"column": "price", "fn": "sum"}],
    },
    "Map": {
        "chart_type": "Map",
        "x": {"column": "country"},
        "y": [{"column": "price", "fn": "sum"}],
    },
    "Bubble": {
        "chart_type": "Bubble",
        "x_measure": {"column": "price", "fn": "sum"},
        "y_measure": {"column": "qty", "fn": "avg"},
        "size": {"column": "weight", "fn": "max"},
        "group": {"column": "product"},
        "quadrant": {"column": "region"},
    },
    "Sankey": {
        "chart_type": "Sankey",
        "x": {"column": "region"},
        "target": {"column": "country"},
        "y": [{"column": "price", "fn": "sum"}],
    },
}


def resolve(chart_type):
    return chartspec.resolve(SPECS[chart_type], SYMBOLS)


class TestEveryChartTypeResolves(UnitTestCase):
    def test_all_ten_types_have_a_fixture(self):
        self.assertEqual(sorted(SPECS), sorted(CHARTS))

    def test_the_produced_config_is_a_fixed_point_of_the_ui_normalization(self):
        for chart_type in SPECS:
            with self.subTest(chart_type=chart_type):
                _, config, _ = resolve(chart_type)
                self.assertEqual(normalize_config(chart_type, config), config)

    def test_a_decompiled_spec_rebuilds_the_identical_config(self):
        """The property `get_item` actually needs.

        Not `decompile(resolve(spec)) == spec`: resolving materialises defaults the caller
        did not write -- a Date dimension gains `granularity: month` (`makeDimension`,
        `query/helpers.ts:224-231`). The decompiled spec is therefore more explicit than
        the original, and what matters is that submitting it back produces the same chart.
        """
        for chart_type in SPECS:
            with self.subTest(chart_type=chart_type):
                _, config, _ = resolve(chart_type)
                decompiled, reason = chartspec.decompile(chart_type, config)
                self.assertIsNone(reason)
                self.assertEqual(chartspec.resolve(decompiled, SYMBOLS)[1], config)

    def test_a_decompiled_spec_only_ever_adds_explicitness(self):
        for chart_type in SPECS:
            with self.subTest(chart_type=chart_type):
                _, config, _ = resolve(chart_type)
                decompiled, _reason = chartspec.decompile(chart_type, config)
                for key, value in SPECS[chart_type].items():
                    if isinstance(value, dict):
                        self.assertLessEqual(value.items(), decompiled[key].items())
                    else:
                        self.assertEqual(decompiled[key], value)

    def test_the_chart_type_defaults_are_not_echoed_back_as_options(self):
        for chart_type in ("Donut", "Funnel"):
            with self.subTest(chart_type=chart_type):
                _, config, _ = resolve(chart_type)
                decompiled, _reason = chartspec.decompile(chart_type, config)
                self.assertNotIn("options", decompiled)

    def test_every_measure_carries_a_real_aggregation(self):
        """`apply_aggregate` frappe.throws on anything outside the six (ibis_utils.py:841-855)."""
        for chart_type in SPECS:
            with self.subTest(chart_type=chart_type):
                _, config, _ = resolve(chart_type)
                measures = chartspec._all_measures(config)
                self.assertTrue(measures)
                for m in measures:
                    self.assertIn(m["aggregation"], schemas.AGGREGATIONS)


class TestNormalizationParity(UnitTestCase):
    """A second transcription of chart.ts:567-603 and helpers.ts:1349-1381."""

    NORMALIZATION_RULES: ClassVar[list] = [
        ("*", "filters", {"filters": [], "logical_operator": "And"}),
        ("*", "order_by", []),
        ("*", "limit", 100),
        ("Funnel", "label_position", "left"),
        ("Donut", "legend_position", "bottom"),
    ]

    DIMENSION_NAME_TARGETS: ClassVar[list] = [
        ("x_axis", "dimension"),
        ("split_by", "dimension"),
        ("date_column", None),
        ("label_column", None),
    ]

    NOT_DIMENSION_NAME_TARGETS: ClassVar[list] = [
        "location_column",
        "dimension",
        "quadrant_column",
        "source_column",
        "target_column",
    ]

    def test_defaults_match_the_transcribed_rules(self):
        for chart_type, key, expected in self.NORMALIZATION_RULES:
            types = list(CHARTS) if chart_type == "*" else [chart_type]
            for t in types:
                with self.subTest(chart_type=t, key=key):
                    self.assertEqual(normalize_config(t, {})[key], expected)

    def test_position_defaults_are_scoped_to_their_chart_type(self):
        for chart_type in CHARTS:
            if chart_type != "Funnel":
                self.assertNotIn("label_position", normalize_config(chart_type, {}))
            if chart_type != "Donut":
                self.assertNotIn("legend_position", normalize_config(chart_type, {}))

    def test_dimension_names_are_backfilled_exactly_where_the_ui_backfills_them(self):
        for outer, inner in self.DIMENSION_NAME_TARGETS:
            with self.subTest(target=f"{outer}.{inner}" if inner else outer):
                raw = {"column_name": "c"}
                config = {outer: {inner: raw} if inner else raw}
                result = normalize_config("Bar", config)
                target = result[outer][inner] if inner else result[outer]
                self.assertEqual(target["dimension_name"], "c")

        for key in ("rows", "columns"):
            with self.subTest(target=key):
                result = normalize_config("Table", {key: [{"column_name": "c"}]})
                self.assertEqual(result[key][0]["dimension_name"], "c")

    def test_the_ui_leaves_these_alone_and_so_do_we(self):
        config = normalize_config(
            "Bubble", {key: {"column_name": "c"} for key in self.NOT_DIMENSION_NAME_TARGETS}
        )
        for key in self.NOT_DIMENSION_NAME_TARGETS:
            with self.subTest(key=key):
                self.assertNotIn("dimension_name", config[key])

    def test_we_emit_dimension_name_everywhere_anyway(self):
        """Safe *because* the normalizer only ever adds: emitting it is a fixed point.

        It matters for Map/Bubble/Sankey, whose dimensions setDimensionNames never visits
        -- without it `translate_dimension` falls back to column_name and a custom `as`
        would be silently discarded (`ibis_utils.py:826-836`).
        """
        for chart_type in ("Map", "Bubble", "Sankey"):
            _, config, _ = resolve(chart_type)
            for key in self.NOT_DIMENSION_NAME_TARGETS:
                if isinstance(config.get(key), dict) and config[key].get("column_name"):
                    with self.subTest(chart_type=chart_type, key=key):
                        self.assertIn("dimension_name", config[key])


class TestMeasures(UnitTestCase):
    def test_count_star_is_byte_identical_to_the_frontend_helper(self):
        _, config, _ = resolve("Row")
        measure = config["y_axis"]["series"][0]["measure"]
        self.assertEqual(
            measure,
            {
                "column_name": "count",
                "data_type": "Integer",
                "aggregation": "count",
                "measure_name": "count_of_rows",
            },
        )

    def test_an_axis_chart_with_no_measure_defaults_to_count(self):
        _, config, _ = chartspec.resolve(
            {"chart_type": "Bar", "x": {"column": "region"}, "y": []}, SYMBOLS
        )
        self.assertEqual(config["y_axis"]["series"][0]["measure"]["measure_name"], "count_of_rows")

    def test_measure_names_are_derived_not_asked_for(self):
        _, config, _ = resolve("Bar")
        self.assertEqual(
            config["y_axis"]["series"][0]["measure"]["measure_name"], "sum_of_price"
        )

    def test_as_overrides_the_derived_name(self):
        _, config, _ = chartspec.resolve(
            {
                "chart_type": "Bar",
                "x": {"column": "region"},
                "y": [{"column": "price", "fn": "sum", "as": "Revenue"}],
            },
            SYMBOLS,
        )
        self.assertEqual(config["y_axis"]["series"][0]["measure"]["measure_name"], "Revenue")

    def test_fn_none_keeps_the_column_name_and_sums(self):
        _, config, notes = chartspec.resolve(
            {
                "chart_type": "Bar",
                "x": {"column": "region"},
                "y": [{"column": "sum_of_amount", "fn": "none"}],
            },
            SYMBOLS,
        )
        measure = config["y_axis"]["series"][0]["measure"]
        self.assertEqual(measure["measure_name"], "sum_of_amount")
        self.assertEqual(measure["aggregation"], "sum")
        self.assertTrue(any("fn: none" in note for note in notes))

    def test_summing_a_string_is_refused_and_points_at_fn_none(self):
        with self.assertRaises(ToolError) as ctx:
            chartspec.resolve(
                {
                    "chart_type": "Bar",
                    "x": {"column": "region"},
                    "y": [{"column": "sum_of_amount", "fn": "sum"}],
                },
                SymbolTable.from_json([
                    {"name": "region", "data_type": "String"},
                    {"name": "sum_of_amount", "data_type": "String"},
                ]),
            )
        self.assertIn('fn: "none"', str(ctx.exception))


class TestNumericDimensions(UnitTestCase):
    """Measured on a real dashboard: grouping by a numeric `month` renders it as a second
    series, because the UI never offers a numeric column as a dimension."""

    def test_a_numeric_dimension_is_warned_about_not_refused(self):
        _, _config, notes = chartspec.resolve(
            {"chart_type": "Bar", "x": {"column": "qty"}, "y": [{"fn": "count"}]}, SYMBOLS
        )
        self.assertTrue(any("does not offer as a dimension" in note for note in notes))

    def test_a_string_dimension_produces_no_such_note(self):
        _, _config, notes = chartspec.resolve(
            {"chart_type": "Bar", "x": {"column": "region"}, "y": [{"fn": "count"}]}, SYMBOLS
        )
        self.assertEqual(notes, [])


class TestGranularity(UnitTestCase):
    def test_a_date_dimension_defaults_to_month(self):
        _, config, _ = chartspec.resolve(
            {"chart_type": "Bar", "x": {"column": "order_date"}, "y": [{"fn": "count"}]}, SYMBOLS
        )
        self.assertEqual(config["x_axis"]["dimension"]["granularity"], "month")

    def test_a_time_dimension_defaults_to_hour(self):
        _, config, _ = chartspec.resolve(
            {"chart_type": "Bar", "x": {"column": "clock_in"}, "y": [{"fn": "count"}]}, SYMBOLS
        )
        self.assertEqual(config["x_axis"]["dimension"]["granularity"], "hour")

    def test_a_time_dimension_rejects_a_date_granularity(self):
        with self.assertRaises(ToolError) as ctx:
            chartspec.resolve(
                {
                    "chart_type": "Bar",
                    "x": {"column": "clock_in", "granularity": "month"},
                    "y": [{"fn": "count"}],
                },
                SYMBOLS,
            )
        self.assertIn("second, minute, hour", str(ctx.exception))

    def test_a_string_dimension_carries_no_granularity(self):
        _, config, _ = chartspec.resolve(
            {"chart_type": "Bar", "x": {"column": "region"}, "y": [{"fn": "count"}]}, SYMBOLS
        )
        self.assertNotIn("granularity", config["x_axis"]["dimension"])

    def test_a_granularity_on_a_string_dimension_is_refused_not_ignored(self):
        """The backend ignores it silently -- a successful wrong answer."""
        with self.assertRaises(ToolError):
            chartspec.resolve(
                {
                    "chart_type": "Bar",
                    "x": {"column": "region", "granularity": "month"},
                    "y": [{"fn": "count"}],
                },
                SYMBOLS,
            )


class TestSpecErrors(UnitTestCase):
    def test_a_key_from_another_chart_type_is_named_not_ignored(self):
        with self.assertRaises(ToolError) as ctx:
            chartspec.resolve(
                {"chart_type": "Bar", "x": {"column": "region"}, "rows": [{"column": "region"}]},
                SYMBOLS,
            )
        message = str(ctx.exception)
        self.assertIn("`rows` does not apply to a Bar chart", message)
        self.assertIn("Table", message)

    def test_an_unknown_column_lists_the_valid_ones(self):
        with self.assertRaises(ToolError) as ctx:
            chartspec.resolve(
                {"chart_type": "Bar", "x": {"column": "nope"}, "y": [{"fn": "count"}]}, SYMBOLS
            )
        self.assertIn("valid_columns", str(ctx.exception))

    def test_wrong_chart_type_casing_is_rejected_not_corrected(self):
        with self.assertRaises(ToolError) as ctx:
            chartspec.resolve({"chart_type": "bar", "x": {"column": "region"}}, SYMBOLS)
        self.assertIn("Bar", str(ctx.exception))

    def test_a_missing_required_slot_names_it(self):
        with self.assertRaises(ToolError) as ctx:
            chartspec.resolve({"chart_type": "Donut", "x": {"column": "region"}}, SYMBOLS)
        self.assertIn("`y` is required", str(ctx.exception))

    def test_a_single_measure_chart_refuses_two(self):
        with self.assertRaises(ToolError) as ctx:
            chartspec.resolve(
                {
                    "chart_type": "Donut",
                    "x": {"column": "region"},
                    "y": [{"column": "price", "fn": "sum"}, {"fn": "count"}],
                },
                SYMBOLS,
            )
        self.assertIn("exactly one measure", str(ctx.exception))

    def test_a_sort_on_a_source_column_is_refused(self):
        with self.assertRaises(ToolError) as ctx:
            chartspec.resolve(
                {
                    "chart_type": "Bar",
                    "x": {"column": "region"},
                    "y": [{"column": "price", "fn": "sum"}],
                    "order_by": [{"column": "price", "desc": True}],
                },
                SYMBOLS,
            )
        self.assertIn("sum_of_price", str(ctx.exception))

    def test_a_sort_on_an_output_column_is_accepted(self):
        _, config, _ = chartspec.resolve(
            {
                "chart_type": "Bar",
                "x": {"column": "region"},
                "y": [{"column": "price", "fn": "sum"}],
                "order_by": [{"column": "sum_of_price", "desc": True}],
            },
            SYMBOLS,
        )
        self.assertEqual(
            config["order_by"],
            [{"column": {"type": "column", "column_name": "sum_of_price"}, "direction": "desc"}],
        )


class TestOptionsPassthrough(UnitTestCase):
    def test_cosmetic_keys_land_on_the_config(self):
        _, config, _ = chartspec.resolve(
            dict(SPECS["Donut"], options={"legend_position": "top", "max_slices": 5}), SYMBOLS
        )
        self.assertEqual(config["legend_position"], "top")
        self.assertEqual(config["max_slices"], 5)

    def test_a_structural_key_cannot_be_smuggled_through_options(self):
        _, config, notes = chartspec.resolve(
            dict(SPECS["Bar"], options={"y_axis": {"series": []}}), SYMBOLS
        )
        self.assertTrue(config["y_axis"]["series"])
        self.assertTrue(any("Ignored `options.y_axis`" in note for note in notes))

    def test_stacked_becomes_the_y_axis_stack_flag(self):
        _, config, _ = chartspec.resolve(dict(SPECS["Bar"], options={"stacked": True}), SYMBOLS)
        self.assertTrue(config["y_axis"]["stack"])
        self.assertNotIn("stacked", config)


class TestCamelCaseContainment(UnitTestCase):
    def test_bubble_camel_case_is_produced_but_never_exposed(self):
        """Design §7.1: handled inside the compiler and never put in front of the model."""
        _, config, _ = resolve("Bubble")
        self.assertIn("xAxis", config)
        self.assertNotIn("xAxis", json.dumps(schemas.CHART_SPEC))
        self.assertNotIn("yAxis", json.dumps(schemas.CHART_SPEC))


class TestDecompileBails(UnitTestCase):
    def test_an_expression_measure_is_not_expressible(self):
        spec, reason = chartspec.decompile(
            "Donut",
            {
                "label_column": {"column_name": "region", "dimension_name": "region"},
                "value_column": {
                    "measure_name": "margin",
                    "expression": {"type": "expression", "expression": "a - b"},
                    "data_type": "Decimal",
                },
            },
        )
        self.assertIsNone(spec)
        self.assertIn("expression measure", reason)

    def test_an_unknown_chart_type_is_reported(self):
        spec, reason = chartspec.decompile("pie", {})
        self.assertIsNone(spec)
        self.assertIn("unknown chart type", reason)

    def test_a_config_missing_its_structural_keys_is_reported(self):
        spec, reason = chartspec.decompile("Donut", {"label_column": {"column_name": "region"}})
        self.assertIsNone(spec)
        self.assertIn("missing", reason)
