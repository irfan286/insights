# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class InsightsDataDoc(Document):
    # begin: auto-generated types
    # This code is auto-generated. Do not modify anything in this block.

    from typing import TYPE_CHECKING

    if TYPE_CHECKING:
        from frappe.types import DF

        body: DF.MarkdownEditor | None
        data_source: DF.Link
        is_stale: DF.Check
        promoted_from: DF.Link | None
        proposed_for_promotion: DF.Check
        referenced_columns: DF.JSON | None
        schema_fingerprint: DF.Data | None
        scope: DF.Literal["Data Source", "Table"]
        sort_order: DF.Int
        source_file: DF.Link | None
        stale_reason: DF.SmallText | None
        status: DF.Literal["Active", "Superseded", "Draft"]
        summary: DF.SmallText | None
        table_name: DF.Data | None
        title: DF.Data
        verified_on: DF.Datetime | None
        zone: DF.Literal["Documentation", "AI Note"]
    # end: auto-generated types

    def validate(self):
        self._enforce_mcp_zone_boundary()
        self._require_table_name_when_table_scoped()
        self._derive_summary()
        self._refresh_fingerprint()

    def _enforce_mcp_zone_boundary(self):
        """The real guarantee behind the provenance split.

        `write_ai_note` has no `zone` parameter at all, which is the first layer. This
        is the second, and the one that actually holds: `handle_mcp` sets
        `frappe.flags.insights_mcp_write` for the duration of every tools/call, so a
        FUTURE tool that forgets layer 1 still cannot write into, or move a row into,
        the human-authored Documentation zone.

        The whole value of provenance separation is that when a query comes out wrong
        you can tell whether the model was misled by human documentation or by its own
        inference. If the model can write the zone it later reads as authoritative,
        that distinction is destroyed and every error looks the same.
        """
        if not frappe.flags.get("insights_mcp_write"):
            return

        if self.zone != "AI Note":
            frappe.throw(
                "The MCP server may only write the AI Note zone.",
                title="Documentation zone is read-only to MCP",
            )
        if not self.is_new() and self.has_value_changed("zone"):
            frappe.throw(
                "The MCP server may not change a document's zone. Promotion is a "
                "human action, performed in Insights.",
                title="Promotion is human-only",
            )

    def _require_table_name_when_table_scoped(self):
        if self.scope == "Table" and not self.table_name:
            frappe.throw("A table-scoped document needs a table name.")
        if self.scope == "Data Source":
            self.table_name = None

    def _derive_summary(self):
        """`summary` is the one-line `purpose` shown per table in list_tables."""
        if self.summary or not self.body:
            return
        for line in (self.body or "").splitlines():
            line = line.strip()
            if line and not line.startswith(("#", ">", "-", "*", "|", "`")):
                self.summary = line[:140]
                return

    def _refresh_fingerprint(self):
        from insights.mcp import docs

        if not self.has_value_changed("body") and self.schema_fingerprint:
            return
        try:
            self.schema_fingerprint = docs.fingerprint(self.data_source, self.table_name)
            self.referenced_columns = frappe.as_json(
                docs.extract_referenced_columns(self.body or "", self.data_source, self.table_name)
            )
        except Exception:
            # A doc must remain writable when the remote schema is unreachable.
            pass

        self.is_stale = 0
        self.stale_reason = None
