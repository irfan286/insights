# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""The single choke point for transient query execution.

`run_query` executes a query that is never saved. That path fires NO permission hook:
`get_permission_query_conditions` and `has_doc_permission` run on `frappe.get_list` /
`frappe.get_doc` / `check_permission`, none of which a transient doc touches. Compare
`insights.api.run_doc_method`, which guards with `doc.check_permission("read")` first
(`insights/api/__init__.py:197-212`) -- we have no equivalent, so we build one.

  >>> HONEST SCOPE ON THIS DEPLOYMENT <<<

`Insights Settings.enable_permissions` is 0 here. `_build_table_permission_query`
(`insights/permissions.py:147-150`) returns EVERY `Insights Table v3` row when that
flag is falsy, so the per-table check below evaluates to True for every table and
every user. It gates role membership and nothing else.

That is not a reason to skip it. It is the one greppable place to tighten when the
flag is turned on, and non-negotiable #3 requires every transient execute to route
through here. But do NOT read "choke point" as "table access is controlled" -- today
any caller with an Insights role can read all ~1,139 tables, including Site DB's
`tabUser` and `tabOAuth Bearer Token`. Say so plainly in any security review.

Row-level restrictions DO still apply: `apply_user_permissions` is 1, and
`InsightsTablev3.get_ibis_table` applies both `apply_table_restrictions` and
`apply_user_permissions` on every path.
"""

import frappe

from insights.insights.query_utils import extract_table_deps_from_operations
from insights.mcp.errors import ToolError, as_tool_error, capture_build_diagnostics

DOCTYPE = "Insights Query v3"


def build_transient(
    operations: list[dict],
    *,
    use_live_connection: bool = True,
    workbook: str | None = None,
    title: str = "MCP Query",
    spec_paths: dict | None = None,
):
    """Compile operations into an ibis expression WITHOUT fetching rows.

    Used by `dry_run` and by `distinct_values`. "Does not return rows" is the only
    guarantee -- it is not free. `build()` resolves sources through
    `InsightsTablev3.get_ibis_table`, and `apply_pivot` runs an eager `.execute()`
    mid-build (`ibis_utils.py:572`), which is why `dry_run` must refuse `pivot_on`.

    With `use_live_connection=True` (our default) the warehouse path is skipped
    entirely, so no import is enqueued -- see docs/mcp-IMPLEMENTATION.md §8 A.
    """
    doc = _transient_doc(operations, use_live_connection=use_live_connection,
                         workbook=workbook, title=title)
    with capture_build_diagnostics() as captured:
        try:
            return doc.build()
        except Exception as exc:
            raise as_tool_error(exc, spec_paths=spec_paths, captured=captured) from exc


def execute_transient(
    operations: list[dict],
    *,
    use_live_connection: bool = True,
    workbook: str | None = None,
    title: str = "MCP Query",
    page: int = 1,
    page_size: int = 100,
    force: bool = False,
    adhoc_filters: dict | None = None,
    spec_paths: dict | None = None,
) -> dict:
    """The ONLY place insights/mcp/ may execute a transient Insights Query v3.

    Returns InsightsQueryv3.execute()'s dict: {sql, columns, rows, time_taken,
    is_aggregated_sql}. Enforced by insights/tests/mcp/test_guards.py, which fails the
    build if `.execute(` appears anywhere else under insights/mcp/.
    """
    doc = _transient_doc(operations, use_live_connection=use_live_connection,
                         workbook=workbook, title=title)
    with capture_build_diagnostics() as captured:
        try:
            return doc.execute(
                adhoc_filters=adhoc_filters,
                force=force,
                page=page,
                page_size=page_size,
            )
        except Exception as exc:
            raise as_tool_error(exc, spec_paths=spec_paths, captured=captured) from exc


def _transient_doc(operations, *, use_live_connection, workbook, title):
    """Build an in-memory doc, check access, and return it. Never inserted."""
    if not operations:
        raise ToolError("The query has no operations.", fix="Supply a spec with a `from` clause.")

    _check_access(operations, workbook)

    doc = frappe.new_doc(DOCTYPE)
    # `name` must be set explicitly. `build()` keys the circular-reference guard on
    # `self.doc.name` (insights_query_v3.py:96-100) and `execute_ibis_query` takes
    # `reference_name=self.name`, so a None name degrades the guard and leaves the
    # Insights Query Execution Log row unattributable. The `mcp-` prefix is the audit
    # trail: every MCP-originated execution is greppable in that log.
    doc.name = f"mcp-{frappe.generate_hash(length=10)}"
    doc.title = title
    doc.workbook = workbook
    doc.use_live_connection = 1 if use_live_connection else 0
    doc.operations = frappe.as_json(operations)
    doc.flags.insights_mcp_transient = True
    return doc


def _check_access(operations: list[dict], workbook: str | None) -> None:
    _require(DOCTYPE, "read")

    if workbook:
        _require("Insights Workbook", "read", doc=str(workbook))

    for dep in resolved_tables(operations):
        _check_table(dep["data_source"], dep["table_name"])


def resolved_tables(operations: list[dict]) -> list[dict]:
    """Derive the (data_source, table_name) pairs this query reads.

    Derived here rather than accepted as an argument on purpose: a caller that forgot
    to list a table would otherwise skip its check silently, which is exactly the
    failure mode a choke point exists to prevent.
    """
    return extract_table_deps_from_operations(operations or [])


def _check_table(data_source: str, table_name: str) -> None:
    from insights.insights.doctype.insights_table_v3.insights_table_v3 import get_table_name

    _require("Insights Data Source v3", "read", doc=data_source)

    name = get_table_name(data_source, table_name)  # md5(ds + table)[:10]
    if frappe.db.exists("Insights Table v3", name):
        _require("Insights Table v3", "read", doc=name)
    else:
        # `datalake` carries 846 tables and the Insights Table v3 index can lag the
        # remote schema. has_permission(doc=<missing>) raises DoesNotExistError, which
        # is a worse diagnostic for the model than a plain doctype-level check.
        _require("Insights Table v3", "read")


def _require(doctype: str, ptype: str, doc: str | None = None) -> None:
    try:
        frappe.has_permission(doctype, ptype, doc=doc, throw=True)
    except frappe.PermissionError as exc:
        raise ToolError(
            f"You do not have {ptype} access to {doctype}" + (f" '{doc}'." if doc else "."),
            fix="Ask an Insights administrator for access, or use a different data source.",
        ) from exc


def execute_saved(
    name: str,
    *,
    page: int = 1,
    page_size: int = 100,
    force: bool = False,
    adhoc_filters: dict | None = None,
) -> dict:
    """Execute a PERSISTED Insights Query v3 by name.

    Distinct from `execute_transient`: `frappe.get_doc` fires the permission hooks that
    the transient path bypasses, so `check_permission` here is real enforcement rather
    than the structural placeholder above.

    It lives in this module anyway so that `insights/mcp/` has exactly ONE file
    containing `.execute(`. The AST guard in test_guards.py is deliberately blunt --
    a rule that needs a clever exception is a rule that erodes.
    """
    doc = frappe.get_doc(DOCTYPE, name)
    doc.check_permission("read")

    with capture_build_diagnostics() as captured:
        try:
            return doc.execute(
                adhoc_filters=adhoc_filters, force=force, page=page, page_size=page_size
            )
        except Exception as exc:
            raise as_tool_error(exc, captured=captured) from exc


def build_saved(name: str):
    """The no-rows sibling of `execute_saved`, for `dry_run` on a saved query."""
    doc = frappe.get_doc(DOCTYPE, name)
    doc.check_permission("read")
    with capture_build_diagnostics() as captured:
        try:
            return doc.build()
        except Exception as exc:
            raise as_tool_error(exc, captured=captured) from exc
