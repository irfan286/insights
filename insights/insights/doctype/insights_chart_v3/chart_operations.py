# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""Python port of the chart render pipeline.

A chart's `config` does not describe its data. A hidden `Insights Query v3`
(`chart.data_query`) does, and until now its `operations[]` were generated only in the
browser -- `frontend/src2/charts/chart.ts:58-92` `refresh()`. A headless caller (the MCP
server, a scheduled render, an export) has no browser, so an MCP-created chart would be a
config blob that never renders.

This module is the port. It is deliberately free of MCP imports so that `chart.ts` can be
pointed at it later (design §7.2 follow-up) and there is exactly one implementation.

Fidelity notes, because "obviously equivalent" rewrites are how the two copies drift:

* The pipeline emits source -> filter_group -> chart operation -> order_by[] and
  **never a `limit` operation**. The chart's limit is the `page_size` argument at execute
  time (`chart.ts:91`).
* Sankey emits no chart operation at all -- it has no branch in `addChartOperation`
  (`chart.ts:220-244`) and its renderer aggregates client-side.
* `normalize_config` reproduces `transformChartDoc` (`chart.ts:567-603`) including the
  asymmetry in `setDimensionNames` (`charts/helpers.ts:1349-1381`), which touches
  `x_axis.dimension`, `split_by.dimension`, `date_column`, `label_column`, `rows[]` and
  `columns[]` and NOT `location_column`, the bubble `dimension`/`quadrant_column`, or the
  Sankey columns. Do not "fix" that here: a config written by this module has to survive a
  round trip through the UI unchanged.
