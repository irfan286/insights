# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""The documentation layer, and the provenance boundary that gives it its value.

The zone guard is the load-bearing test here. If the MCP server can write the
human-authored Documentation zone, then when a query comes out wrong you can no longer
tell whether the model was misled by a person's documentation or by its own inference —
and every error looks the same. That is the whole reason the subsystem is split.
"""

import frappe

from insights.mcp import docs
from insights.mcp.errors import ToolError
from insights.tests.base import InsightsIntegrationTestCase

SOURCE = "demo_data"
TABLE = "orders"


class TestDataDocZoneGuard(InsightsIntegrationTestCase):
    def tearDown(self):
        frappe.flags.insights_mcp_write = False
        frappe.db.delete("Insights Data Doc", {"title": ["like", "ZoneTest%"]})
        super().tearDown()

    def _make(self, zone, title="ZoneTest note"):
        doc = frappe.new_doc("Insights Data Doc")
        doc.update({
            "data_source": SOURCE, "scope": "Data Source", "zone": zone,
            "status": "Active", "title": title, "body": "body text",
        })
        return doc

    def test_a_human_can_write_the_documentation_zone(self):
        doc = self._make("Documentation").insert()
        self.assertEqual(doc.zone, "Documentation")

    def test_mcp_cannot_write_the_documentation_zone(self):
        frappe.flags.insights_mcp_write = True
        with self.assertRaises(frappe.ValidationError):
            self._make("Documentation").insert()

    def test_mcp_can_write_the_ai_note_zone(self):
        frappe.flags.insights_mcp_write = True
        doc = self._make("AI Note").insert()
        self.assertEqual(doc.zone, "AI Note")

    def test_mcp_cannot_promote_by_changing_the_zone(self):
        """The second layer: even a tool that forgot to hard-code the zone is stopped."""
        doc = self._make("AI Note").insert()
        frappe.flags.insights_mcp_write = True
        doc.zone = "Documentation"
        with self.assertRaises(frappe.ValidationError):
            doc.save()

    def test_summary_is_derived_from_the_body_when_blank(self):
        doc = self._make("Documentation")
        doc.body = "# Heading\n\nOrders placed by customers, one row per order.\n"
        doc.insert()
        self.assertEqual(doc.summary, "Orders placed by customers, one row per order.")

    def test_table_scope_requires_a_table_name(self):
        doc = self._make("Documentation")
        doc.scope = "Table"
        with self.assertRaises(frappe.ValidationError):
            doc.insert()


class TestCompose(InsightsIntegrationTestCase):
    @classmethod
    def before_class(cls):
        cls.human = frappe.get_doc({
            "doctype": "Insights Data Doc", "data_source": SOURCE, "scope": "Table",
            "table_name": TABLE, "zone": "Documentation", "status": "Active",
            "title": "ComposeTest orders", "body": "`order_status` is a lifecycle state.",
        }).insert()
        cls.note = frappe.get_doc({
            "doctype": "Insights Data Doc", "data_source": SOURCE, "scope": "Table",
            "table_name": TABLE, "zone": "AI Note", "status": "Active",
            "title": "ComposeTest inference", "body": "price appears to exclude freight.",
        }).insert()

    @classmethod
    def after_class(cls):
        frappe.db.delete("Insights Data Doc", {"title": ["like", "ComposeTest%"]})

    def test_blocks_carry_zone_trust_and_provenance(self):
        composed = docs.compose(SOURCE, TABLE)
        by_zone = {b.zone: b for b in composed.blocks}
        self.assertIn("DOCUMENTATION", by_zone)
        self.assertIn("AI NOTE", by_zone)
        self.assertEqual(by_zone["DOCUMENTATION"].trust, "authoritative")
        self.assertIn("verify", by_zone["AI NOTE"].trust)
        self.assertIn("uploaded by", by_zone["DOCUMENTATION"].provenance)
        self.assertIn("inferred by Claude", by_zone["AI NOTE"].provenance)

    def test_ai_note_provenance_does_not_attribute_inference_to_a_person(self):
        note = next(b for b in docs.compose(SOURCE, TABLE).blocks if b.zone == "AI NOTE")
        self.assertTrue(note.provenance.startswith("inferred by Claude"))
        self.assertIn("unverified", note.provenance)

    def test_ai_notes_can_be_excluded(self):
        composed = docs.compose(SOURCE, TABLE, include_ai_notes=False)
        self.assertEqual([b.zone for b in composed.blocks], ["DOCUMENTATION"])

    def test_rendered_output_carries_the_conflict_rule_once(self):
        rendered = docs.render_blocks(docs.compose(SOURCE, TABLE))
        self.assertEqual(rendered.count(docs.CONFLICT_RULE), 1)

    def test_blocks_for_takes_resolved_tables_shape(self):
        """The shape guards.resolved_tables returns, so an error handler can pass it
        straight through."""
        blocks = docs.blocks_for([{"data_source": SOURCE, "table_name": TABLE}])
        self.assertTrue(blocks)

    def test_describe_table_splices_documentation_in(self):
        import insights.mcp.tools  # noqa: F401
        from insights.mcp.tools.discovery import describe_table

        out = describe_table(data_source=SOURCE, table_name=TABLE, include_preview=False)
        self.assertIn("ComposeTest orders", out)
        self.assertIn("DOCUMENTATION", out)


class TestErdAndStaleness(InsightsIntegrationTestCase):
    def test_erd_is_generated_from_recorded_links(self):
        result = docs.erd(SOURCE)
        if result.mermaid:
            self.assertTrue(result.mermaid.startswith("erDiagram"))
            self.assertIn("inferred from FK direction", result.mermaid)
        else:
            self.assertIn("UNKNOWN", result.note)

    def test_empty_erd_says_unknown_not_none(self):
        """A source with no links must not read as 'no relationships exist'."""
        result = docs.erd("__no_such_source__")
        self.assertIsNone(result.mermaid)
        self.assertIn("auto-discovers", result.note)

    def test_fingerprint_is_stable_and_scope_sensitive(self):
        a = docs.fingerprint(SOURCE, TABLE)
        self.assertEqual(a, docs.fingerprint(SOURCE, TABLE))
        self.assertNotEqual(a, docs.fingerprint(SOURCE))

    def test_referenced_columns_are_extracted_from_backticks(self):
        found = docs.extract_referenced_columns(
            "`order_status` matters, `not_a_column` does not.", SOURCE, TABLE
        )
        self.assertIn("order_status", found)
        self.assertNotIn("not_a_column", found)


class TestWriteAiNote(InsightsIntegrationTestCase):
    def tearDown(self):
        frappe.flags.insights_mcp_write = False
        frappe.db.delete("Insights Data Doc", {"title": ["like", "NoteTest%"]})
        super().tearDown()

    def test_note_is_written_into_the_ai_zone(self):
        import insights.mcp.tools  # noqa: F401
        from insights.mcp.tools.docs import write_ai_note

        out = write_ai_note(data_source=SOURCE, title="NoteTest one", body="observed X")
        self.assertIn("unverified AI note", out)
        row = frappe.get_last_doc("Insights Data Doc", filters={"title": "NoteTest one"})
        self.assertEqual(row.zone, "AI Note")

    def test_supersedes_retires_the_earlier_note(self):
        from insights.mcp.tools.docs import write_ai_note

        write_ai_note(data_source=SOURCE, title="NoteTest old", body="first guess")
        old = frappe.get_last_doc("Insights Data Doc", filters={"title": "NoteTest old"})
        write_ai_note(data_source=SOURCE, title="NoteTest new", body="corrected",
                      supersedes=old.name)
        self.assertEqual(frappe.db.get_value("Insights Data Doc", old.name, "status"), "Superseded")

    def test_cannot_supersede_human_documentation(self):
        from insights.mcp.tools.docs import write_ai_note

        human = frappe.get_doc({
            "doctype": "Insights Data Doc", "data_source": SOURCE, "scope": "Data Source",
            "zone": "Documentation", "status": "Active", "title": "NoteTest human",
            "body": "authoritative",
        }).insert()
        with self.assertRaises(ToolError):
            write_ai_note(data_source=SOURCE, title="NoteTest attempt", body="x",
                          supersedes=human.name)


class TestPromotion(InsightsIntegrationTestCase):
    def tearDown(self):
        frappe.db.delete("Insights Data Doc", {"title": ["like", "PromoteTest%"]})
        super().tearDown()

    def test_promotion_creates_a_new_row_and_preserves_provenance(self):
        """Never mutate the note's zone in place -- that would destroy the record of
        what the model claimed versus what a human verified."""
        from insights.api.docs import promote_note

        note = frappe.get_doc({
            "doctype": "Insights Data Doc", "data_source": SOURCE, "scope": "Data Source",
            "zone": "AI Note", "status": "Active", "title": "PromoteTest note",
            "body": "an inference", "proposed_for_promotion": 1,
        }).insert()

        promoted_name = promote_note(note.name, edited_body="a verified fact")
        promoted = frappe.get_doc("Insights Data Doc", promoted_name)

        self.assertEqual(promoted.zone, "Documentation")
        self.assertEqual(promoted.promoted_from, note.name)
        self.assertEqual(promoted.body, "a verified fact")
        self.assertIsNotNone(promoted.verified_on)
        note.reload()
        self.assertEqual(note.zone, "AI Note")       # unchanged
        self.assertEqual(note.status, "Superseded")

    def test_promotion_is_not_reachable_as_an_mcp_tool(self):
        import insights.mcp.tools  # noqa: F401
        from insights.mcp import mcp

        names = set(mcp._tool_registry)
        self.assertNotIn("promote_note", names)
        for name in names:
            self.assertNotIn("promote", name)


class TestCompositionBudget(InsightsIntegrationTestCase):
    """Regression: a real corpus exposed a bug the small fixtures above could not.

    With 28 data-source-scoped blocks competing for one 8KB narrative budget, the old
    logic spent nearly all of it on the first block and then emitted a dozen EMPTY
    husks — heading, provenance line, "…truncated", no content. That reads to the model
    as though the documentation itself were blank, which is worse than saying so.
    """

    PREFIX = "BudgetTest"

    @classmethod
    def before_class(cls):
        for i in range(12):
            frappe.get_doc({
                "doctype": "Insights Data Doc", "data_source": SOURCE,
                "scope": "Data Source", "zone": "Documentation", "status": "Active",
                "title": f"{cls.PREFIX} block {i:02d}",
                "body": f"Section {i}.\n\n" + ("lorem ipsum dolor sit amet. " * 120),
                "sort_order": i,
            }).insert()

    @classmethod
    def after_class(cls):
        frappe.db.delete("Insights Data Doc", {"title": ["like", f"{cls.PREFIX}%"]})

    def test_no_block_is_returned_with_an_empty_body(self):
        composed = docs.compose(SOURCE, include_erd=False)
        for block in composed.blocks:
            self.assertTrue(
                block.body.strip(),
                f"block {block.block_id} came back with no content at all",
            )

    def test_blocks_that_did_not_fit_are_listed_with_their_ids(self):
        composed = docs.compose(SOURCE, include_erd=False)
        self.assertTrue(composed.omitted, "expected some blocks to be omitted")
        for entry in composed.omitted:
            self.assertTrue(entry["block_id"])
            self.assertTrue(entry["title"])

    def test_rendered_output_tells_the_reader_how_to_fetch_the_rest(self):
        rendered = docs.render_blocks(docs.compose(SOURCE, include_erd=False))
        self.assertIn("not shown", rendered)
        self.assertIn("get_docs(block_id=", rendered)

    def test_an_omitted_block_is_retrievable_in_full(self):
        composed = docs.compose(SOURCE, include_erd=False)
        first = composed.omitted[0]
        fetched = docs.block(first["block_id"])
        self.assertIsNotNone(fetched)
        self.assertFalse(fetched.truncated)
        self.assertIn("lorem ipsum", fetched.body)

    def test_the_narrative_budget_is_actually_respected(self):
        composed = docs.compose(SOURCE, include_erd=False)
        total = sum(len(b.body.encode("utf-8")) for b in composed.blocks)
        self.assertLessEqual(total, docs.TIER_CAPS["narrative"])
