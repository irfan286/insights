# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""Markdown rendering and the response byte cap.

Every tool returns a `str` (non-negotiable #4): a `dict` return makes upstream write
`json.dumps(result)` into the text block AND set `structuredContent` to the same object
(`tools/handlers.py:46-66`), so the model pays for the identical payload twice.

Rows go out as a compact markdown table rather than per-row JSON objects -- roughly
half the tokens for a narrow result, because the column names are written once.
"""

MAX_RESPONSE_BYTES = 20_000
MAX_CELL_CHARS = 80


def _cell(value, limit: int = MAX_CELL_CHARS) -> str:
    if value is None:
        return ""
    text = str(value).replace("|", "\\|").replace("\n", " ").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def table(rows, columns=None, *, cell_limit: int = MAX_CELL_CHARS) -> str:
    """Render a list of dicts as a markdown table."""
    rows = list(rows or [])
    if not rows:
        return "_(no rows)_"

    columns = list(columns or rows[0].keys())
    out = ["| " + " | ".join(columns) + " |", "|" + "|".join(["---"] * len(columns)) + "|"]
    for row in rows:
        out.append("| " + " | ".join(_cell(row.get(c), cell_limit) for c in columns) + " |")
    return "\n".join(out)


def section(title: str, body: str, *, level: int = 2) -> str:
    return f"{'#' * level} {title}\n\n{body}"


def cap(text: str, *, limit: int = MAX_RESPONSE_BYTES, hint: str | None = None) -> str:
    """Truncate to a byte budget, visibly.

    Silent truncation is worse than a short answer: the model reads a cut-off table as
    the complete result and reasons from it.
    """
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text

    keep = encoded[: limit - 400].decode("utf-8", errors="ignore")
    keep = keep[: keep.rfind("\n") + 1] if "\n" in keep else keep
    note = hint or (
        "Response capped at 20KB. Narrow it with `select` to fewer columns, lower "
        "`page_size`, or request the next page."
    )
    return f"{keep}\n\n---\n**TRUNCATED.** {note}"
