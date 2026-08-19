# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""`run_query` -- compile a QuerySpec and execute it."""

import frappe
from frappe_mcp import ToolAnnotations

from insights.mcp import mcp, render
from insights.mcp.errors import ToolError
from insights.mcp.schemas import RUN_QUERY
from insights.mcp.validate import tool_args

# Operations we refuse to accept through raw_operations. `code` is non-negotiable #9
# (safe_exec policy unreviewed, open question #16). `sql` is an arbitrary-SQL surface
# that belongs behind an opt-in toolset, not the default one.
FORBIDDEN_RAW_TYPES = ("code", "sql")
DISPATCHED_TYPES = (
    "source", "join", "union", "filter", "filter_group", "select", "rename", "remove",
    "mutate", "cast", "summarize", "order_by", "limit", "pivot_wider",
    "custom_operation",
)


@mcp.tool(
    name="run_query",
    input_schema=RUN_QUERY,
    annotations=ToolAnnotations(
        title="Run query",
        # No documents are created -- that is what the client's permission prompt is
        # about. It is not side-effect free, hence idempotentHint=False.
        readOnlyHint=True,
        idempotentHint=False,
        openWorldHint=True,
    ),
)
@tool_args(RUN_QUERY)
def run_query(
    spec: dict = None,
    raw_operations: list = None,
    saved_query: str = None,
    dry_run: bool = False,
    workbook: str = None,
    use_live_connection: bool = True,
    page: int = 1,
    page_size: int = 20,
    force: bool = False,
    include_sql: bool = False,
    include_count: bool = False,
    include_operations: bool = False,
    **_kw,
) -> str:
    """Compile a QuerySpec and execute it, returning rows.

    Call get_docs(data_source) and describe_table(...) before your first query against
    a source. Queries written without them are frequently wrong in ways that look
    correct -- status codes, join keys and deprecated tables are not guessable.

    Supply exactly one of: spec (preferred) | raw_operations | saved_query.

    dry_run returns the SQL and output columns WITHOUT rows. It is not free: it still
    resolves the tables and writes an execution log row.

    Nothing is saved. This creates no query, chart or dashboard.
    """
    from insights.mcp.compiler import compile
    from insights.mcp.guards import build_transient, execute_transient

    supplied = [n for n, v in (("spec", spec), ("raw_operations", raw_operations), ("saved_query", saved_query)) if v]
    if len(supplied) != 1:
        raise ToolError(
            f"Supply exactly one of spec, raw_operations or saved_query — got {len(supplied)}"
            + (f" ({', '.join(supplied)})" if supplied else ""),
            fix="Use `spec` unless you need union, custom_operation or nested filter groups.",
        )

    if saved_query:
        return _run_saved(saved_query, page=page, page_size=page_size, force=force,
                          dry_run=dry_run, include_sql=include_sql)

    symbols = None
    if spec:
        operations, symbols = compile(spec)
    else:
        operations = _validated_raw(raw_operations)

    if dry_run:
        if any(o.get("type") == "pivot_wider" for o in operations):
            raise ToolError(
                "dry_run cannot preview a pivot: building one requires reading the "
                "pivot column's distinct values, which executes a query.",
                fix="Run without dry_run, or remove pivot_on.",
            )
        expr = build_transient(
            operations, use_live_connection=use_live_connection, workbook=workbook,
            spec_paths=symbols.spec_paths if symbols else None,
        )
        return _render_dry_run(expr, operations, include_operations)

    result = execute_transient(
        operations,
        use_live_connection=use_live_connection,
        workbook=workbook,
        page=page,
        page_size=page_size,
        force=force,
        spec_paths=symbols.spec_paths if symbols else None,
    )
    return _render_result(
        result, operations,
        include_sql=include_sql, include_count=include_count,
        include_operations=include_operations, use_live_connection=use_live_connection,
        page=page, page_size=page_size,
    )


def _validated_raw(raw_operations) -> list:
    """`perform_operation` silently no-ops an unknown type (ibis_utils.py:157), so an
    unvalidated escape hatch produces successful wrong answers."""
    if not isinstance(raw_operations, list) or not raw_operations:
        raise ToolError("`raw_operations` must be a non-empty array.", spec_path="raw_operations")

    for i, op in enumerate(raw_operations):
        if not isinstance(op, dict):
            raise ToolError(f"raw_operations[{i}] is not an object.", spec_path=f"raw_operations[{i}]")
        op_type = op.get("type")
        if op_type in FORBIDDEN_RAW_TYPES:
            raise ToolError(
                f"The '{op_type}' operation is not available through this server.",
                spec_path=f"raw_operations[{i}].type",
                fix="Use `spec`, or a saved query built in the Insights UI.",
            )
        if op_type not in DISPATCHED_TYPES:
            raise ToolError(
                f"Unknown operation type '{op_type}'. The backend would silently skip it.",
                spec_path=f"raw_operations[{i}].type",
                valid_columns=DISPATCHED_TYPES,
            )
    return raw_operations


