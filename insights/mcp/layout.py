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


def reflow(items: list, *, chart_types: dict | None = None) -> list:
    """Assign every item an `{i, x, y, w, h}`, flowing left to right and wrapping.

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
