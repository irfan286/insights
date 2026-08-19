# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""Discovery tools: what data exists, and what it means.

These are what the model calls first, so they carry two things beyond raw schema:
the documentation blocks (§5.4 rung 1 -- the primary delivery channel, because no
Claude client auto-loads MCP resources), and the deployment warnings that would
otherwise have gone in `initialize.instructions`, which `frappe_mcp` cannot serve.
"""

import frappe
from frappe_mcp import ToolAnnotations

from insights.mcp import mcp, render
from insights.mcp.errors import ToolError
from insights.mcp.schemas import (
    DESCRIBE_TABLE,
    DISTINCT_VALUES,
    LIST_DATA_SOURCES,
    LIST_TABLES,
)
from insights.mcp.validate import tool_args

# FIELDTYPES.MEASURE (frontend/src2/helpers/constants.ts:11-19)
MEASURE_TYPES = ("Integer", "Decimal")


def _role(data_type: str) -> str:
    return "measure" if data_type in MEASURE_TYPES else "dimension"


@mcp.tool(
    name="list_data_sources",
    input_schema=LIST_DATA_SOURCES,
    annotations=ToolAnnotations(
        title="List data sources",
        readOnlyHint=True,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
@tool_args(LIST_DATA_SOURCES)
def list_data_sources(**_kw) -> str:
    """List the connected data sources you can query, and which have documentation.

    Start here. Then call get_docs(data_source) before writing your first query against
    a source -- queries written without the documentation are frequently wrong in ways
    that look correct.
    """
    sources = frappe.get_list(
        "Insights Data Source v3",
        filters={"status": "Active"},
        fields=["name", "title", "database_type", "is_site_db"],
        order_by="creation asc",
    )
    if not sources:
        return "No data sources are available to you."

    documented = _documented_counts(sources)
    table_counts = _table_counts(sources)

    rows = []
    for source in sources:
        docs = documented.get(source.name, 0)
        rows.append({
            "data_source": source.name,
            "type": source.database_type,
            "tables": table_counts.get(source.name, 0),
            "documented": f"{docs} block(s)" if docs else "none",
        })

    body = [render.table(rows, ["data_source", "type", "tables", "documented"])]

    # §8.3 / §5.4 rung 5 option 1: the deployment warnings that would otherwise have
    # gone in initialize.instructions, which upstream cannot serve. Placed here because
    # this is the first tool called in every flow, so a human reading the transcript
    # sees it. The model does not need it; the operator does.
    warnings = _deployment_warnings(sources)
    if warnings:
        body.append("**Notes about this deployment**\n\n" + "\n".join(f"- {w}" for w in warnings))

    body.append(
        "Next: `get_docs(data_source)` for what the tables mean, then "
        "`list_tables(data_source)` and `describe_table(...)`."
    )
    return render.cap("\n\n".join(body))


def _deployment_warnings(sources) -> list[str]:
    warnings = []

    if not frappe.db.get_single_value("Insights Settings", "enable_permissions"):
        warnings.append(
            "Table-level access control is OFF on this deployment "
            "(`Insights Settings.enable_permissions` is unset), so every table listed "
            "here is readable by this identity."
        )

    unsynced = frappe.db.count("Insights Table v3", {"stored": 1})
    if not unsynced:
        warnings.append(
            "No tables are synced to the warehouse, so queries run against the live "
            "database. `use_live_connection` defaults to true and should stay true."
        )

    if any(s.is_site_db for s in sources):
        warnings.append(
            "One of these sources is the Frappe site database itself, which contains "
            "user records and access logs alongside business data."
        )

    return warnings


def _documented_counts(sources) -> dict:
    """Counted per source rather than with a GROUP BY: Frappe 16 rejects a raw
    "count(name) as total" string in `fields`, and there are only a handful of
    sources, so N cheap COUNTs beat building qb aggregate syntax here."""
    if not frappe.db.table_exists("Insights Data Doc"):
        return {}
    return {
        s.name: frappe.db.count("Insights Data Doc", {"data_source": s.name, "status": "Active"})
        for s in sources
    }


def _table_counts(sources) -> dict:
    return {s.name: frappe.db.count("Insights Table v3", {"data_source": s.name}) for s in sources}


@mcp.tool(
    name="list_tables",
    input_schema=LIST_TABLES,
    annotations=ToolAnnotations(
        title="List tables",
        readOnlyHint=True,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
@tool_args(LIST_TABLES)
def list_tables(data_source: str, search: str = None, limit: int = 50, start: int = 0, **_kw) -> str:
    """List the tables in a data source, with a one-line purpose where documented.

    Use `search` to narrow by name -- some sources have hundreds of tables. A table
    listed here is not necessarily synced to the warehouse; `warehouse_ready: no` means
    it will be read live, which is the normal case.
    """
    _require_source(data_source)

    filters = {"data_source": data_source}
    if search:
        filters["table"] = ["like", f"%{search}%"]

    total = frappe.db.count("Insights Table v3", filters)
    tables = frappe.get_list(
        "Insights Table v3",
        filters=filters,
        fields=["table", "label", "stored", "last_synced_on"],
        order_by="table asc",
        limit=limit,
        start=start,
    )
    if not tables:
        hint = f" matching '{search}'" if search else ""
        raise ToolError(
            f"No tables found in '{data_source}'{hint}.",
            fix="Call list_data_sources to confirm the name, or drop the search term.",
        )

    purposes = _table_purposes(data_source, [t["table"] for t in tables])
    rows = [
        {
            "table_name": t["table"],
            "warehouse_ready": "yes" if t["stored"] else "no",
            "purpose": purposes.get(t["table"], ""),
        }
        for t in tables
    ]

    header = f"{len(tables)} of {total} tables in `{data_source}`"
    if start:
        header += f" (from {start})"
    body = [header, render.table(rows, ["table_name", "warehouse_ready", "purpose"])]

    shown = start + len(tables)
    if shown < total:
        body.append(
            f"{total - shown} more. Call again with `start: {shown}`, or pass `search` "
            f"to narrow by name."
        )
    body.append("Next: `describe_table(data_source, table_name)` for columns and joins.")
    return render.cap("\n\n".join(body))


def _table_purposes(data_source: str, tables: list[str]) -> dict:
    """Tier-1 documentation: one line per table, free because we are here anyway."""
    if not tables or not frappe.db.table_exists("Insights Data Doc"):
        return {}
    rows = frappe.get_all(
        "Insights Data Doc",
        filters={
            "data_source": data_source,
            "scope": "Table",
            "status": "Active",
            "table_name": ["in", tables],
        },
        fields=["table_name", "summary", "title"],
    )
    out = {}
    for row in rows:
        if row["table_name"] not in out:
            out[row["table_name"]] = (row["summary"] or row["title"] or "")[:100]
    return out


@mcp.tool(
    name="describe_table",
    input_schema=DESCRIBE_TABLE,
    annotations=ToolAnnotations(
        title="Describe table",
        readOnlyHint=True,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
@tool_args(DESCRIBE_TABLE)
def describe_table(
    data_source: str,
    table_name: str,
    include_joins: bool = True,
    include_docs: bool = True,
    include_preview: bool = False,
    **_kw,
) -> str:
    """Columns, joinable tables and curated documentation for one table.

    Call this before writing any query against a table you have not queried in this
    conversation.

    `joins: []` means UNKNOWN, not "no joins exist" -- Insights only auto-discovers
    links for Frappe databases. For other sources the correct joins, if documented,
    appear in the DOCUMENTATION block.
    """
    from insights.api.data_sources import get_data_source_table_columns

    _require_source(data_source)
    try:
        columns = get_data_source_table_columns(data_source, table_name)
    except Exception as exc:
        raise ToolError(
            f"Could not read '{table_name}' in '{data_source}'.",
            fix="Check the name with list_tables.",
        ) from exc

    # `get_data_source_table_columns` emits key `column`, not `name` -- `name` belongs
    # to the query *result* shape (get_columns_from_schema). See §8 N.
    rows = [
        {"name": c["column"], "data_type": c.get("type"), "role": _role(c.get("type"))}
        for c in columns
    ]
    body = [
        f"### `{table_name}` in `{data_source}`",
        render.table(rows, ["name", "data_type", "role"]),
    ]

    if include_joins:
        body.append(_joins_section(data_source, table_name))

    if include_docs:
        docs = _docs_section(data_source, table_name)
        if docs:
            body.append(docs)

    if include_preview:
        body.append(_preview_section(data_source, table_name))

    body.append(
        "Column list is cached per worker with no expiry; call "
        "`update_data_source_tables` in Insights if the schema has changed."
    )
    return render.cap("\n\n".join(body))


def _joins_section(data_source: str, table_name: str) -> str:
    links = frappe.get_all(
        "Insights Table Link v3",
        or_filters={"left_table": table_name, "right_table": table_name},
        filters={"data_source": data_source},
        fields=["left_table", "left_column", "right_table", "right_column"],
        limit=50,
    )
    if not links:
        return (
            "**Joins:** none recorded. This means UNKNOWN, not none — Insights only "
            "auto-discovers links for Frappe databases. Check the documentation below, "
            "or ask which columns relate these tables."
        )
    rows = [
        {
            "other_table": l["right_table"] if l["left_table"] == table_name else l["left_table"],
            "this_column": l["left_column"] if l["left_table"] == table_name else l["right_column"],
            "other_column": l["right_column"] if l["left_table"] == table_name else l["left_column"],
        }
        for l in links
    ]
    return "**Joins**\n\n" + render.table(rows, ["other_table", "this_column", "other_column"])


def _docs_section(data_source: str, table_name: str) -> str:
    try:
        from insights.mcp import docs

        composed = docs.compose(data_source, table_name)
    except Exception:
        return ""
    return docs.render_blocks(composed) if composed else ""


def _preview_section(data_source: str, table_name: str) -> str:
    from insights.mcp.guards import execute_transient

    ops = [
        {"type": "source", "table": {"type": "table", "data_source": data_source, "table_name": table_name}},
        {"type": "limit", "limit": 5},
    ]
    try:
        result = execute_transient(ops, page_size=5, title=f"MCP preview {table_name}")
    except ToolError:
        raise
    except Exception:
        return "**Preview:** unavailable."
    return "**Preview (5 rows)**\n\n" + render.table(
        result["rows"], [c["name"] for c in result["columns"]], cell_limit=40
    )


@mcp.tool(
    name="distinct_values",
    input_schema=DISTINCT_VALUES,
    annotations=ToolAnnotations(
        title="Distinct column values",
        readOnlyHint=True,
        idempotentHint=False,
        openWorldHint=True,
    ),
)
@tool_args(DISTINCT_VALUES)
def distinct_values(
    column_name: str,
    data_source: str = None,
    table_name: str = None,
    saved_query: str = None,
    search: str = None,
    limit: int = 20,
    **_kw,
) -> str:
    """The real values in a category column, with row counts.

    Use this before writing a filter on a column whose values you are guessing at --
    status codes and category names are rarely what they sound like.

    Supply either data_source + table_name, or saved_query.
    """
    if saved_query:
        return _distinct_from_query(saved_query, column_name, search, limit)

    if not data_source or not table_name:
        raise ToolError(
            "Supply either `data_source` + `table_name`, or `saved_query`.",
            spec_path="table_name",
        )

    from insights.mcp.compiler import compile
    from insights.mcp.guards import execute_transient

    spec = {
        "from": {"data_source": data_source, "table": table_name},
        "group_by": [{"column": column_name}],
        "aggregate": [{"fn": "count"}],
        "sort": [{"column": "count_of_rows", "desc": True}],
        "limit": min(int(limit), 100),
    }
    if search:
        spec["where"] = [{"column": column_name, "op": "contains", "value": str(search)}]

    ops, _symbols = compile(spec)
    result = execute_transient(ops, page_size=min(int(limit), 100), title=f"MCP distinct {column_name}")

    if not result["rows"]:
        return f"`{column_name}` in `{table_name}` has no values matching that search."
    return render.cap(
        f"Distinct values of `{column_name}` in `{table_name}`, most common first:\n\n"
        + render.table(result["rows"], [column_name, "count_of_rows"])
    )


def _distinct_from_query(saved_query: str, column_name: str, search, limit: int) -> str:
    doc = frappe.get_doc("Insights Query v3", saved_query)
    doc.check_permission("read")
    try:
        values = doc.get_distinct_column_values(column_name, search_text=search, limit=limit)
    except Exception as exc:
        raise ToolError(
            f"Could not read distinct values of '{column_name}' from query '{saved_query}'.",
        ) from exc
    rows = [{column_name: v.get("value") if isinstance(v, dict) else v} for v in (values or [])]
    if not rows:
        return f"`{column_name}` has no values in query `{saved_query}`."
    return render.cap(
        f"Distinct values of `{column_name}` in query `{saved_query}`:\n\n"
        + render.table(rows, [column_name])
    )


def _require_source(data_source: str) -> None:
    if not frappe.db.exists("Insights Data Source v3", data_source):
        available = frappe.get_list("Insights Data Source v3", pluck="name", limit=20)
        raise ToolError(
            f"No data source called '{data_source}'.",
            spec_path="data_source",
            valid_columns=available,
            fix="Call list_data_sources.",
        )
    frappe.has_permission("Insights Data Source v3", "read", doc=data_source, throw=True)
