# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""`get_docs` and `write_ai_note`.

`write_ai_note` has NO zone parameter. That is layer one of the provenance boundary;
`InsightsDataDoc.validate()` is layer two and the one that actually holds. Promotion
into the authoritative zone is human-only and is not reachable from any MCP tool --
see `insights/api/docs.py`.
"""

import frappe
from frappe_mcp import ToolAnnotations

from insights.mcp import docs, mcp, render
from insights.mcp.errors import ToolError, transactional
from insights.mcp.schemas import GET_DOCS, WRITE_AI_NOTE
from insights.mcp.validate import tool_args


@mcp.tool(
    name="get_docs",
    input_schema=GET_DOCS,
    annotations=ToolAnnotations(
        title="Get data documentation",
        readOnlyHint=True,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
@tool_args(GET_DOCS)
def get_docs(
    data_source: str,
    table_name: str = None,
    block_id: str = None,
    include_erd: bool = True,
    include_ai_notes: bool = True,
    **_kw,
) -> str:
    """Curated documentation for a data source: what the tables mean and how to join them.

    Call this before writing your first query against a source. It carries business
    context, enum dictionaries, which tables are canonical versus deprecated, and the
    relationship diagram.

    Trust levels differ and are labelled. DOCUMENTATION is written by humans and is
    authoritative. AI NOTES were inferred by a previous session and are unverified.
    """
    if block_id:
        found = docs.block(block_id)
        if not found:
            raise ToolError(f"No documentation block called '{block_id}'.", spec_path="block_id")
        return render.cap(found.to_markdown(level=3), limit=docs.TIER_CAPS["block"])

    composed = docs.compose(
        data_source, table_name, include_erd=include_erd, include_ai_notes=include_ai_notes
    )
    if not composed:
        scope = f"table `{table_name}` in " if table_name else ""
        return (
            f"No documentation has been written for {scope}`{data_source}` yet.\n\n"
            f"Use `describe_table` for the raw schema. If you work out something "
            f"non-obvious about this data, record it with `write_ai_note` so the next "
            f"session does not have to rediscover it."
        )
    return render.cap(docs.render_blocks(composed, level=3), limit=docs.TIER_CAPS["source"])


@mcp.tool(
    name="write_ai_note",
    input_schema=WRITE_AI_NOTE,
    annotations=ToolAnnotations(
        title="Write an AI note",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=False,
    ),
)
@tool_args(WRITE_AI_NOTE)
@transactional
def write_ai_note(
    data_source: str,
    title: str,
    body: str,
    table_name: str = None,
    supersedes: str = None,
    propose_promotion: bool = False,
    **_kw,
) -> str:
    """Record something you worked out about this data, for future sessions.

    Write what you OBSERVED and HOW you observed it, so a human can verify it — for
    example "comparing order_total to invoice grand_total on 200 sampled rows,
    order_total appears to exclude tax", not "order_total excludes tax".

    Your note lands in the AI Notes zone, which is explicitly unverified. You cannot
    write the human-authored Documentation zone; only a person can promote a note into
    it. Use `propose_promotion` to flag one worth reviewing, and `supersedes` to retire
    an earlier note you now believe is wrong.
    """
    if not frappe.db.exists("Insights Data Source v3", data_source):
        raise ToolError(f"No data source called '{data_source}'.", spec_path="data_source")
    frappe.has_permission("Insights Data Source v3", "read", doc=data_source, throw=True)

    if supersedes:
        _supersede(supersedes, data_source)

    note = frappe.new_doc("Insights Data Doc")
    note.data_source = data_source
    note.scope = "Table" if table_name else "Data Source"
    note.table_name = table_name
    note.zone = "AI Note"          # hard-coded; there is no zone parameter by design
    note.status = "Active"
    note.title = title
    note.body = body
    note.promoted_from = None
    note.proposed_for_promotion = 1 if propose_promotion else 0
    note.insert()

    scope = f"`{table_name}` in `{data_source}`" if table_name else f"`{data_source}`"
    lines = [f"Noted against {scope} as an unverified AI note (`{note.name}`)."]
    if supersedes:
        lines.append(f"Superseded `{supersedes}`.")
    if propose_promotion:
        lines.append("Flagged for human review; a person may promote it to the authoritative zone.")
    return " ".join(lines)


def _supersede(name: str, data_source: str) -> None:
    existing = frappe.db.get_value(
        "Insights Data Doc", name, ["zone", "data_source"], as_dict=True
    )
    if not existing:
        raise ToolError(f"No note called '{name}' to supersede.", spec_path="supersedes")
    if existing.zone != "AI Note":
        raise ToolError(
            "Only AI notes can be superseded this way. Human documentation is retired "
            "by a person in Insights.",
            spec_path="supersedes",
        )
    if existing.data_source != data_source:
        raise ToolError(
            f"Note '{name}' belongs to a different data source.", spec_path="supersedes"
        )
    frappe.db.set_value("Insights Data Doc", name, "status", "Superseded")
