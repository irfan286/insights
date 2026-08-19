# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""The per-data-source documentation layer.

Schema tells the model what columns exist. It does not tell it that `status = 'C'`
means cancelled, that a table was superseded in 2023, or that joining on `order_ref`
duplicates rows unless you also filter `is_current = 1`. This module carries that.

**Delivery.** No Claude client auto-loads MCP resources, and `frappe_mcp` does not
implement them anyway (`server/handlers.py:39-56` raise NotImplementedError). So the
documentation reaches the model by piggy-backing on the tools it already calls:
`describe_table` splices in the blocks for one table, `list_tables` shows a one-line
purpose per table, and `run_query` errors append the blocks for the tables involved --
a failure is the moment the model is most receptive to grounding.

**Provenance is the point.** Three zones with different trust levels, never merged:
generated schema (fact), human Documentation (authoritative), AI Notes (unverified
inference). `compose()` returns them separately with headers saying which is which,
plus a conflict rule telling the model how to arbitrate.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

import frappe

DOCTYPE = "Insights Data Doc"

CONFLICT_RULE = (
    "SCHEMA is authoritative for what exists and its type. DOCUMENTATION is "
    "authoritative for what it means, which joins are correct, and which tables are "
    "canonical. AI NOTES are unverified inference — treat them as hypotheses."
)

TIER_CAPS = {"source": 12_000, "narrative": 8_000, "erd": 4_000, "table": 8_000, "block": 32_000}
# Below this a truncated block carries no usable content, only a heading and a
# provenance line. Emitting those wastes budget AND reads to the model as though the
# documentation itself were empty -- worse than saying "N more blocks, fetch them by id".
MIN_USEFUL_BLOCK = 400
MAX_ERD_EDGES = 60
TRUST = {
    "Documentation": "authoritative",
    "AI Note": "low — verify before relying",
}


@dataclass(frozen=True)
class DocBlock:
    block_id: str
    zone: str            # "DOCUMENTATION" | "AI NOTE"
    provenance: str
    trust: str
    title: str
    body: str
    scope: str
    table_name: str | None = None
    truncated: bool = False
    stale_warning: str | None = None

    def to_markdown(self, *, level: int = 4) -> str:
        out = [f"{'#' * level} [{self.zone} · {self.trust}] {self.title}", f"_{self.provenance}_"]
        if self.stale_warning:
            out.append(self.stale_warning)
        out.append(self.body)
        if self.truncated:
            out.append(f'…truncated. Call `get_docs(block_id="{self.block_id}")` for the full text.')
        return "\n\n".join(out)


@dataclass
class ComposedDocs:
    data_source: str
    table: str | None = None
    blocks: list[DocBlock] = field(default_factory=list)
    erd: str | None = None
    erd_note: str | None = None
    erd_truncated: bool = False
    total_edges: int = 0
    omitted: list = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.blocks or self.erd or self.erd_note)


# --------------------------------------------------------------------------- #
# composition
# --------------------------------------------------------------------------- #


