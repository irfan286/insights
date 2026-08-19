# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""Promotion — human-only, and deliberately NOT an MCP tool.

Promotion is what makes the Documentation zone worth trusting: a person decided a
claim was true. A tool that let the model move its own note into the authoritative
zone would erase exactly the guarantee the whole subsystem exists to provide. The most
the model may do is `write_ai_note(propose_promotion=True)`.

Promotion never mutates the note's zone in place. It creates a NEW Documentation row
and marks the note Superseded, so the provenance record survives.
"""

import frappe

from insights.decorators import insights_whitelist


@insights_whitelist()
def promote_note(name: str, edited_body: str | None = None) -> str:
    """Promote an AI note into the authoritative Documentation zone."""
    note = frappe.get_doc("Insights Data Doc", name)
    frappe.has_permission("Insights Data Source v3", "write", doc=note.data_source, throw=True)

    if note.zone != "AI Note":
        frappe.throw("Only AI notes can be promoted.")
    if note.status == "Superseded":
        frappe.throw("This note has already been superseded.")

    promoted = frappe.new_doc("Insights Data Doc")
    promoted.data_source = note.data_source
    promoted.scope = note.scope
    promoted.table_name = note.table_name
    promoted.zone = "Documentation"
    promoted.status = "Active"
    promoted.title = note.title
    promoted.summary = note.summary
    promoted.body = edited_body if edited_body is not None else note.body
    promoted.promoted_from = note.name
    promoted.verified_on = frappe.utils.now()
    promoted.insert()

    note.db_set({"status": "Superseded", "proposed_for_promotion": 0})
    return promoted.name


@insights_whitelist()
def list_promotion_queue(data_source: str | None = None) -> list[dict]:
    """AI notes the model has flagged for a human to verify."""
    filters = {"zone": "AI Note", "status": "Active", "proposed_for_promotion": 1}
    if data_source:
        filters["data_source"] = data_source
    return frappe.get_list(
        "Insights Data Doc",
        filters=filters,
        fields=["name", "data_source", "table_name", "title", "summary", "modified"],
        order_by="modified desc",
    )


# --------------------------------------------------------------------------- #
# Markdown import — human-only, like everything else in this module
# --------------------------------------------------------------------------- #

import os
import re

# A heading only becomes a table-scoped block when it NAMES a table in backticks and
# that name resolves to a real Insights Table v3 row. Matching bare words against
# schema-stripped table names looks generous and is worthless: "brand" in a prose
# heading would bind to `staging.netsuite__brand`. Backticks are the author's own
# marker for "this is an identifier" — trust that, nothing looser.
# `title` is a Data field: Frappe hard-caps it at 140 characters and raises
# CharacterLengthExceededError rather than truncating. Several headings in real
# documentation are longer than that, so trim explicitly.
TITLE_MAX = 140


def _title(text: str, fallback: str = "(untitled)") -> str:
    text = (text or "").strip() or fallback
    return text if len(text) <= TITLE_MAX else text[: TITLE_MAX - 1].rstrip() + "…"


_TICKED = re.compile(r"`([A-Za-z_][A-Za-z0-9_.]*)`")
_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")


def _known_tables(data_source: str) -> set[str]:
    return set(
        frappe.get_all("Insights Table v3", filters={"data_source": data_source}, pluck="table")
    )


def _resolve(ticked, tables, schemas) -> str | None:
    for name in ticked:
        if name in tables:
            return name
        for schema in schemas:
            qualified = f"{schema}.{name}"
            if qualified in tables:
                return qualified
    return None


_PATTERN_HEADING = re.compile(r"^\s*Pola:\s*`([a-z0-9_]+)`", re.I)


def _expand_pattern(heading_text: str, tables, schemas) -> list[str]:
    """Resolve a "Pola: `ms_kdbrg`" heading to every table that follows it.

    `bronze-legacy.md` documents ~29 table PATTERNS replicated across ~9 source
    systems, so its headings name no single table — `ms_kdbrg` is not a table, but
    `bronze.legacy_909sromorbit_ms_kdbrg` and eight siblings are. Without this the
    whole file collapses to one unusable data-source blob and 253 tables get no
    documentation at all.
    """
    match = _PATTERN_HEADING.match(heading_text)
    if not match:
        return []
    suffix = f"_{match.group(1)}"
    return sorted(
        t for t in tables
        if t.endswith(suffix) and (not schemas or t.split(".", 1)[0] in schemas)
    )


def slice_markdown(text: str, data_source: str, *, schemas=None, min_level: int = 2,
                   expand_patterns: bool = False):
    """Split one markdown document into (table_name | None, title, body) blocks.

    Deterministic and inspectable — no embeddings, no fuzzy matching. Content before
    the first table-naming heading, and any section whose heading names no table,
    accumulates into the data-source-scoped preamble, so nothing is silently dropped.
    """
    tables = _known_tables(data_source)
    schemas = schemas or sorted({t.split(".", 1)[0] for t in tables if "." in t})

    blocks, preamble = [], []
    current = None

    for line in text.splitlines():
        match = _HEADING.match(line)
        if match and len(match.group(1)) >= min_level:
            heading = match.group(2).strip()
            table = _resolve(_TICKED.findall(heading), tables, schemas)
            if table:
                current = {"tables": [table], "title": heading, "lines": []}
                blocks.append(current)
                continue
            if expand_patterns:
                expanded = _expand_pattern(heading, tables, schemas)
                if expanded:
                    current = {"tables": expanded, "title": heading, "lines": [],
                               "pattern": True}
                    blocks.append(current)
                    continue
            if current:
                # A sub-heading inside a table section stays with that section.
                current["lines"].append(line)
                continue
            current = None
            preamble.append(line)
            continue

        (current["lines"] if current else preamble).append(line)

    out = []
    if any(l.strip() for l in preamble):
        out.append((None, None, "\n".join(preamble).strip()))
    for block in blocks:
        body = "\n".join(block["lines"]).strip()
        if block.get("pattern") and len(block["tables"]) > 1:
            # Say plainly that this text is shared, so a reader who corrects it knows
            # there are siblings. Duplication is the accepted cost of per-table reach.
            body = (
                f"_Pola tabel yang berulang di {len(block['tables'])} sistem sumber; "
                f"teks yang sama dilampirkan ke masing-masing._\n\n{body}"
            )
        for table in block["tables"]:
            out.append((table, block["title"], body))
    return out


@insights_whitelist()
def import_markdown_docs(
    data_source: str,
    directory: str,
    dry_run: bool = True,
    replace: bool = False,
    expand_patterns: bool = True,
) -> dict:
    """Import a directory of markdown files into the Documentation zone.

    Human-only by construction: this is an `@insights_whitelist()` API, not an MCP
    tool, and it writes `zone = "Documentation"` — which `InsightsDataDoc.validate()`
    refuses whenever `frappe.flags.insights_mcp_write` is set. A model cannot reach it.

    `dry_run=True` (the default) writes nothing and returns the proposed split, so a
    person can confirm it before anything lands. That confirmation step is the point:
    an automatic slice of someone's documentation is a guess about their intent.
    """
    frappe.only_for("Insights Admin")
    frappe.has_permission("Insights Data Source v3", "write", doc=data_source, throw=True)

    directory = os.path.abspath(os.path.expanduser(directory))
    if not os.path.isdir(directory):
        frappe.throw(f"Not a directory: {directory}")

    files = sorted(f for f in os.listdir(directory) if f.endswith(".md"))
    if not files:
        frappe.throw(f"No .md files in {directory}")

    summary = {"data_source": data_source, "dry_run": dry_run, "files": [], "created": 0}

    if not dry_run and replace:
        frappe.db.delete(
            "Insights Data Doc", {"data_source": data_source, "zone": "Documentation"}
        )

    for filename in files:
        with open(os.path.join(directory, filename), encoding="utf-8") as handle:
            blocks = slice_markdown(
                handle.read(), data_source, expand_patterns=expand_patterns
            )

        stem = os.path.splitext(filename)[0]
        entry = {"file": filename, "table_blocks": 0, "source_blocks": 0, "tables": []}

        for table, title, body in blocks:
            if not body:
                continue
            entry["table_blocks" if table else "source_blocks"] += 1
            if table:
                entry["tables"].append(table)
            if dry_run:
                continue

            doc = frappe.new_doc("Insights Data Doc")
            doc.data_source = data_source
            doc.scope = "Table" if table else "Data Source"
            doc.table_name = table
            doc.zone = "Documentation"
            doc.status = "Active"
            doc.title = _title(title, f"{stem} — overview")
            doc.body = body
            doc.insert(ignore_permissions=False)
            summary["created"] += 1

        summary["files"].append(entry)

    if not dry_run:
        frappe.db.commit()
    return summary


@insights_whitelist()
def import_html_diagrams(data_source: str, file_path: str, dry_run: bool = True) -> dict:
    """Extract Mermaid diagrams from an HTML page into data-source documentation.

    Written for a hand-authored lineage page that already embeds Mermaid in
    `<pre class="mermaid">` blocks — extraction, not conversion. Titles come from the
    surrounding `<details class="panel">` markup so each diagram keeps its own name
    rather than repeating the section heading.

    These land as Data Source-scoped blocks with a `sort_order` that puts them AFTER
    the written narrative: the tier budget in `mcp.docs.compose` spends itself in
    order, and prose grounds a model better per byte than a 15KB flowchart.
    """
    import html as html_lib

    frappe.only_for("Insights Admin")
    frappe.has_permission("Insights Data Source v3", "write", doc=data_source, throw=True)

    path = os.path.abspath(os.path.expanduser(file_path))
    if not os.path.isfile(path):
        frappe.throw(f"Not a file: {path}")

    with open(path, encoding="utf-8") as handle:
        source = handle.read()

    def strip_tags(value: str) -> str:
        return html_lib.unescape(re.sub(r"<[^>]+>", "", value or "")).strip()

    diagrams = []
    for match in re.finditer(r'<pre class="mermaid">\s*(.*?)</pre>', source, re.S):
        body = html_lib.unescape(match.group(1)).strip()
        before = source[: match.start()]

        panels = re.findall(r'<span class="panel-title">(.*?)</span>', before, re.S)
        sections = re.findall(r"<h2[^>]*>(.*?)</h2>", before, re.S)
        descs = re.findall(r'<p class="panel-desc">(.*?)</p>', before, re.S)

        section = strip_tags(sections[-1]) if sections else "Diagram"
        panel = strip_tags(panels[-1]) if panels else None
        title = f"{section} — {panel}" if panel else section
        note = strip_tags(descs[-1]) if descs else ""
        diagrams.append((title, note, body))

    if not diagrams:
        frappe.throw("No <pre class=\"mermaid\"> blocks found in that file.")

    summary = {"file": path, "dry_run": dry_run, "diagrams": [], "created": 0}
    for index, (title, note, body) in enumerate(diagrams):
        summary["diagrams"].append({"title": title, "bytes": len(body.encode("utf-8"))})
        if dry_run:
            continue

        doc = frappe.new_doc("Insights Data Doc")
        doc.data_source = data_source
        doc.scope = "Data Source"
        doc.zone = "Documentation"
        doc.status = "Active"
        doc.title = _title(title)
        doc.summary = note[:140] if note else None
        doc.sort_order = 100 + index          # after the written narrative
        doc.body = (f"{note}\n\n" if note else "") + f"```mermaid\n{body}\n```"
        doc.insert()
        summary["created"] += 1

    if not dry_run:
        frappe.db.commit()
    return summary
