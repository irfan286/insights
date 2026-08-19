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