def compose(
    data_source: str,
    table: str | None = None,
    *,
    include_erd: bool = True,
    include_ai_notes: bool = True,
    budget: int | None = None,
) -> ComposedDocs:
    """Gather the documentation for a source, or one table within it.

    Tier caps are enforced HERE and nowhere else. Three call sites would otherwise each
    have to reimplement them, and the one that forgot would be a silent context
    blow-out.
    """
    _check_access(data_source)
    composed = ComposedDocs(data_source=data_source, table=table)
    if not frappe.db.table_exists(DOCTYPE):
        return composed

    budget = budget or (TIER_CAPS["table"] if table else TIER_CAPS["source"])
    narrative_budget = budget if table else TIER_CAPS["narrative"]

    zones = ["Documentation"] + (["AI Note"] if include_ai_notes else [])
    filters = {"data_source": data_source, "status": "Active", "zone": ["in", zones]}
    if table:
        # A table view also carries the source-level narrative: the model asking about
        # one table still needs the conventions that apply across the whole source.
        rows = frappe.get_all(
            DOCTYPE,
            filters={**filters, "table_name": table, "scope": "Table"},
            fields="*",
            order_by="zone asc, sort_order asc, creation asc",
        )
    else:
        rows = frappe.get_all(
            DOCTYPE,
            filters={**filters, "scope": "Data Source"},
            fields="*",
            order_by="zone asc, sort_order asc, creation asc",
        )

    spent = 0
    for index, row in enumerate(rows):
        remaining = narrative_budget - spent
        block = _to_block(row, remaining=remaining) if remaining > 0 else None
        if block is None:
            # Stop cleanly and hand back the ids, rather than emitting empty husks.
            composed.omitted = [
                {"block_id": r.get("name"), "title": r.get("title"), "zone": r.get("zone")}
                for r in rows[index:]
            ]
            break
        composed.blocks.append(block)
        spent += len(block.body.encode("utf-8"))

    if include_erd and not table:
        result = erd(data_source)
        composed.erd, composed.erd_note = result.mermaid, result.note
        composed.erd_truncated, composed.total_edges = result.truncated, result.total_edges

    return composed


def blocks_for(refs, *, limit: int = 4, budget: int = 4_000) -> list[DocBlock]:
    """Documentation for the tables named in a failure. §5.4 rung 1.

    `refs` is an iterable of (data_source, table_name) — exactly what
    `guards.resolved_tables` returns, so an error handler can pass it straight through.
    """
    out, spent, seen = [], 0, set()
    for ref in list(refs or [])[:limit]:
        data_source = ref.get("data_source") if isinstance(ref, dict) else ref[0]
        table = ref.get("table_name") if isinstance(ref, dict) else ref[1]
        if (data_source, table) in seen:
            continue
        seen.add((data_source, table))
        try:
            composed = compose(data_source, table, include_erd=False)
        except Exception:
            continue
        for block in composed.blocks:
            if spent >= budget:
                return out
            out.append(block)
            spent += len(block.body.encode("utf-8"))
    return out


def block(block_id: str) -> DocBlock | None:
    """Tier 4: one block, in full, bypassing the composition budget."""
    if not frappe.db.exists(DOCTYPE, block_id):
        return None
    row = frappe.get_doc(DOCTYPE, block_id)
    _check_access(row.data_source)
    return _to_block(row.as_dict(), remaining=TIER_CAPS["block"])


def _to_block(row, *, remaining: int) -> DocBlock | None:
    """Render one row, or None when what would survive truncation is not worth sending.

    Two traps, both found against a real 600KB corpus rather than the small fixtures:

    1. Trimming back to the last paragraph break can throw away nearly everything. A
       body of "Section 2.\n\n<3KB of prose>" cut at 900 bytes has its last `\n\n` at
       offset 10, so the naive trim yields ten bytes. Only honour the paragraph
       boundary when it keeps most of what we were allowed.
    2. Guarding on `remaining` alone is not enough — the check has to be on the body
       that actually results, or the caller's budget accounting silently under-counts
       and the cap never engages.
    """
    if remaining <= 0:
        return None

    body = (row.get("body") or "").strip()
    encoded = body.encode("utf-8")
    truncated = len(encoded) > remaining
    if truncated:
        allowed = max(0, remaining - 100)
        body = encoded[:allowed].decode("utf-8", errors="ignore")
        cut = body.rfind("\n\n")
        if cut > len(body) * 0.6:          # only if it keeps most of the text
            body = body[:cut]
        if len(body.encode("utf-8")) < MIN_USEFUL_BLOCK:
            return None

    zone = row.get("zone") or "AI Note"
    return DocBlock(
        block_id=row.get("name"),
        zone=zone.upper(),
        provenance=_provenance(row),
        trust=TRUST.get(zone, "unknown"),
        title=row.get("title") or "(untitled)",
        body=body,
        scope=row.get("scope"),
        table_name=row.get("table_name"),
        truncated=truncated,
        stale_warning=_stale_warning(row),
    )


