# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""Dashboard grid layout.

The model says "half width"; this module says `{i, x, y, w, h}`. Asking a language model
for grid coordinates produces overlapping tiles, and the overlap is invisible until a
human opens the dashboard.

Every number here is read off `frontend/src2/dashboard/dashboard.ts:77-140` -- 20 columns
("for 5 columns" in its own comment), charts 10x8, Number charts 20x3, text 10x1,
filters 4x1.
"""

import frappe

GRID_COLS = 20

SIZES = {
    "chart": (10, 8),
    "number_chart": (20, 3),
    "text": (10, 1),
    "filter": (4, 1),
}


def new_id() -> str:
    """The grid item id. `getUniqueId` (helpers/index.ts:27-29) produces 8 characters."""
    return frappe.generate_hash(length=8)


def size_for(item: dict, *, chart_types: dict | None = None) -> tuple[int, int]:
    """`width: "full"` is consumed here and removed, so it never reaches the stored doc."""
    kind = item.get("type")
    full_width = item.pop("width", None) == "full"

    if kind == "chart":
        chart_type = (chart_types or {}).get(item.get("chart"))
        # A Number chart is a single figure; the UI gives it a full-width banner.
        width, height = SIZES["number_chart"] if chart_type == "Number" else SIZES["chart"]
    else:
        width, height = SIZES.get(kind, SIZES["chart"])

    return (GRID_COLS, height) if full_width else (width, height)


def has_layout(item: dict) -> bool:
    layout = item.get("layout") or {}
    return all(key in layout for key in ("x", "y", "w", "h"))


def place_new(items: list, *, chart_types: dict | None = None) -> list:
    """Give a layout to the items that have none, and touch nothing else.

    This is the default for `update_dashboard`, and the reason is a bug report: a full
    reflow on every update threw away whatever arrangement a human had made in the UI.
    Adding one chart should not rearrange the other five.

    It mirrors what the UI does when you add something. A chart or text block goes to
    `x: 0` on a fresh row below everything (`addChart`/`addText`,
    `dashboard.ts:77-118`); a filter joins the existing top filter row if it fits, and
    only if it does not does anything already on the board move -- see
    `_position_new_filter`.
    """
    for item in items:
        if has_layout(item):
            item.pop("width", None)  # consumed here so it never reaches the stored doc
            continue

        width, height = size_for(item, chart_types=chart_types)
        item["layout"] = {
            "i": (item.get("layout") or {}).get("i") or new_id(),
            "x": 0,
            "y": 0,
            "w": width,
            "h": height,
        }

        if item.get("type") == "filter":
            _position_new_filter(items, item)
        else:
            item["layout"]["y"] = _max_y(items, exclude=item)

    return items


def _max_y(items: list, *, exclude: dict | None = None) -> int:
    """`getMaxY`, dashboard.ts:101-103 -- computed over the items already on the board."""
    heights = [
        item["layout"]["y"] + item["layout"]["h"]
        for item in items
        if item is not exclude and has_layout(item)
    ]
    return max(heights, default=0)


def _position_new_filter(items: list, new_filter: dict) -> None:
    """`positionNewFilter`, dashboard.ts:145-181, ported branch for branch.

    The overflow branch is the one place incremental placement moves an existing item:
    when the top filter row is full, the new filter takes `y: 0` and everything sitting
    at or above one filter-height is pushed down. That is what the UI does, and a filter
    silently landing off-screen would be worse.
    """
    _, filter_height = SIZES["filter"]
    existing = [
        item
        for item in items
        if item is not new_filter and item.get("type") == "filter" and has_layout(item)
    ]
    if not existing:
        # The UI returns here, leaving the new filter overlapping whatever sits at the
        # top, and lets grid-layout-plus resolve the collision when it renders. A
        # headless writer has no renderer to lean on, so we pre-apply the same push the
        # overflow branch does -- the stored layout then matches what the UI ends up
        # showing instead of what it briefly stores.
        _push_board_down(items, new_filter, filter_height)
        return

    top_row_y = min(item["layout"]["y"] for item in existing)
    top_row = [item for item in existing if item["layout"]["y"] == top_row_y]
    rightmost_x = max(
        [item["layout"]["x"] + (item["layout"]["w"] or SIZES["filter"][0]) for item in top_row],
        default=0,
    )

    if rightmost_x + new_filter["layout"]["w"] <= GRID_COLS:
        new_filter["layout"]["x"] = rightmost_x
        new_filter["layout"]["y"] = top_row_y
        return

    for item in existing:
        item["layout"]["y"] += filter_height

    _push_board_down(items, new_filter, filter_height)


def _push_board_down(items: list, new_filter: dict, filter_height: int) -> None:
    others = [
        item
        for item in items
        if item is not new_filter and item.get("type") != "filter" and has_layout(item)
    ]
    if others and min(item["layout"]["y"] for item in others) <= filter_height:
        for item in others:
            item["layout"]["y"] = max(0, item["layout"]["y"] + filter_height)


def reflow(items: list, *, chart_types: dict | None = None) -> list:
    """Re-lay-out EVERY item, flowing left to right and wrapping.

    Right for a dashboard being created, where there is no arrangement to lose. On an
    existing one it is destructive, so `update_dashboard` only does this when asked --
    see `place_new`.

    Filters are laid out first, on their own row, because that is where the UI puts them
    (`positionNewFilter`, `dashboard.ts:145-180`) and because a filter buried between two
    charts reads as decoration.

    An existing `layout.i` is preserved: it is what `remove_item_ids` addresses, and
    changing it under the caller would silently break their next call.
    """
    filters = [i for i in items if i.get("type") == "filter"]
    rest = [i for i in items if i.get("type") != "filter"]

    y = _place_row(filters, 0, chart_types)
    _place_row(rest, y, chart_types)
    return items


def _place_row(items: list, start_y: int, chart_types: dict | None) -> int:
    x, y, row_height = 0, start_y, 0

    for item in items:
        width, height = size_for(item, chart_types=chart_types)
        if x + width > GRID_COLS:
            x, y = 0, y + row_height
            row_height = 0

        layout = item.get("layout") or {}
        item["layout"] = {
            "i": layout.get("i") or new_id(),
            "x": x,
            "y": y,
            "w": width,
            "h": height,
        }

        x += width
        row_height = max(row_height, height)

    return y + row_height if items else start_y
