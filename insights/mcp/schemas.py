# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""Explicit JSON Schemas for every MCP tool.

>>> READ THIS BEFORE ADDING A TOOL <<<

**Every field description must live inside the schema dict here, never in the function
docstring's `Args:` block.** `frappe_mcp.server.tools.__init__.get_tool` computes the
inferred schema, merges docstring `Args:` descriptions into *that* object, and only
then does `input_schema = options.get("input_schema") or _input_schema`
(`tools/__init__.py:66-71`). When we pass an explicit `input_schema`, the object the
descriptions were written into is discarded. A contributor who adds an `Args:` block
and expects it to reach the model will be silently wrong.

**Why explicit at all** (non-negotiable #2): upstream's type-hint inference cannot
express enums, defaults, minimum/maximum, nested object properties, or `$defs` --
`_convert_type_to_json_schema` falls through to `return {}` for anything it does not
recognise (`tool_schema.py:113-114`). A `dict` parameter would collapse to
`{"type": "object"}` and the entire DSL would vanish from `tools/list`.

**Self-contained.** A tool's `inputSchema` goes on the wire alone, so `$ref`s point only
at `$defs` inside the same schema -- never across tools.

The same constant is passed to BOTH `@mcp.tool(input_schema=...)` and `@tool_args(...)`,
because upstream declares the schema and then never validates against it (§3.6).
"""

from insights.insights.doctype.insights_chart_v3.chart_operations import AXIS_CHARTS, CHARTS

# --- controlled vocabularies -------------------------------------------------
# Casing is load-bearing. `perform_operation` is an if/elif chain that falls through to
# `return self.query` on an unknown type (ibis_utils.py:157) -- a wrong case is a SILENT
# no-op producing a successful wrong answer, not an error. The compiler rejects wrong
# casing rather than correcting it, so the model learns.

CAST_TYPES = ["String", "Text", "Integer", "Decimal", "Date", "Time", "Datetime", "Boolean"]
DERIVE_TYPES = ["String", "Integer", "Decimal", "Date", "Datetime", "Time", "Boolean", "Auto"]
JOIN_TYPES = ["inner", "left", "right", "full"]
AGGREGATIONS = ["sum", "count", "avg", "min", "max", "count_distinct"]
GRANULARITIES = [
    "second", "minute", "hour", "day", "week", "month", "quarter", "year", "fiscal_year",
]
FILTER_OPERATORS = [
    "=", "!=", ">", ">=", "<", "<=",
    "in", "not_in", "between", "within",
    "contains", "not_contains", "starts_with", "ends_with",
    "is_set", "is_not_set", "is_true", "is_false", "is_not_true",
]

# Inlined into every schema that uses it rather than referenced with
# `$ref: "#/$defs/Filter"`. A `$ref` is resolved against the ROOT schema on the wire,
# and QUERY_SPEC is embedded as `run_query.properties.spec` -- so `#/$defs/Filter`
# pointed at a location that does not exist on the root and every `where` clause
# raised PointerToNowhere. DRY is preserved in Python (this dict is reused by
# identity); only the serialized JSON repeats, ~600 bytes per occurrence.
FILTER = {
    "type": "object",
    "properties": {
        "column": {"type": "string"},
        "op": {
            "enum": FILTER_OPERATORS,
            "description": (
                "'within' takes a timespan string such as 'Last 7 days' or "
                "'Current month (include current)'. 'is_set'/'is_not_set'/'is_true'/"
                "'is_false' take no value."
            ),
        },
        "value": {},
    },
    "required": ["column", "op"],
}

QUERY_SPEC = {
    "type": "object",
    "description": (
        "CANNOT express: union, custom_operation, remove, expression-form filters, "
        "column-to-column comparisons, or nested filter groups deeper than one level. "
        "For those, use run_query.raw_operations."
    ),
    "properties": {
        "from": {
            "type": "object",
            "properties": {
                "data_source": {"type": "string"},
                "table": {"type": "string"},
                "query": {
                    "type": "string",
                    "description": "use a saved query as the source instead",
                },
            },
        },
        "joins": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "table": {"type": "string"},
                    "data_source": {"type": "string"},
                    "how": {"enum": JOIN_TYPES, "default": "left"},
                    "left_on": {"type": "string"},
                    "right_on": {"type": "string"},
                    "select": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Right-table columns to keep. Strongly recommended: without "
                            "it every right-table column is carried through, and any name "
                            "colliding with the left table is silently renamed."
                        ),
                    },
                },
                "required": ["table", "left_on", "right_on"],
            },
        },
        "cast": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "column": {"type": "string"},
                    "data_type": {"enum": CAST_TYPES},
                },
                "required": ["column", "data_type"],
            },
        },
        "where": {
            "type": "array",
            "description": (
                "ANDed together; applied BEFORE aggregation. Use having for "
                "post-aggregation filters."
            ),
            "items": FILTER,
        },
        "where_any": {
            "type": "array",
            "description": (
                "ORed together. When both where and where_any are present they are ANDed "
                "as two groups: (where[0] AND where[1] ...) AND (where_any[0] OR "
                "where_any[1] ...)."
            ),
            "items": FILTER,
        },
        "derive": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "expression": {
                        "type": "string",
                        "description": (
                            "ibis expression. Bare column identifiers, no quotes around "
                            "column names. Use & | ~ - NOT and/or/not. String literals in "
                            "single quotes. e.g. (amount > 100) & (status == 'C')"
                        ),
                    },
                    "data_type": {"enum": DERIVE_TYPES, "default": "Auto"},
                },
                "required": ["name", "expression"],
            },
        },
        "rename": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"column": {"type": "string"}, "as": {"type": "string"}},
                "required": ["column", "as"],
            },
        },
        "group_by": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "column": {"type": "string"},
                    "granularity": {
                        "enum": GRANULARITIES,
                        "description": (
                            "Date/Datetime/Time columns only. Omit for String columns."
                        ),
                    },
                    "as": {"type": "string"},
                },
                "required": ["column"],
            },
        },
        "aggregate": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "column": {"type": "string"},
                    "fn": {"enum": AGGREGATIONS},
                    "expression": {"type": "string"},
                    "as": {"type": "string"},
                },
            },
        },
        "having": {
            "type": "array",
            "description": (
                "Filters applied AFTER aggregation. Columns here must be aggregate/"
                "group_by output aliases (e.g. count_of_rows), not source columns. This "
                "is how you express 'customers with more than 10 orders'."
            ),
            "items": FILTER,
        },
        "pivot_on": {
            "type": "object",
            "properties": {
                "column": {"type": "string"},
                "max_values": {"type": "integer", "default": 10},
            },
        },
        "sort": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "column": {"type": "string"},
                    "desc": {"type": "boolean", "default": False},
                },
                "required": ["column"],
            },
        },
        "limit": {"type": "integer"},
        "select": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["from"],
}


# --- tool input schemas ------------------------------------------------------
# Each is self-contained: `$ref`s point only at `$defs` inside the same schema.

LIST_DATA_SOURCES = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}

LIST_TABLES = {
    "type": "object",
    "properties": {
        "data_source": {"type": "string"},
        "search": {"type": "string", "description": "case-insensitive substring match on the table name"},
        "limit": {"type": "integer", "default": 50, "maximum": 500},
        "start": {"type": "integer", "default": 0, "description": "offset, for paging past `limit`"},
    },
    "required": ["data_source"],
}

DESCRIBE_TABLE = {
    "type": "object",
    "properties": {
        "data_source": {"type": "string"},
        "table_name": {"type": "string"},
        "include_joins": {"type": "boolean", "default": True},
        "include_docs": {"type": "boolean", "default": True},
        "include_preview": {
            "type": "boolean",
            "default": False,
            "description": "return 5 sample rows. Costs tokens; off by default.",
        },
    },
    "required": ["data_source", "table_name"],
}

DISTINCT_VALUES = {
    "type": "object",
    "properties": {
        "data_source": {"type": "string"},
        "table_name": {"type": "string"},
        "saved_query": {"type": "string", "description": "alternative to data_source + table_name"},
        "column_name": {"type": "string"},
        "search": {"type": "string"},
        "limit": {"type": "integer", "default": 20, "maximum": 100},
    },
    "required": ["column_name"],
}

RUN_QUERY = {
    "type": "object",
    "properties": {
        "spec": QUERY_SPEC,
        "raw_operations": {
            "type": "array",
            "items": {"type": "object"},
            "description": (
                "ADVANCED escape hatch: raw v3 operations[]. Use only for union, "
                "custom_operation, or deeply nested filter groups. Prefer spec."
            ),
        },
        "saved_query": {"type": "string", "description": "run an existing Insights Query v3 by name"},
        "dry_run": {
            "type": "boolean",
            "default": False,
            "description": (
                "Build the query and return SQL + output columns WITHOUT returning rows. "
                "NOT free - it still resolves tables and writes an execution log."
            ),
        },
        "workbook": {"type": "string"},
        "use_live_connection": {"type": "boolean", "default": True},
        "page": {"type": "integer", "default": 1, "minimum": 1},
        "page_size": {"type": "integer", "default": 20, "maximum": 10000, "minimum": 1},
        "force": {"type": "boolean", "default": False, "description": "bypass the 10-minute result cache"},
        "include_sql": {"type": "boolean", "default": False},
        "include_count": {"type": "boolean", "default": False},
        "include_operations": {"type": "boolean", "default": False},
    },
}

GET_DOCS = {
    "type": "object",
    "properties": {
        "data_source": {"type": "string"},
        "table_name": {"type": "string", "description": "omit for the data-source-level overview"},
        "block_id": {"type": "string", "description": "fetch one block in full, e.g. after a truncation hint"},
        "include_erd": {"type": "boolean", "default": True},
        "include_ai_notes": {"type": "boolean", "default": True},
    },
    "required": ["data_source"],
}

WRITE_AI_NOTE = {
    "type": "object",
    "properties": {
        "data_source": {"type": "string"},
        "table_name": {"type": "string", "description": "omit for a data-source-level note"},
        "title": {"type": "string"},
        "body": {
            "type": "string",
            "description": (
                "markdown. State what you observed and how you observed it, so a human "
                "can verify it."
            ),
        },
        "supersedes": {"type": "string", "description": "name of an earlier AI note this replaces"},
        "propose_promotion": {"type": "boolean", "default": False},
    },
    "required": ["data_source", "title", "body"],
}


# --- ChartSpec ---------------------------------------------------------------
# A chart's stored `config` is a different shape per chart type, and Bubble's keys are
# camelCase while everything else is snake_case (`chart.types.ts:141-151`). Neither is
# something to put in front of a model. `ChartSpec` is one flat object whose keys carry
# the same vocabulary as QuerySpec; `chartspec.py` maps it onto the per-type config and
# rejects keys that do not belong to the chosen chart_type.
#
# No `oneOf`/`if-then` per chart type: the model does not honour them (§4.2's finding for
# run_query), and an explicit runtime error naming the wrong key teaches it more.

CHART_TYPES = list(CHARTS)
AXIS_CHART_TYPES = list(AXIS_CHARTS)

DIMENSION_REF = {
    "type": "object",
    "properties": {
        "column": {"type": "string"},
        "granularity": {
            "enum": GRANULARITIES,
            "description": (
                "Date/Datetime/Time columns only, and Time accepts only "
                "second/minute/hour. Omit for String columns -- it is silently ignored "
                "there, which produces a wrong answer that looks right."
            ),
        },
        "as": {"type": "string", "description": "output label; defaults to the column name"},
    },
    "required": ["column"],
}

MEASURE_REF = {
    "type": "object",
    "properties": {
        "column": {"type": "string", "description": "omit with fn=count to count rows"},
        "fn": {
            "enum": AGGREGATIONS + ["none"],
            "description": (
                "'none' means the column is ALREADY aggregated by the bound query: it "
                "keeps the column's own name and re-aggregates with sum, which is the "
                "identity only when the chart groups by what the query already grouped by."
            ),
        },
        "as": {"type": "string", "description": "output label; defaults to fn_of_column"},
    },
    "required": ["fn"],
}

ORDER_BY_REF = {
    "type": "object",
    "properties": {
        "column": {
            "type": "string",
            "description": "an OUTPUT column of the chart -- a dimension label or a measure name",
        },
        "desc": {"type": "boolean", "default": False},
    },
    "required": ["column"],
}

CHART_SPEC = {
    "type": "object",
    "description": (
        "One flat spec for all 10 chart types. Which keys apply depends on chart_type; "
        "supplying a key that the chosen type does not use is an error naming the right "
        "one. Bar/Line/Row: x, y, split_by. Number: y (+ optional x as the date column). "
        "Donut/Funnel: x (label), y[0] (value). Map: x (location), y[0] (value). "
        "Table: rows, columns, values. Bubble: x_measure, y_measure, size, group, "
        "quadrant. Sankey: x (source), target, y[0] (value) -- Sankey adds no aggregation, "
        "so its bound query must already emit source/target/value rows."
    ),
    "properties": {
        "chart_type": {"enum": CHART_TYPES},
        "x": DIMENSION_REF,
        "y": {"type": "array", "items": MEASURE_REF},
        "split_by": {
            "type": "object",
            "description": "Bar/Line/Row only. Pivots the measures into one series per value.",
            "properties": {
                "column": {"type": "string"},
                "granularity": {"enum": GRANULARITIES},
                "max_values": {"type": "integer", "default": 10, "minimum": 1, "maximum": 100},
            },
            "required": ["column"],
        },
        "target": DIMENSION_REF,
        "rows": {"type": "array", "items": DIMENSION_REF, "description": "Table only"},
        "columns": {
            "type": "array",
            "items": DIMENSION_REF,
            "description": "Table only: pivot the values into one column per value",
        },
        "values": {"type": "array", "items": MEASURE_REF, "description": "Table only"},
        "x_measure": MEASURE_REF,
        "y_measure": MEASURE_REF,
        "size": MEASURE_REF,
        "group": DIMENSION_REF | {"description": "Bubble only: the dimension each bubble represents"},
        "quadrant": DIMENSION_REF,
        "filters": {
            "type": "array",
            "description": "applied to the bound query's output, before the chart aggregates",
            "items": FILTER,
        },
        "order_by": {"type": "array", "items": ORDER_BY_REF},
        "limit": {
            "type": "integer",
            "default": 100,
            "minimum": 1,
            "description": "rows fetched for the chart; it is a page size, not a query LIMIT",
        },
        "options": {
            "type": "object",
            "description": (
                "cosmetic passthrough: stacked, legend_position, label_position, "
                "show_percentage, max_slices, map_type, orient, node_align, "
                "show_data_labels, show_raw_rows, max_column_values."
            ),
        },
    },
    "required": ["chart_type"],
}


# --- Phase 2 tool input schemas ----------------------------------------------

SAVE_QUERY = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "spec": QUERY_SPEC,
        "raw_operations": {
            "type": "array",
            "items": {"type": "object"},
            "description": "ADVANCED: raw v3 operations[]. Prefer spec.",
        },
        "workbook": {
            "type": "string",
            "description": "omit to create a new workbook and have its name returned",
        },
        "workbook_title": {"type": "string", "description": "title for the workbook, if one is created"},
        "use_live_connection": {"type": "boolean", "default": True},
    },
    "required": ["title"],
}

LIST_WORKBOOKS = {
    "type": "object",
    "properties": {
        "search": {"type": "string", "description": "case-insensitive substring match on the title"},
        "limit": {"type": "integer", "default": 20, "maximum": 100, "minimum": 1},
    },
}

GET_ITEM = {
    "type": "object",
    "properties": {
        "type": {"enum": ["query", "chart", "dashboard", "workbook"]},
        "name": {"type": "string"},
        "include_spec": {
            "type": "boolean",
            "default": True,
            "description": (
                "also return a QuerySpec/ChartSpec decompiled from the stored artifact. "
                "Returns null with a reason when the artifact is outside what the DSL "
                "can express -- the raw operations/config are returned either way."
            ),
        },
        "include_sample_rows": {"type": "boolean", "default": False, "description": "charts only; costs a query"},
    },
    "required": ["type", "name"],
}

DELETE_ITEM = {
    "type": "object",
    "properties": {
        # No "workbook". Insights Workbook.on_trash force-deletes every query, chart,
        # dashboard and folder inside it (`insights_workbook.py:32-46`). That is a UI
        # decision with a confirmation dialog, not a tool call.
        "type": {"enum": ["query", "chart", "dashboard", "ai_note"]},
        "name": {"type": "string"},
    },
    "required": ["type", "name"],
}

CREATE_CHART = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "name of the saved Insights Query v3 to chart"},
        "title": {"type": "string"},
        "spec": CHART_SPEC,
        "render": {
            "enum": ["auto", "force", "skip"],
            "default": "auto",
            "description": (
                "'auto' renders unless the chart pivots (split_by, or table columns) -- a "
                "pivot runs an extra query at build time to discover its column values, so "
                "the first render can be slow. 'force' renders anyway. 'skip' never renders; "
                "call update_chart(rerender_only: true) later."
            ),
        },
    },
    "required": ["query", "title", "spec"],
}

UPDATE_CHART = {
    "type": "object",
    "properties": {
        "chart": {"type": "string"},
        "title": {"type": "string"},
        "spec": CHART_SPEC,
        "rerender_only": {
            "type": "boolean",
            "default": False,
            "description": "re-run the chart against its source query without changing it",
        },
        "render": {"enum": ["auto", "force", "skip"], "default": "auto"},
    },
    "required": ["chart"],
}

DASHBOARD_ITEM = {
    "type": "object",
    "properties": {
        "type": {"enum": ["chart", "text"]},
        "chart": {"type": "string", "description": "required when type is chart"},
        "text": {"type": "string", "description": "markdown, required when type is text"},
        "width": {
            "enum": ["half", "full"],
            "default": "half",
            "description": "half is 10 of 20 grid columns; Number charts are always full width",
        },
    },
    "required": ["type"],
}

DASHBOARD_FILTER = {
    "type": "object",
    "properties": {
        "label": {"type": "string"},
        "filter_type": {"enum": ["String", "Number", "Date", "Boolean", "AsOfDate"], "default": "String"},
        "links": {
            "type": "array",
            "description": "which chart each filter drives, and on which column",
            "items": {
                "type": "object",
                "properties": {
                    "chart": {"type": "string"},
                    "column": {"type": "string"},
                    "start_column": {"type": "string", "description": "AsOfDate only"},
                    "end_column": {"type": "string", "description": "AsOfDate only"},
                },
                "required": ["chart"],
            },
        },
    },
    "required": ["label"],
}

CREATE_DASHBOARD = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "workbook": {"type": "string", "description": "defaults to the workbook of the first chart"},
        "items": {"type": "array", "items": DASHBOARD_ITEM},
        "filters": {"type": "array", "items": DASHBOARD_FILTER},
    },
    "required": ["title"],
}

UPDATE_DASHBOARD = {
    "type": "object",
    "properties": {
        "dashboard": {"type": "string"},
        "title": {"type": "string"},
        "add_items": {"type": "array", "items": DASHBOARD_ITEM},
        "remove_item_ids": {
            "type": "array",
            "items": {"type": "string"},
            "description": "the `id` values returned by get_item(type='dashboard')",
        },
        "add_filters": {"type": "array", "items": DASHBOARD_FILTER},
        "reflow": {
            "type": "boolean",
            "default": True,
            "description": "re-run the layout generator over the merged item list",
        },
    },
    "required": ["dashboard"],
}