def _provenance(row) -> str:
    when = str(row.get("modified") or row.get("creation") or "")[:10]
    owner = row.get("owner") or "unknown"
    if row.get("zone") == "Documentation":
        verified = row.get("verified_on")
        base = f"uploaded by {owner}, {when}"
        return f"{base}, verified {str(verified)[:10]}" if verified else base
    # Deliberately does NOT read "inferred by <human email>". The owner of an
    # MCP-written note is whoever's API key was used; attributing the model's inference
    # to a named person is exactly the provenance collapse this subsystem prevents.
    return f"inferred by Claude via MCP (session user {owner}), {when}, unverified"


def _stale_warning(row) -> str | None:
    """An actionable banner, not a bare 'schema changed'.

    A vague warning trains the model to ignore the banner, so name the dead columns.
    """
    if not row.get("is_stale"):
        return None
    reason = row.get("stale_reason") or "the schema has changed since this was written"
    return f"⚠️ **STALE** — {reason}\nVerify the guidance below before relying on it."


def render_blocks(composed: ComposedDocs, *, level: int = 3) -> str:
    """Markdown for the documentation section of a tool response.

    `compose()` returns structure; rendering lives here so `describe_table` can splice
    blocks into its own response and `errors.py` can embed them under an error heading,
    rather than every caller string-slicing a pre-rendered blob.
    """
    if not composed:
        return ""
    out = [f"{'#' * level} Documentation", f"_{CONFLICT_RULE}_"]
    for block_ in composed.blocks:
        out.append(block_.to_markdown(level=level + 1))
    if composed.erd:
        out.append(f"{'#' * (level + 1)} Table relationships\n\n```mermaid\n{composed.erd}\n```")
    elif composed.erd_note:
        out.append(composed.erd_note)
    if composed.omitted:
        listing = "\n".join(
            f'- `{o["block_id"]}` — {o["title"]}' for o in composed.omitted[:40]
        )
        more = f"\n_(and {len(composed.omitted) - 40} more)_" if len(composed.omitted) > 40 else ""
        out.append(
            f"{'#' * (level + 1)} {len(composed.omitted)} further block(s) not shown\n\n"
            f"The response budget was spent. Fetch any of these in full with "
            f"`get_docs(block_id=...)`:\n\n{listing}{more}"
        )
    return "\n\n".join(out)


# --------------------------------------------------------------------------- #
# generated zone: the ERD
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ErdResult:
    mermaid: str | None = None
    note: str | None = None
    truncated: bool = False
    total_edges: int = 0


def erd(data_source: str, *, focus_tables=None, max_edges: int = MAX_ERD_EDGES) -> ErdResult:
    """Mermaid ERD from `Insights Table Link v3`.

    An edge is ~40 characters against ~200 for the equivalent JSON, it renders natively
    in Claude clients, and it degrades to readable text when it does not.
    """
    links = frappe.get_all(
        "Insights Table Link v3",
        filters={"data_source": data_source},
        fields=["left_table", "left_column", "right_table", "right_column"],
        limit=max_edges * 4,
    )
    total = len(links)
    if not total:
        # `update_table_links` populates only when is_site_db or is_frappe_db
        # (insights_data_source_v3.py:420-422), so an empty ERD means "not discovered",
        # never "no relationships exist".
        return ErdResult(
            note=(
                "**No table links recorded for this data source.** Insights only "
                "auto-discovers links for Frappe databases. Correct joins for this "
                "source, if documented, are in the Documentation blocks above — "
                "`joins: []` from describe_table means UNKNOWN, not none."
            )
        )

    if focus_tables:
        focus = set(focus_tables)
        links = [l for l in links if l.left_table in focus or l.right_table in focus]

    truncated = len(links) > max_edges
    links = links[:max_edges]

    lines = [
        "erDiagram",
        "%% cardinality inferred from FK direction; not recorded in Insights.",
    ]
    for link in links:
        left, right = _mermaid_name(link.left_table), _mermaid_name(link.right_table)
        lines.append(f'  {left} }}o--|| {right} : "{link.left_column} → {link.right_column}"')

    return ErdResult(mermaid="\n".join(lines), truncated=truncated, total_edges=total)


