# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""`create_dashboard` and `update_dashboard`.

Two things here exist purely to keep a failure mode away from the model.

**Layout.** The caller says "half"; `insights.mcp.layout` produces the grid coordinates.

**Filter links.** A dashboard filter points at a chart's column through a string encoded
as `` `<query>`.`<column>` ``, matched by an anchored regex at
`insights_dashboard_v3.py:107-124`. A malformed link does not raise -- it silently
disables the filter AND blocks `get_distinct_column_values` on it. So the encoding is
built here, from a `{chart, column}` pair, and the column is verified to exist on the
chart's SOURCE query (not its hidden data_query) before anything is written.
"""

import re

import frappe
from frappe_mcp import ToolAnnotations

from insights.mcp import layout, mcp, render
from insights.mcp.errors import ToolError, transactional
from insights.mcp.schemas import CREATE_DASHBOARD, UPDATE_DASHBOARD
from insights.mcp.validate import tool_args

DOCTYPE = "Insights Dashboard v3"
LINK_PATTERN = re.compile(r"^`([^`]+)`\.`([^`]+)`$")


def encode_filter_link(query: str, column: str) -> str:
    return f"`{query}`.`{column}`"


def decode_filter_link(value: str) -> tuple[str | None, str | None]:
    match = LINK_PATTERN.match(value or "")
    return match.groups() if match else (None, None)


@mcp.tool(
    name="create_dashboard",
    input_schema=CREATE_DASHBOARD,
    annotations=ToolAnnotations(
        title="Create a dashboard",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=False,
    ),
)
@tool_args(CREATE_DASHBOARD)
@transactional
def create_dashboard(
    title: str,
    workbook: str = None,
    items: list = None,
    filters: list = None,
    **_kw,
) -> str:
    """Assemble saved charts into a dashboard and return its URL.

    Charts must already exist (`create_chart`) and must all live in one workbook -- a
    dashboard cannot span workbooks. Layout is generated for you: say "half" or "full".
    """
    items = items or []
    filters = filters or []
    if not items and not filters:
        raise ToolError(
            "A dashboard needs at least one item.",
            spec_path="items",
            fix='Pass items like [{"type": "chart", "chart": "<name>"}].',
        )

    charts = _resolve_charts(items, filters)
    workbook = str(workbook or _workbook_of(charts))
    frappe.has_permission("Insights Workbook", "write", doc=workbook, throw=True)
    _assert_same_workbook(charts, workbook)

    built = _build_items(items, charts) + _build_filters(filters, charts, existing_names=set())
    layout.reflow(built, chart_types={name: c.chart_type for name, c in charts.items()})

    dashboard = frappe.new_doc(DOCTYPE)
    dashboard.title = title
    dashboard.workbook = workbook
    dashboard.items = built
    dashboard.insert()

    return render.cap("\n\n".join([
        f"Created dashboard `{dashboard.name}` ({title}) in workbook `{workbook}` "
        f"with {len(built)} item(s).",
        _items_table(built),
        _url_note(dashboard.name),
    ]))


@mcp.tool(
    name="update_dashboard",
    input_schema=UPDATE_DASHBOARD,
    annotations=ToolAnnotations(
        title="Update a dashboard",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=False,
    ),
)
@tool_args(UPDATE_DASHBOARD)
@transactional
def update_dashboard(
    dashboard: str,
    title: str = None,
    add_items: list = None,
    remove_item_ids: list = None,
    add_filters: list = None,
    reflow: bool = True,
    **_kw,
) -> str:
    """Add charts, text or filters to an existing dashboard, or remove items from it.

    One write: the merged item list is laid out and saved in a single save, because
    `items` is a single JSON field and a per-item write would lose the others.
    Use the `id` values from `get_item(type="dashboard")` for `remove_item_ids`.
    """
    add_items = add_items or []
    add_filters = add_filters or []
    remove_item_ids = set(remove_item_ids or [])

    doc = frappe.get_doc(DOCTYPE, dashboard)
    doc.check_permission("write")

    current = frappe.parse_json(doc.items) or []
    kept = [i for i in current if (i.get("layout") or {}).get("i") not in remove_item_ids]
    removed = len(current) - len(kept)
    if remove_item_ids and not removed:
        raise ToolError(
            "None of those item ids are on this dashboard.",
            spec_path="remove_item_ids",
            valid_columns=[(i.get("layout") or {}).get("i") for i in current],
        )

    charts = _resolve_charts(add_items, add_filters)
    _assert_same_workbook(charts, str(doc.workbook))

    existing_names = {i.get("filter_name") for i in kept if i.get("type") == "filter"}
    merged = kept + _build_items(add_items, charts) + _build_filters(
        add_filters, charts, existing_names=existing_names
    )

    if not merged:
        raise ToolError(
            "That would leave the dashboard empty.",
            spec_path="remove_item_ids",
            fix="Delete the dashboard with delete_item instead.",
        )

    if reflow:
        chart_types = _chart_types_for(merged)
        layout.reflow(merged, chart_types=chart_types)

    if title:
        doc.title = title
    doc.items = merged
    doc.save()

    changes = []
    if add_items:
        changes.append(f"added {len(add_items)} item(s)")
    if add_filters:
        changes.append(f"added {len(add_filters)} filter(s)")
    if removed:
        changes.append(f"removed {removed} item(s)")
    if title:
        changes.append("renamed it")

    return render.cap("\n\n".join([
        f"Updated dashboard `{dashboard}`: {', '.join(changes) or 'no structural change'}.",
        _items_table(merged),
        _url_note(dashboard),
    ]))


# --------------------------------------------------------------------------- #
# item construction
# --------------------------------------------------------------------------- #


def _resolve_charts(items: list, filters: list) -> dict:
    names = {i["chart"] for i in items if i.get("type") == "chart" and i.get("chart")}
    for f in filters or []:
        names |= {link.get("chart") for link in (f.get("links") or []) if link.get("chart")}

    charts = {}
    for name in sorted(names):
        if not frappe.db.exists("Insights Chart v3", name):
            raise ToolError(
                f"No chart named '{name}'.",
                spec_path="items",
                fix="Create it with create_chart, or list them with get_item(type=\"workbook\").",
            )
        doc = frappe.get_doc("Insights Chart v3", name)
        doc.check_permission("read")
        charts[name] = doc
    return charts


def _workbook_of(charts: dict) -> str:
    workbooks = {str(c.workbook) for c in charts.values()}
    if not workbooks:
        raise ToolError(
            "`workbook` is required for a dashboard with no charts.", spec_path="workbook"
        )
    if len(workbooks) > 1:
        raise ToolError(
            f"Those charts live in different workbooks: {', '.join(sorted(workbooks))}.",
            spec_path="items",
            fix="A dashboard cannot span workbooks.",
        )
    return workbooks.pop()


def _assert_same_workbook(charts: dict, workbook: str) -> None:
    strays = sorted(n for n, c in charts.items() if str(c.workbook) != workbook)
    if strays:
        raise ToolError(
            f"Chart(s) {', '.join(strays)} are not in workbook '{workbook}'.",
            spec_path="items",
            fix="A dashboard can only show charts from its own workbook.",
        )


def _build_items(items: list, charts: dict) -> list:
    built = []
    for index, item in enumerate(items):
        kind = item.get("type")
        if kind == "chart":
            if not item.get("chart"):
                raise ToolError("A chart item needs a `chart`.", spec_path=f"items[{index}]")
            # `set_linked_charts` indexes item["type"] and item["chart"] with no guard
            # (insights_dashboard_v3.py:82-86), so both keys are mandatory, always.
            built.append({"type": "chart", "chart": item["chart"], "layout": {}})
        elif kind == "text":
            built.append({"type": "text", "text": item.get("text") or "", "layout": {}})
        else:
            raise ToolError(
                f"Unknown dashboard item type '{kind}'.", spec_path=f"items[{index}].type"
            )

        if item.get("width") == "full":
            # Consumed and removed by layout.size_for, so it never lands in the doc.
            built[-1]["width"] = "full"
    return built


def _build_filters(filters: list, charts: dict, *, existing_names: set) -> list:
    built = []
    names = set(existing_names)

    for index, spec in enumerate(filters):
        path = f"filters[{index}]"
        label = spec.get("label")
        if not label:
            raise ToolError("A filter needs a `label`.", spec_path=f"{path}.label")
        if label in names:
            raise ToolError(
                f"This dashboard already has a filter called '{label}'.",
                spec_path=f"{path}.label",
            )
        names.add(label)

        filter_type = spec.get("filter_type") or "String"
        item = {
            "type": "filter",
            "filter_name": label,
            "filter_type": filter_type,
            "links": {},
            "range_links": {},
            "layout": {},
        }

        for link_index, link in enumerate(spec.get("links") or []):
            link_path = f"{path}.links[{link_index}]"
            chart = charts.get(link.get("chart"))
            if not chart:
                raise ToolError("`chart` is required on a filter link.", spec_path=link_path)
            query = chart.query
            if not query:
                raise ToolError(
                    f"Chart '{chart.name}' is not bound to a query, so it cannot be filtered.",
                    spec_path=link_path,
                )

            if filter_type == "AsOfDate":
                start = _verified_column(query, link.get("start_column"), f"{link_path}.start_column")
                end = _verified_column(query, link.get("end_column"), f"{link_path}.end_column")
                item["range_links"][chart.name] = {
                    "start_column": encode_filter_link(query, start),
                    "end_column": encode_filter_link(query, end),
                }
            else:
                column = _verified_column(query, link.get("column"), f"{link_path}.column")
                item["links"][chart.name] = encode_filter_link(query, column)

        built.append(item)
    return built


def _verified_column(query: str, column: str, path: str) -> str:
    """A link naming a column the query does not emit is worse than an error: it is silent."""
    from insights.mcp.guards import columns_for_saved_query

    if not column:
        raise ToolError("A filter link needs a column.", spec_path=path)

    available = [c["name"] for c in columns_for_saved_query(query)]
    if column not in available:
        raise ToolError(
            f"Query '{query}' does not emit a column called '{column}'.",
            spec_path=path,
            valid_columns=available,
        )
    return column


def _chart_types_for(items: list) -> dict:
    names = [i["chart"] for i in items if i.get("type") == "chart" and i.get("chart")]
    if not names:
        return {}
    return dict(
        frappe.get_all(
            "Insights Chart v3",
            filters={"name": ["in", names]},
            fields=["name", "chart_type"],
            as_list=True,
        )
    )


def _items_table(items: list) -> str:
    return render.table([
        {
            "id": (i.get("layout") or {}).get("i", ""),
            "type": i.get("type"),
            "ref": i.get("chart") or i.get("filter_name") or (i.get("text") or "")[:40],
            "position": "x{x} y{y} {w}x{h}".format(**(i.get("layout") or {})),
        }
        for i in items
    ])


def _url_note(name: str) -> str:
    from frappe.utils import get_url

    # `allow_header_override` is deliberately left at its default here, unlike
    # `origin.py` (§8 G). This is a display string, not a security decision, and behind a
    # tunnel the request's own host is the one the reader can actually click.
    return (
        f"URL: {get_url()}/insights/shared/dashboard/{name}\n\n"
        "_That link 404s for anyone not signed in: the dashboard is not public. Public "
        "links arrive in Phase 3._"
    )