"""

import copy

import frappe

AXIS_CHARTS = ("Bar", "Line", "Row")
CHARTS = (
    "Number",
    "Bar",
    "Line",
    "Row",
    "Donut",
    "Funnel",
    "Table",
    "Map",
    "Bubble",
    "Sankey",
)

DEFAULT_LIMIT = 100
DEFAULT_MAX_COLUMN_VALUES = 10


def count_measure() -> dict:
    """The literal measure the backend special-cases at `ibis_utils.py:810-814`.

    Mirrors `count()` in `frontend/src2/query/helpers.ts:92-97`, key order included.
    """
    return {
        "column_name": "count",
        "data_type": "Integer",
        "aggregation": "count",
        "measure_name": "count_of_rows",
    }


def column_ref(column_name: str) -> dict:
    return {"type": "column", "column_name": column_name}


def _as_dict(value) -> dict:
    parsed = frappe.parse_json(value) if isinstance(value, str) else value
    return copy.deepcopy(parsed) if isinstance(parsed, dict) else {}


def _dimensions_of(value) -> list:
    """A list of dimension dicts that actually name a column."""
    return [d for d in (value or []) if isinstance(d, dict) and d.get("column_name")]


def _measures_of(value) -> list:
    return [m for m in (value or []) if isinstance(m, dict) and m.get("measure_name")]


# ---------------------------------------------------------------------------
# transformChartDoc -- chart.ts:567-603
# ---------------------------------------------------------------------------


def _handle_old_x_axis_config(old):
    """chart.ts imports this from charts/helpers.ts:1331-1338."""
    if isinstance(old, dict) and old.get("column_name"):
        return {"dimension": old}
    return old


def _handle_old_y_axis_config(old):
    """charts/helpers.ts:1340-1347."""
    if isinstance(old, list):
        return {"series": [{"measure": measure} for measure in old]}
    return old


def _set_dimension_name(dimension):
    if (
        isinstance(dimension, dict)
        and not dimension.get("dimension_name")
        and dimension.get("column_name")
    ):
        dimension["dimension_name"] = dimension["column_name"]
    return dimension


def set_dimension_names(config: dict) -> dict:
    """charts/helpers.ts:1349-1381. The omissions are load-bearing -- see module docstring."""
    if isinstance(config.get("x_axis"), dict) and config["x_axis"].get("dimension"):
        config["x_axis"]["dimension"] = _set_dimension_name(config["x_axis"]["dimension"])
    if isinstance(config.get("split_by"), dict) and config["split_by"].get("dimension"):
        config["split_by"]["dimension"] = _set_dimension_name(config["split_by"]["dimension"])
    if config.get("date_column"):
        config["date_column"] = _set_dimension_name(config["date_column"])
    if config.get("label_column"):
        config["label_column"] = _set_dimension_name(config["label_column"])
    if config.get("rows"):
        config["rows"] = [_set_dimension_name(row) for row in config["rows"]]
    if config.get("columns"):
        config["columns"] = [_set_dimension_name(col) for col in config["columns"]]
    return config


def ensure_config_slots(config: dict, chart_type: str) -> dict:
    """charts/helpers.ts:1535-1549.

    The empty containers a config form binds to before a human has filled anything in.
    A config that reaches the UI without them is mutated on the first render, so an
    MCP-written chart has to carry them too or the two never compare equal.
    """
    if chart_type in AXIS_CHARTS:
        config["x_axis"] = config.get("x_axis") or {}
        config["x_axis"]["dimension"] = config["x_axis"].get("dimension") or {}
        config["y_axis"] = config.get("y_axis") or {}
        config["y_axis"]["series"] = config["y_axis"].get("series") or []

    if chart_type == "Map":
        config["location_column"] = config.get("location_column") or {}
        config["value_column"] = config.get("value_column") or {}

    return config


def normalize_config(chart_type: str, config) -> dict:
    """Every default `transformChartDoc` injects on load, applied up front instead.

    A config written without these is silently mutated the first time a human opens the
    chart, so a UI-written and an MCP-written chart would never compare equal.
    """
    config = _as_dict(config)

    filters = config.get("filters")
    has_filters = isinstance(filters, dict) and filters.get("filters")
    config["filters"] = filters if has_filters else {"filters": [], "logical_operator": "And"}
    config["order_by"] = config.get("order_by") or []
    config["limit"] = config.get("limit") or DEFAULT_LIMIT

    if config.get("x_axis"):
        config["x_axis"] = _handle_old_x_axis_config(config["x_axis"])
    if isinstance(config.get("y_axis"), list):
        config["y_axis"] = _handle_old_y_axis_config(config["y_axis"])
    if config.get("split_by"):
        config["split_by"] = _handle_old_x_axis_config(config["split_by"])

    if chart_type == "Funnel":
        config["label_position"] = config.get("label_position") or "left"
    if chart_type == "Donut":
        config["legend_position"] = config.get("legend_position") or "bottom"

    config = set_dimension_names(config)
    config = ensure_config_slots(config, chart_type)

    # The bar config form writes this default when it mounts, which leaves a freshly
    # opened chart dirty, so `transformChartDoc` sets it on load instead. Absence is
    # what carries the default -- a config that says `False` meant it.
    if chart_type == "Bar" and "stack" not in config["y_axis"]:
        config["y_axis"]["stack"] = True

    return config


# ---------------------------------------------------------------------------
# validateConfig -- chart.ts:94-205
# ---------------------------------------------------------------------------


def validate_config(chart_type: str, config, query: str | None = None) -> list[str]:
    """The messages `validateConfig` collects, in the same order.

    The TypeScript dereferences `config.x_axis.dimension`, `config.location_column` and
    `config.value_column` without a guard, so a config missing them throws there rather
    than returning a message. Here they are guarded and reported instead -- a headless
    caller deserves the diagnostic, not a TypeError.
    """
    config = _as_dict(config)
    messages = []

    if not query:
        messages.append("Query is required")
    if not chart_type:
        messages.append("Chart type is required")
    if chart_type and chart_type not in CHARTS:
        messages.append(f"Invalid chart type: {chart_type}")

    if chart_type in AXIS_CHARTS:
        x_axis = config.get("x_axis") or {}
        dimension = x_axis.get("dimension") or {}
        split_by = (config.get("split_by") or {}).get("dimension") or {}
        if not dimension.get("column_name"):
            messages.append("X-axis is required")
        elif dimension.get("column_name") == split_by.get("column_name"):
            messages.append("X-axis and Split by cannot be the same")

    if chart_type == "Number":
        if not _measures_of(config.get("number_columns")):
            messages.append("Number column is required")

    if chart_type in ("Donut", "Funnel"):
        if not (config.get("label_column") or {}).get("column_name"):
            messages.append("Label column is required")
        if not (config.get("value_column") or {}).get("measure_name"):
            messages.append("Value column is required")

    if chart_type == "Table":
        if not _dimensions_of(config.get("rows")):
            messages.append("Rows are required")

    if chart_type == "Map":
        if not (config.get("location_column") or {}).get("column_name"):
            messages.append("Location column is required")
        if not (config.get("value_column") or {}).get("measure_name"):
            messages.append("Value column is required")

    if chart_type == "Bubble":
        if not (config.get("xAxis") or {}).get("measure_name"):
            messages.append("X-axis is required")
        if not (config.get("yAxis") or {}).get("measure_name"):
            messages.append("Y-axis is required")

    if chart_type == "Sankey":
        # No branch exists in validateConfig, and Sankey genuinely renders from raw rows
        # (design §7.1). Report the real requirement without blocking.
        for key, label in (
            ("source_column", "Source column"),
            ("target_column", "Target column"),
        ):
            if not (config.get(key) or {}).get("column_name"):
                messages.append(f"{label} is required")
        if not (config.get("value_column") or {}).get("measure_name"):
            messages.append("Value column is required")

    return messages


# ---------------------------------------------------------------------------
# The operation builders -- chart.ts:207-384
# ---------------------------------------------------------------------------


def _add_order_by(operations: list, column_name: str, direction: str) -> None:
    """query.ts:415-432 -- exact match is a no-op, same column re-sorted replaces in place."""
    for op in operations:
        if (
            op["type"] == "order_by"
            and op["column"]["column_name"] == column_name
            and op["direction"] == direction
        ):
            return

    for index, op in enumerate(operations):
        if op["type"] == "order_by" and op["column"]["column_name"] == column_name:
            operations[index] = {
                "type": "order_by",
                "column": column_ref(column_name),
                "direction": direction,
            }
            return

    operations.append(
        {"type": "order_by", "column": column_ref(column_name), "direction": direction}
    )


def _axis_chart_operations(config: dict) -> list[dict]:
    """chart.ts:246-266."""
    x_axis = (config.get("x_axis") or {}).get("dimension")
    series = (config.get("y_axis") or {}).get("series") or []
    values = _measures_of([s.get("measure") for s in series if isinstance(s, dict)])
    values = values or [count_measure()]

    split_by = (config.get("split_by") or {}).get("dimension") or {}
    if split_by.get("column_name"):
        return [
            {
                "type": "pivot_wider",
                "rows": [x_axis],
                "columns": [split_by],
                "values": values,
                "max_column_values": (config.get("split_by") or {}).get("max_split_values")
                or DEFAULT_MAX_COLUMN_VALUES,
            }
        ]

    return [{"type": "summarize", "measures": values, "dimensions": [x_axis]}]


def _number_chart_operations(config: dict) -> list[dict]:
    """chart.ts:268-275."""
    date_column = config.get("date_column") or {}
    return [
        {
            "type": "summarize",
            "measures": _measures_of(config.get("number_columns")),
            "dimensions": [date_column] if date_column.get("column_name") else [],
        }
    ]


def _donut_chart_operations(config: dict) -> list[dict]:
    """chart.ts:277-288 -- shared by Donut and Funnel.

    The implicit `desc` sort is emitted here, ahead of the config's own `order_by` list, so
    a user sort on the same column replaces it through the dedup rule rather than fighting it.
    """
    value_column = config.get("value_column") or {}
    operations = [
        {
            "type": "summarize",
            "measures": [value_column],
            "dimensions": [config.get("label_column") or {}],
        }
    ]
    _add_order_by(operations, value_column.get("measure_name"), "desc")
    return operations


def _table_chart_operations(config: dict) -> list[dict]:
    """chart.ts:290-332 -- the three-way branch."""
    rows = _dimensions_of(config.get("rows"))
    columns = _dimensions_of(config.get("columns"))
    values = _measures_of(config.get("values"))

    if columns:
        return [
            {
                "type": "pivot_wider",
                "rows": rows,
                "columns": columns,
                "values": values,
                "max_column_values": config.get("max_column_values")
                or DEFAULT_MAX_COLUMN_VALUES,
            }
        ]

    if not values and config.get("show_raw_rows"):
        # `select` has no aliasing of its own, so custom row labels need an explicit
        # rename to survive -- unlike `summarize`, where dimension_name becomes the
        # output column name (chart.ts:317-319).
        operations = [{"type": "select", "column_names": [r["column_name"] for r in rows]}]
        for row in rows:
            if row.get("dimension_name") and row["dimension_name"] != row["column_name"]:
                operations.append(
                    {
                        "type": "rename",
                        "column": column_ref(row["column_name"]),
                        "new_name": row["dimension_name"],
                    }
                )
        return operations

    return [{"type": "summarize", "measures": values, "dimensions": rows}]


def _map_chart_operations(config: dict) -> list[dict]:
    """chart.ts:334-341."""
    return [
        {
            "type": "summarize",
            "measures": [config.get("value_column") or {}],
            "dimensions": [config.get("location_column") or {}],
        }
    ]


def _bubble_chart_operations(config: dict) -> list[dict]:
    """chart.ts:343-373. Bubble is the only chart with camelCase config keys."""
    measures = []
    for key in ("xAxis", "yAxis", "size_column"):
        measure = config.get(key) or {}
        if measure.get("measure_name"):
            measures.append(measure)

    dimensions = []
    for key in ("dimension", "quadrant_column"):
        dimension = config.get(key) or {}
        if dimension.get("column_name"):
            dimensions.append(dimension)

    return [{"type": "summarize", "measures": measures, "dimensions": dimensions}]


CHART_OPERATION_BUILDERS = {
    "Bar": _axis_chart_operations,
    "Line": _axis_chart_operations,
    "Row": _axis_chart_operations,
    "Number": _number_chart_operations,
    "Donut": _donut_chart_operations,
    "Funnel": _donut_chart_operations,
    "Table": _table_chart_operations,
    "Map": _map_chart_operations,
    "Bubble": _bubble_chart_operations,
    # Sankey is absent on purpose: addChartOperation has no branch for it, so its
    # data_query is source + filters + order_by over the bound query's raw columns.
}


def build_data_query_operations(chart) -> list[dict]:
    """The operations a chart's hidden data_query should carry. Contract name, do not rename.

    `chart` is anything exposing `.query`, `.chart_type` and `.config` -- an
    `Insights Chart v3` document, or a plain `frappe._dict` in tests.
    """
    chart_type = chart.chart_type
    config = normalize_config(chart_type, chart.config)

    errors = validate_config(chart_type, config, query=chart.query)
    if errors:
        frappe.throw(
            "This chart's configuration is incomplete: " + "; ".join(errors),
            title="Invalid chart configuration",
        )

    operations = [
        {
            "type": "source",
            "table": {"type": "query", "workbook": "", "query_name": chart.query},
        }
    ]

    if config["filters"].get("filters"):
        operations.append({"type": "filter_group", **config["filters"]})

    builder = CHART_OPERATION_BUILDERS.get(chart_type)
    if builder:
        operations.extend(builder(config))

    for sort in config["order_by"]:
        column_name = (sort.get("column") or {}).get("column_name")
        direction = sort.get("direction")
        if column_name and direction:
            _add_order_by(operations, column_name, direction)

    return operations
