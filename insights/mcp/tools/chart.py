# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""`create_chart` and `update_chart`.

A chart is two documents: the `Insights Chart v3` the human sees, and a hidden
`Insights Query v3` (`chart.data_query`) that actually fetches its rows. The hidden one is
minted by `before_save -> set_data_query()` with a raw `db_insert()`
(`insights_chart_v3.py:68-78`), so it is never supplied here -- and its operations come
from `chart_operations`, the Python port of the browser's render pipeline.

The render itself goes through `guards.execute_saved`, not through the chart doctype's own
`refresh_data_query`, so `insights/mcp/` keeps exactly one file containing `.execute(`
(design §8.3). A guard test enforces that.
"""

import time

import frappe
from frappe_mcp import ToolAnnotations

from insights.mcp import chartspec, guards, mcp
from insights.mcp.compiler import SymbolTable
from insights.mcp.errors import ToolError, transactional
from insights.mcp.render import cap, table
from insights.mcp.schemas import CREATE_CHART, UPDATE_CHART
from insights.mcp.validate import tool_args

DOCTYPE = "Insights Chart v3"
SAMPLE_ROWS = 5

# Not a timeout -- there is no honest way to enforce one here (see `_render`). It is the
# threshold above which the response says the chart will be slow in the UI.
SLOW_RENDER_SECONDS = 30


@mcp.tool(
    name="create_chart",
    input_schema=CREATE_CHART,
    annotations=ToolAnnotations(
        title="Create a chart",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=True,
    ),
)
@tool_args(CREATE_CHART)
@transactional
def create_chart(query: str, title: str, spec: dict, render: str = "auto", **_kw) -> str:
    """Build a chart on a saved query, render it, and return sample rows.

    `query` is the name save_query returned. The chart's columns are resolved against that
    query's OUTPUT columns, so name the aliases it produces (`sum_of_price`), not the
    source columns underneath them.
    """
    source = _source_query(query)
    symbols = SymbolTable.from_json(guards.columns_for_saved_query(query))
    chart_type, config, notes = chartspec.resolve(spec, symbols)

    chart = frappe.new_doc(DOCTYPE)
    chart.title = title
    chart.workbook = str(source.workbook)
    chart.query = query
    chart.chart_type = chart_type
    chart.config = config
    # Never set data_query: before_save mints it, and a supplied one orphans a row.
    chart.insert()

    body = [f"Created chart `{chart.name}` ({chart_type}: {title}) on query `{query}`."]
    body += _sync_and_render(chart, render, notes)
    body.append(
        f"Next: `create_dashboard(items=[{{\"type\": \"chart\", \"chart\": \"{chart.name}\"}}])`."
    )
    return cap("\n\n".join(body))


@mcp.tool(
    name="update_chart",
    input_schema=UPDATE_CHART,
    annotations=ToolAnnotations(
        title="Update a chart",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=True,
    ),
)
@tool_args(UPDATE_CHART)
@transactional
def update_chart(
    chart: str,
    title: str = None,
    spec: dict = None,
    rerender_only: bool = False,
    render: str = "auto",
    **_kw,
) -> str:
    """Change a chart's configuration, or just re-run it against its source query.

    `rerender_only` covers "the source query changed, refresh the chart" and takes no
    other argument.

    A `spec` replaces the whole configuration rather than patching it, so a chart_type
    change is always coherent -- there is no half-updated config of the kind the UI's
    `resetConfig` exists to prevent. Styling a human added (colours, number formats,
    column widths) is carried across; anything you set in `options` wins.
    """
    doc = frappe.get_doc(DOCTYPE, chart)
    doc.check_permission("write")

    if rerender_only and (spec or title):
        raise ToolError(
            "`rerender_only` re-runs the chart unchanged; it takes no other argument.",
            spec_path="rerender_only",
        )

    notes = []
    changed = []

    if spec:
        symbols = SymbolTable.from_json(guards.columns_for_saved_query(doc.query))
        previous = frappe.parse_json(doc.config) or {}
        chart_type, config, notes = chartspec.resolve(spec, symbols)

        carried = _carried_styling(previous, config)
        if carried:
            config.update(carried)
            notes.append(f"Kept existing styling: {', '.join(sorted(carried))}.")

        doc.chart_type = chart_type
        doc.config = config
        changed.append(f"reconfigured as {chart_type}")

    if title:
        doc.title = title
        changed.append("renamed")

    if spec or title:
        doc.save()

    body = [
        f"Updated chart `{chart}`"
        + (f": {', '.join(changed)}." if changed else " -- re-rendered only.")
    ]
    body += _sync_and_render(doc, render, notes)
    return cap("\n\n".join(body))


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _source_query(name: str):
    if not frappe.db.exists("Insights Query v3", name):
        raise ToolError(
            f"No saved query named '{name}'.",
            spec_path="query",
            fix="Save one with save_query first, or list them with get_item(type=\"workbook\").",
        )
    doc = frappe.get_doc("Insights Query v3", name)
    doc.check_permission("read")
    return doc


def _carried_styling(previous: dict, config: dict) -> dict:
    """Cosmetic keys a human set that the new spec does not mention."""
    return {
        key: value
        for key, value in previous.items()
        if key not in chartspec.STRUCTURAL_CONFIG_KEYS and key not in config
    }


def _sync_and_render(chart, mode: str, notes: list) -> list:
    plan = chart.sync_data_query()
    operations = plan["operations"]

    body = []
    if notes:
        body.append("\n".join(f"- {note}" for note in notes))

    pivots = any(op["type"] == "pivot_wider" for op in operations)
    if mode == "skip" or (mode == "auto" and pivots):
        body.append(_skipped_note(chart.name, pivots, mode))
        return body

    result, elapsed = _render(chart)
    columns = [c["name"] for c in result["columns"]]
    body.append(
        "**Sample rows**\n\n"
        + table(result["rows"][:SAMPLE_ROWS], columns)
        + f"\n\n_{len(result['rows'])} row(s), {len(columns)} column(s)_"
    )
    if elapsed > SLOW_RENDER_SECONDS:
        body.append(
            f"⚠️ The render took {elapsed:.0f}s, so this chart will be slow to open. "
            "Consider a lower `limit`, a coarser granularity, or binding it to a query "
            "that is already aggregated."
        )
    return body


def _skipped_note(name: str, pivots: bool, mode: str) -> str:
    if mode == "skip":
        return (
            f"Not rendered (`render: skip`). Run "
            f"`update_chart(chart=\"{name}\", rerender_only=true)` when you want the rows."
        )
    return (
        "Not rendered: this chart pivots, and a pivot runs an extra query at build time to "
        "discover its column values (`ibis_utils.py:572`), which can be slow on a wide "
        f"column. Run `update_chart(chart=\"{name}\", rerender_only=true)` or pass "
        "`render: \"force\"` to render it anyway."
    )


def _render(chart) -> tuple[dict, float]:
    """Execute the chart's data_query and report how long it took.

    There is no timeout, and that is deliberate rather than an omission. `signal.alarm`
    only works on the main thread, and `bench serve` is threaded
    (`frappe/app.py:520-528`), so a SIGALRM-based cap would go green in the in-process
    test suite and raise ValueError on the dev server. Worse, the expensive step is inside
    a C extension, where a Python signal handler cannot run until it returns. The honest
    control is a per-connection statement timeout at the data-source layer; until that
    exists, the defences are the pivot refusal above and this measurement.
    """
    started = time.monotonic()
    config = frappe.parse_json(chart.config) or {}
    result = guards.execute_saved(chart.data_query, page_size=config.get("limit") or 100)
    return result, time.monotonic() - started