def _run_saved(name, *, page, page_size, force, dry_run, include_sql) -> str:
    # Routed through guards even though a saved query fires the real permission hooks,
    # so that insights/mcp/ has exactly one module containing `.execute(`.
    from insights.mcp.guards import build_saved, execute_saved

    if dry_run:
        operations = frappe.parse_json(
            frappe.db.get_value("Insights Query v3", name, "operations")
        ) or []
        return _render_dry_run(build_saved(name), operations, False)

    live = bool(frappe.db.get_value("Insights Query v3", name, "use_live_connection"))
    result = execute_saved(name, page=page, page_size=page_size, force=force)
    return _render_result(result, [], include_sql=include_sql, include_count=False,
                          include_operations=False, use_live_connection=live,
                          page=page, page_size=page_size)


def _render_dry_run(expr, operations, include_operations: bool) -> str:
    import ibis

    from insights.insights.doctype.insights_data_source_v3.ibis_utils import (
        get_columns_from_schema,
    )

    columns = get_columns_from_schema(expr.schema())
    body = [
        "**Dry run — no rows returned.**",
        "**Output columns**\n\n" + render.table(columns, ["name", "type"]),
    ]
    try:
        body.append("**SQL**\n\n```sql\n" + ibis.to_sql(expr).strip() + "\n```")
    except Exception:
        body.append("_(SQL could not be rendered)_")
    if include_operations:
        body.append("**Operations**\n\n```json\n" + frappe.as_json(operations) + "\n```")
    return render.cap("\n\n".join(body))


def _render_result(result, operations, *, include_sql, include_count, include_operations,
                   use_live_connection, page, page_size) -> str:
    columns = result.get("columns") or []
    rows = result.get("rows") or []
    names = [c["name"] for c in columns]

    body = [render.table(rows, names)]

    meta = [f"{len(rows)} row(s)"]
    if page > 1:
        meta.append(f"page {page}")
    # `time_taken == -1` is the cache-hit sentinel (ibis_utils.py:979-980).
    if result.get("time_taken") == -1:
        meta.append("from cache — pass `force: true` to bypass")
    elif result.get("time_taken") is not None:
        meta.append(f"{result['time_taken']}s")
    body.append("_" + " · ".join(meta) + "_")

    if len(rows) == page_size:
        body.append(f"There may be more rows. Request `page: {page + 1}`.")

    if not rows:
        body.append(_zero_row_diagnosis(operations, use_live_connection))

    if include_count:
        body.append("_(include_count is not implemented yet; it costs a second round trip)_")
    if include_sql and result.get("sql"):
        body.append("**SQL**\n\n```sql\n" + str(result["sql"]).strip() + "\n```")
    if include_operations and operations:
        body.append("**Operations**\n\n```json\n" + frappe.as_json(operations) + "\n```")

    return render.cap("\n\n".join(body))


def _zero_row_diagnosis(operations, use_live_connection: bool) -> str:
    """§4.5: the highest-severity correctness trap available.

    An un-synced warehouse table resolves to an EMPTY temp table rather than an error
    (`data_warehouse.py:322-332`), so the query succeeds and returns nothing. On this
    deployment nothing is synced, which makes it the likeliest cause of a zero-row
    result whenever live mode is off.
    """
    if use_live_connection:
        return "No rows matched. Check the filter values with `distinct_values`."

    from insights.mcp.guards import resolved_tables

    unsynced = []
    for dep in resolved_tables(operations):
        stored = frappe.db.get_value(
            "Insights Table v3",
            {"data_source": dep["data_source"], "table": dep["table_name"]},
            "stored",
        )
        if not stored:
            unsynced.append(dep["table_name"])

    if unsynced:
        return (
            f"No rows — and {', '.join(unsynced)} is not synced to the warehouse, so an "
            f"empty table was substituted. **Retry with `use_live_connection: true`.**"
        )
    return "No rows matched. Check the filter values with `distinct_values`."
