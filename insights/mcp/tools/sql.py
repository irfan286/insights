# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""`run_sql` -- execute one read-only native SQL statement against a data source.

The QuerySpec compiler covers the common shapes and keeps the model on rails, but it
cannot express everything a real question needs: window functions, recursive CTEs,
backend-specific functions, correlated subqueries. This is the escape hatch for those.

It is a separate tool rather than a `sql` field on `run_query` because the two have
different risk profiles, and a model choosing between tools reads the descriptions. Note
that `run_query` deliberately refuses `sql` through `raw_operations` (FORBIDDEN_RAW_TYPES)
-- that refusal stays, so this module is the only way in and the only place to audit.
"""

import frappe
from frappe_mcp import ToolAnnotations

from insights.mcp import mcp
from insights.mcp.errors import ToolError
from insights.mcp.schemas import RUN_SQL
from insights.mcp.validate import tool_args


@mcp.tool(
    name="run_sql",
    input_schema=RUN_SQL,
    annotations=ToolAnnotations(
        title="Run native SQL",
        # readOnlyHint is truthful only because assert_read_only refuses writes. If an
        # administrator turns on Allow MCP SQL Writes that stops being true, which is
        # part of why the setting's description says so in those words.
        readOnlyHint=True,
        idempotentHint=False,
        openWorldHint=True,
    ),
)
@tool_args(RUN_SQL)
def run_sql(
    data_source: str = None,
    sql: str = None,
    dry_run: bool = False,
    workbook: str = None,
    use_live_connection: bool = True,
    page: int = 1,
    page_size: int = 20,
    force: bool = False,
    **_kw,
) -> str:
    """Run ONE read-only SQL statement against a data source and return rows.

    Use this when QuerySpec cannot express the query -- window functions, recursive
    CTEs, backend-specific functions. Prefer run_query otherwise: it validates column
    names against the real schema and gives better errors when something is wrong.

    Call get_docs(data_source) and describe_table(...) first. Table and column names are
    not guessable, and a syntactically valid query against the wrong column is the
    expensive kind of mistake.

    Write statements are refused. So are multiple statements. SELECT ... INTO and a
    DELETE hidden inside a CTE are both caught -- do not try to route around this.

    The SQL runs in the data source's own dialect when use_live_connection is true. With
    it false the statement is transpiled to DuckDB, which does not always survive
    backend-specific syntax.

    Nothing is saved. To keep the result, pass the same sql to save_query.
    """
    from insights.mcp.guards import build_transient, execute_transient
    from insights.mcp.sqlguard import assert_read_only, dialect_for
    from insights.mcp.tools.query import _render_dry_run, _render_result

    operations = build_sql_operations(data_source, sql)

    assert_read_only(sql, dialect=dialect_for(data_source))

    if dry_run:
        expr = build_transient(
            operations, use_live_connection=use_live_connection, workbook=workbook,
        )
        return _render_dry_run(expr, operations, False)

    result = execute_transient(
        operations,
        use_live_connection=use_live_connection,
        workbook=workbook,
        title="MCP SQL",
        page=page,
        page_size=page_size,
        force=force,
    )
    return _render_result(
        result, operations,
        include_sql=False, include_count=False, include_operations=False,
        use_live_connection=use_live_connection, page=page, page_size=page_size,
    )


def build_sql_operations(data_source: str, sql: str) -> list[dict]:
    """The one place a native-SQL operations[] is constructed.

    Shape mirrors the frontend's SQLArgs exactly (`frontend/src2/types/query.types.ts`:
    `{ raw_sql, data_source }`), so a query saved here opens in the SQL editor like any
    other native query.
    """
    if not data_source:
        raise ToolError("`data_source` is required.", spec_path="data_source")
    if not sql or not sql.strip():
        raise ToolError("`sql` is required.", spec_path="sql")

    if not frappe.db.exists("Insights Data Source v3", data_source):
        raise ToolError(
            f"No data source named '{data_source}'.",
            spec_path="data_source",
            fix="Call list_data_sources to see what exists.",
        )

    return [{"type": "sql", "raw_sql": sql.strip(), "data_source": data_source}]