def _mermaid_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", name or "")


# --------------------------------------------------------------------------- #
# staleness
# --------------------------------------------------------------------------- #


def fingerprint(data_source: str, table: str | None = None) -> str:
    """sha1 of the sorted (column, type) list for a table, or of the table list.

    The data-source-scoped fingerprint catches tables being added or removed but NOT
    column drift inside a table. Say so wherever it is surfaced.
    """
    if table:
        from insights.api.data_sources import get_data_source_table_columns

        columns = get_data_source_table_columns(data_source, table)
        material = sorted(f"{c['column']}:{c.get('type')}" for c in columns)
    else:
        material = sorted(
            frappe.get_all("Insights Table v3", filters={"data_source": data_source}, pluck="table")
        )
    return hashlib.sha1("|".join(material).encode("utf-8")).hexdigest()


def extract_referenced_columns(body: str, data_source: str, table: str | None = None) -> list[str]:
    """Identifiers in backticks that are real columns of the scoped table."""
    if not body or not table:
        return []
    try:
        from insights.api.data_sources import get_data_source_table_columns

        known = {c["column"] for c in get_data_source_table_columns(data_source, table)}
    except Exception:
        return []
    ticked = set(re.findall(r"`([A-Za-z_][A-Za-z0-9_]*)`", body))
    return sorted(ticked & known)


def flag_stale_docs() -> None:
    """Daily sweep. Registered in hooks.py.

    One pass, reusing the cached schema. Sets is_stale/stale_reason so the Desk UI can
    badge stale docs for the humans who own them.
    """
    if not frappe.db.table_exists(DOCTYPE):
        return

    cache: dict = {}
    for row in frappe.get_all(
        DOCTYPE,
        filters={"status": "Active"},
        fields=["name", "data_source", "table_name", "schema_fingerprint", "referenced_columns"],
    ):
        key = (row.data_source, row.table_name)
        if key not in cache:
            try:
                cache[key] = fingerprint(row.data_source, row.table_name)
            except Exception:
                cache[key] = None
        current = cache[key]
        if current is None or not row.schema_fingerprint:
            continue

        if current == row.schema_fingerprint:
            if frappe.db.get_value(DOCTYPE, row.name, "is_stale"):
                frappe.db.set_value(DOCTYPE, row.name, {"is_stale": 0, "stale_reason": None})
            continue

        frappe.db.set_value(
            DOCTYPE,
            row.name,
            {
                "is_stale": 1,
                "stale_reason": _stale_reason(row),
            },
        )
    frappe.db.commit()


def _stale_reason(row) -> str:
    """Name the dead columns. A bare 'schema changed' teaches the reader to ignore it."""
    try:
        from insights.api.data_sources import get_data_source_table_columns

        if not row.table_name:
            return "the table list of this data source has changed since this was written."
        present = {c["column"] for c in get_data_source_table_columns(row.data_source, row.table_name)}
        referenced = set(frappe.parse_json(row.referenced_columns) or [])
        missing = sorted(referenced - present)
        if missing:
            return f"referenced but no longer present: {', '.join(missing)}."
        return "the schema has changed, though every column this document names still exists."
    except Exception:
        return "the schema has changed since this was written."


def _check_access(data_source: str) -> None:
    """Documentation carries business narrative -- gate it like the source itself."""
    frappe.has_permission("Insights Data Source v3", "read", doc=data_source, throw=True)
