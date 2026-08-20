# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""Persistence and read-back: `save_query`, `list_workbooks`, `get_item`, `delete_item`.

These four are grouped by the container they act on rather than by verb. `get_item` and
`delete_item` are polymorphic over query/chart/dashboard, so they belong next to the
workbook, not next to any one leaf.
"""

import frappe
from frappe_mcp import ToolAnnotations

from insights.mcp import mcp, render
from insights.mcp.errors import ToolError, transactional
from insights.mcp.schemas import DELETE_ITEM, GET_ITEM, LIST_WORKBOOKS, SAVE_QUERY
from insights.mcp.validate import tool_args

DOCTYPES = {
    "query": "Insights Query v3",
    "chart": "Insights Chart v3",
    "dashboard": "Insights Dashboard v3",
    "workbook": "Insights Workbook",
    "ai_note": "Insights Data Doc",
}


@mcp.tool(
    name="save_query",
    input_schema=SAVE_QUERY,
    annotations=ToolAnnotations(
        title="Save a query",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=False,
    ),
)
@tool_args(SAVE_QUERY)
@transactional
def save_query(
    title: str,
    spec: dict = None,
    raw_operations: list = None,
    workbook: str = None,
    workbook_title: str = None,
    use_live_connection: bool = True,
    **_kw,
) -> str:
    """Persist a QuerySpec as an Insights Query v3 so a chart can be built on it.

    Run the spec through run_query first. This tool saves; it does not check that the
    query returns anything sensible.

    Supply exactly one of `spec` or `raw_operations`. Omit `workbook` and a new one is
    created and its name returned -- charts and dashboards must live in the same workbook
    as the queries they use.
    """
    from insights.mcp.compiler import compile
    from insights.mcp.tools.query import _validated_raw

    if bool(spec) == bool(raw_operations):
        raise ToolError(
            "Supply exactly one of `spec` or `raw_operations`.",
            fix="Prefer `spec`; `raw_operations` is the escape hatch.",
        )

    if spec:
        operations, symbols = compile(spec)
        columns = symbols.to_json()
    else:
        operations, columns = _validated_raw(raw_operations), None

    created_workbook = False
    if not workbook:
        workbook_doc = frappe.new_doc("Insights Workbook")
        workbook_doc.title = workbook_title or title
        workbook_doc.insert()
        workbook = str(workbook_doc.name)
        created_workbook = True
    else:
        workbook = str(workbook)
        frappe.has_permission("Insights Workbook", "write", doc=workbook, throw=True)

    query = frappe.new_doc("Insights Query v3")
    query.title = title
    query.workbook = workbook
    query.is_builder_query = 1
    # The doctype default is 0, which resolves tables through the warehouse. On a bench
    # with nothing imported that silently substitutes an empty temp table.
    query.use_live_connection = 1 if use_live_connection else 0
    query.operations = operations
    query.insert()

    body = [f"Saved query `{query.name}` ({title}) in workbook `{workbook}`."]
    if created_workbook:
        body.append(f"Created workbook `{workbook}` -- pass it to save_query/create_chart to reuse it.")
    if columns:
        body.append(
            "**Output columns**\n\n"
            + render.table(
                [{"name": c["name"], "data_type": c["data_type"], "role": c["role"]} for c in columns]
            )
        )
    body.append(f"Next: `create_chart(query=\"{query.name}\", ...)`.")
    return render.cap("\n\n".join(body))


@mcp.tool(
    name="list_workbooks",
    input_schema=LIST_WORKBOOKS,
    annotations=ToolAnnotations(
        title="List workbooks",
        readOnlyHint=True,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
@tool_args(LIST_WORKBOOKS)
def list_workbooks(search: str = None, limit: int = 20, **_kw) -> str:
    """List the workbooks this user can open, newest first."""
    from insights.api.workbooks import get_workbooks

    workbooks = get_workbooks(search_term=search, limit=limit)
    if not workbooks:
        return "No workbooks found. `save_query` creates one when you omit `workbook`."

    rows = [
        {
            # autoname: autoincrement -- these are integers, and every Link field that
            # points at them stores a string.
            "workbook": str(w["name"]),
            "title": w.get("title") or "",
            "owner": w.get("owner") or "",
            "modified": str(w.get("modified") or "")[:10],
        }
        for w in workbooks
    ]
    return render.cap(render.table(rows) + "\n\nNext: `get_item(type=\"workbook\", name=...)`.")


@mcp.tool(
    name="get_item",
    input_schema=GET_ITEM,
    annotations=ToolAnnotations(
        title="Read an item back",
        readOnlyHint=True,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
@tool_args(GET_ITEM)
def get_item(
    type: str,
    name: str,
    include_spec: bool = True,
    include_sample_rows: bool = False,
    **_kw,
) -> str:
    """Read a saved query, chart, dashboard or workbook back.

    With `include_spec`, a QuerySpec or ChartSpec decompiled from the stored artifact is
    returned alongside it -- edit that and pass it to save_query/update_chart. It comes
    back null with a reason when the artifact is outside what the DSL can express; the raw
    operations or config are returned either way.
    """
    doctype = DOCTYPES.get(type)
    if not doctype:
        raise ToolError(f"Unknown item type '{type}'.", spec_path="type")

    if not frappe.db.exists(doctype, name):
        raise ToolError(f"No {type} named '{name}'.", spec_path="name")

    handler = {
        "query": _get_query,
        "chart": _get_chart,
        "dashboard": _get_dashboard,
        "workbook": _get_workbook,
    }[type]
    return render.cap(handler(name, include_spec, include_sample_rows))


def _get_query(name: str, include_spec: bool, _sample: bool) -> str:
    from insights.mcp.compiler import decompile

    doc = frappe.get_doc("Insights Query v3", name)
    doc.check_permission("read")
    operations = frappe.parse_json(doc.operations) or []

    body = [
        f"### Query `{name}`",
        _meta_line({
            "title": doc.title,
            "workbook": str(doc.workbook),
            "use_live_connection": bool(doc.use_live_connection),
            "native_sql": bool(doc.is_native_query),
            "operations": len(operations),
        }),
    ]

    if include_spec:
        body.append(_spec_block(*decompile(operations), label="QuerySpec"))

    body.append("**operations**\n\n```json\n" + frappe.as_json(operations) + "\n```")
    return "\n\n".join(body)


def _get_chart(name: str, include_spec: bool, sample: bool) -> str:
    from insights.mcp import chartspec, guards

    doc = frappe.get_doc("Insights Chart v3", name)
    doc.check_permission("read")
    config = frappe.parse_json(doc.config) or {}

    data_operations = []
    if doc.data_query:
        data_operations = frappe.parse_json(
            frappe.db.get_value("Insights Query v3", doc.data_query, "operations") or "[]"
        )

    body = [
        f"### Chart `{name}`",
        _meta_line({
            "title": doc.title,
            "chart_type": doc.chart_type,
            "query": doc.query,
            "workbook": str(doc.workbook),
            "rendered": bool(data_operations),
        }),
    ]

    if not data_operations:
        body.append(
            f'_Not yet rendered -- call `update_chart(chart="{name}", rerender_only=true)`._'
        )

    if include_spec:
        body.append(_spec_block(*chartspec.decompile(doc.chart_type, config), label="ChartSpec"))

    body.append("**config**\n\n```json\n" + frappe.as_json(config) + "\n```")

    if sample and data_operations:
        result = guards.execute_saved(doc.data_query, page_size=5)
        body.append(
            "**Sample rows**\n\n"
            + render.table(result["rows"], [c["name"] for c in result["columns"]])
        )

    return "\n\n".join(body)


def _get_dashboard(name: str, _include_spec: bool, _sample: bool) -> str:
    doc = frappe.get_doc("Insights Dashboard v3", name)
    doc.check_permission("read")
    items = frappe.parse_json(doc.items) or []

    rows = []
    for item in items:
        layout = item.get("layout") or {}
        rows.append({
            "id": layout.get("i") or item.get("id") or "",
            "type": item.get("type"),
            "ref": item.get("chart") or item.get("filter_name") or (item.get("text") or "")[:40],
            "position": "x{x} y{y} {w}x{h}".format(
                x=layout.get("x", 0),
                y=layout.get("y", 0),
                w=layout.get("w", 0),
                h=layout.get("h", 0),
            ),
        })

    body = [
        f"### Dashboard `{name}`",
        _meta_line({"title": doc.title, "workbook": str(doc.workbook), "items": len(items)}),
        render.table(rows) if rows else "_(no items)_",
    ]

    links = _decoded_filter_links(items)
    if links:
        # The backtick encoding is this layer's business, in both directions. The model
        # should never have to read or write `` `query`.`column` ``.
        body.append("**Filter links**\n\n" + render.table(links))

    body.append(_sharing_line(doc))
    body.append(
        "There is no DashboardSpec: edit a dashboard with `update_dashboard`, using the "
        "`id` column above for `remove_item_ids`."
    )
    return "\n\n".join(body)


def _decoded_filter_links(items: list) -> list:
    from insights.mcp.tools.dashboard import decode_filter_link

    rows = []
    for item in items:
        if item.get("type") != "filter":
            continue
        for chart, encoded in (item.get("links") or {}).items():
            query, column = decode_filter_link(encoded)
            rows.append({
                "filter": item.get("filter_name"),
                "chart": chart,
                "query": query,
                "column": column,
            })
        for chart, span in (item.get("range_links") or {}).items():
            start = decode_filter_link(span.get("start_column"))
            end = decode_filter_link(span.get("end_column"))
            rows.append({
                "filter": item.get("filter_name"),
                "chart": chart,
                "query": start[0],
                "column": f"{start[1]} .. {end[1]}",
            })
    return rows


def _sharing_line(doc) -> str:
    if not doc.has_permission("write"):
        # as_dict only populates the sharing fields for a writer
        # (insights_dashboard_v3.py:67-76), so silence here means "cannot tell",
        # not "not shared".
        return "_sharing_state_unavailable: caller lacks write permission._"
    state = "public" if doc.is_public else "not shared publicly"
    return f"_Sharing: {state}. Public links arrive in Phase 3 (`share_dashboard`)._"


def _get_workbook(name: str, _include_spec: bool, _sample: bool) -> str:
    from insights.api import get_doc

    doc = get_doc("Insights Workbook", str(name))
    body = [f"### Workbook `{name}`", _meta_line({"title": doc.get("title")})]

    for key, label in (("queries", "Queries"), ("charts", "Charts"), ("dashboards", "Dashboards")):
        # as_dict JSON-stringifies these four fields (insights_workbook.py:230-233).
        entries = frappe.parse_json(doc.get(key) or "[]")
        if not entries:
            continue
        rows = [
            {
                "name": e.get("name"),
                "title": e.get("title") or "",
                **({"chart_type": e.get("chart_type")} if key == "charts" else {}),
            }
            for e in entries
        ]
        body.append(f"**{label}**\n\n" + render.table(rows))

    return "\n\n".join(body)


def _spec_block(spec, reason, *, label: str) -> str:
    if spec is None:
        return f"**{label}**: not available -- {reason}."
    return f"**{label}**\n\n```json\n" + frappe.as_json(spec) + "\n```"


def _meta_line(values: dict) -> str:
    return "_" + " · ".join(f"{k}: {v}" for k, v in values.items() if v not in (None, "")) + "_"


@mcp.tool(
    name="delete_item",
    input_schema=DELETE_ITEM,
    annotations=ToolAnnotations(
        title="Delete an item",
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
@tool_args(DELETE_ITEM)
@transactional
def delete_item(type: str, name: str, **_kw) -> str:
    """Delete a query, chart, dashboard or AI note.

    Workbooks cannot be deleted here: `Insights Workbook.on_trash` force-deletes every
    query, chart, dashboard and folder inside it. That is a decision for a human with a
    confirmation dialog in front of them.

    Deleting a chart also deletes its hidden data_query, which is correct -- that query
    exists only to feed the chart.
    """
    doctype = DOCTYPES.get(type)
    if not doctype:
        raise ToolError(f"Unknown item type '{type}'.", spec_path="type")

    if not frappe.db.exists(doctype, name):
        raise ToolError(f"No {type} named '{name}'.", spec_path="name")

    if type == "ai_note":
        zone = frappe.db.get_value("Insights Data Doc", name, "zone")
        if zone != "AI Note":
            raise ToolError(
                f"`{name}` is in the {zone} zone, which is human-owned.",
                spec_path="name",
                fix="Only notes written by write_ai_note can be deleted here.",
            )

    try:
        frappe.delete_doc(doctype, name)
    except frappe.LinkExistsError as exc:
        # A chart on a dashboard is linked through the dashboard's linked_charts child
        # table. Frappe's own message is HTML with desk URLs in it, which is no use to a
        # model; say what to do instead.
        raise ToolError(
            f"`{name}` is still in use and cannot be deleted yet.",
            spec_path="name",
            fix=(
                "Remove it from the dashboards that show it first "
                "(`update_dashboard(remove_item_ids=[...])`), or delete those dashboards."
            ),
        ) from exc

    extra = " Its hidden data_query went with it." if type == "chart" else ""
    return f"Deleted {type} `{name}`.{extra}"
