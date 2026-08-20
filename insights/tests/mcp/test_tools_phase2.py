# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""The Phase 2 tools, exercised the way a session actually uses them.

One test walks the whole sequence -- save_query -> create_chart -> get_item ->
create_dashboard -> update_dashboard -> delete_item -- against `Site DB`, because the
interesting failures in this layer are between the tools rather than inside any one of
them: a chart whose data_query never got operations, a dashboard item missing the `type`
key that `set_linked_charts` subscripts unguarded, a filter link that parses as valid but
names a column the query does not emit.

`Site DB` and not `demo_data`: DuckDB is fine in-process but takes the web worker down
over HTTP (§8 Q), and a test that only ever passes in-process teaches the next session the
wrong lesson about which source to demo with.
"""

import frappe

from insights.insights.doctype.insights_data_source_v3.insights_data_source_v3 import (
    db_connections,
)
from insights.mcp.errors import ToolError
from insights.mcp.tools.chart import create_chart, update_chart
from insights.mcp.tools.dashboard import create_dashboard, decode_filter_link, update_dashboard
from insights.mcp.tools.workbook import delete_item, get_item, list_workbooks, save_query
from insights.tests.base import InsightsIntegrationTestCase
from insights.tests.factories import DT, delete_workbooks

PREFIX = "Phase2ToolsTest"

SPEC = {
    "from": {"data_source": "Site DB", "table": "tabToDo"},
    "group_by": [{"column": "status"}],
    "aggregate": [{"fn": "count"}],
    "sort": [{"column": "count_of_rows", "desc": True}],
}

TWO_DIMENSION_SPEC = {
    "from": {"data_source": "Site DB", "table": "tabToDo"},
    "group_by": [{"column": "status"}, {"column": "priority"}],
    "aggregate": [{"fn": "count"}],
}

CHART_SPEC = {
    "chart_type": "Bar",
    "x": {"column": "status"},
    "y": [{"column": "count_of_rows", "fn": "none"}],
}


class TestPhase2Tools(InsightsIntegrationTestCase):
    COMMIT_AFTER_TEST_SETUP = True
    COMMIT_AFTER_TEST_TEARDOWN = True

    def after_test(self):
        delete_workbooks(title_prefix=PREFIX)

    def save(self, title=f"{PREFIX} Query", workbook=None):
        # Omit `workbook` rather than passing null: @tool_args validates what it is given,
        # and a conformant MCP client omits an unset optional rather than sending None.
        extra = {"workbook": workbook} if workbook else {}
        response = save_query(
            title=title, spec=SPEC, workbook_title=f"{PREFIX} Workbook", **extra
        )
        return _first_backtick(response), response

    def test_save_query_creates_a_workbook_and_reports_its_columns(self):
        name, response = self.save()

        doc = frappe.get_doc(DT.QUERY, name)
        self.assertEqual(doc.use_live_connection, 1, "a saved query must not default to the warehouse")
        self.assertEqual(doc.is_builder_query, 1)
        self.assertTrue(doc.workbook)
        self.assertIn("count_of_rows", response)
        self.assertIn("Created workbook", response)

    def test_save_query_reuses_a_workbook_when_given_one(self):
        first, _ = self.save()
        workbook = str(frappe.db.get_value(DT.QUERY, first, "workbook"))
        second, _ = self.save(title=f"{PREFIX} Query 2", workbook=workbook)
        self.assertEqual(str(frappe.db.get_value(DT.QUERY, second, "workbook")), workbook)

    def test_save_query_refuses_both_spec_and_raw_operations(self):
        with self.assertRaises(ToolError):
            save_query(title=PREFIX, spec=SPEC, raw_operations=[{"type": "source"}])

    def test_list_workbooks_shows_the_new_one(self):
        self.save()
        self.assertIn(PREFIX, list_workbooks(search=PREFIX))

    def test_the_whole_sequence(self):
        query, _ = self.save()

        with db_connections():
            chart_response = create_chart(
                query=query, title=f"{PREFIX} Chart", spec=CHART_SPEC
            )
        chart = _first_backtick(chart_response)

        # The chart rendered: sample rows came back, and the hidden data_query was filled.
        self.assertIn("Sample rows", chart_response)
        data_query = frappe.db.get_value(DT.CHART, chart, "data_query")
        self.assertTrue(data_query)
        operations = frappe.parse_json(frappe.db.get_value(DT.QUERY, data_query, "operations"))
        self.assertEqual([op["type"] for op in operations], ["source", "summarize"])
        self.assertEqual(
            frappe.db.get_value(DT.QUERY, data_query, "use_live_connection"),
            1,
            "the data_query fell back to the warehouse -- the chart would render blank",
        )

        # Read it back, and the ChartSpec survives the round trip.
        read_back = get_item(type="chart", name=chart)
        self.assertIn("ChartSpec", read_back)
        self.assertIn('"chart_type": "Bar"', read_back)
        self.assertNotIn("not available", read_back)

        # And so does the QuerySpec.
        query_read_back = get_item(type="query", name=query)
        self.assertIn("QuerySpec", query_read_back)
        self.assertIn('"count_of_rows"', query_read_back)

        # Assemble a dashboard.
        dashboard_response = create_dashboard(
            title=f"{PREFIX} Dashboard",
            items=[{"type": "chart", "chart": chart}, {"type": "text", "text": "Hello"}],
        )
        dashboard = _first_backtick(dashboard_response)
        items = frappe.parse_json(frappe.get_doc(DT.DASHBOARD, dashboard).items)
        self.assertEqual([i["type"] for i in items], ["chart", "text"])
        for item in items:
            self.assertIn("type", item)
            self.assertTrue(item["layout"]["i"])
        self.assertEqual(items[0]["layout"], {"i": items[0]["layout"]["i"], "x": 0, "y": 0, "w": 10, "h": 8})
        self.assertIn("/insights/shared/dashboard/", dashboard_response)

        # linked_charts is derived, never written by us.
        self.assertEqual(
            [d.chart for d in frappe.get_doc(DT.DASHBOARD, dashboard).linked_charts], [chart]
        )

        # Add a filter, and the backtick encoding is built for the model.
        update_dashboard(
            dashboard=dashboard,
            add_filters=[
                {"label": "Status", "links": [{"chart": chart, "column": "status"}]}
            ],
        )
        items = frappe.parse_json(frappe.get_doc(DT.DASHBOARD, dashboard).items)
        filters = [i for i in items if i["type"] == "filter"]
        self.assertEqual(len(filters), 1)
        self.assertEqual(decode_filter_link(filters[0]["links"][chart]), (query, "status"))
        # The server's own regex is the real arbiter of a valid link.
        self.assertTrue(frappe.get_doc(DT.DASHBOARD, dashboard).is_filter_column(query, "status"))
        # Filters take the top row.
        self.assertEqual(filters[0]["layout"]["y"], 0)

        # Removing by the id get_item reports.
        text_id = next(i["layout"]["i"] for i in items if i["type"] == "text")
        update_dashboard(dashboard=dashboard, remove_item_ids=[text_id])
        items = frappe.parse_json(frappe.get_doc(DT.DASHBOARD, dashboard).items)
        self.assertNotIn("text", [i["type"] for i in items])

        # Deleting the chart takes its hidden data_query with it.
        delete_item(type="dashboard", name=dashboard)
        delete_item(type="chart", name=chart)
        self.assertFalse(frappe.db.exists(DT.CHART, chart))
        self.assertFalse(frappe.db.exists(DT.QUERY, data_query))

    def test_a_filter_on_a_column_the_query_does_not_emit_is_refused(self):
        query, _ = self.save()
        with db_connections():
            chart = _first_backtick(
                create_chart(query=query, title=f"{PREFIX} Chart", spec=CHART_SPEC)
            )
        dashboard = _first_backtick(
            create_dashboard(title=f"{PREFIX} Dashboard", items=[{"type": "chart", "chart": chart}])
        )

        with self.assertRaises(ToolError) as ctx:
            update_dashboard(
                dashboard=dashboard,
                add_filters=[{"label": "Nope", "links": [{"chart": chart, "column": "nope"}]}],
            )
        self.assertIn("status", str(ctx.exception))

    def test_render_skip_leaves_the_chart_unrendered_but_created(self):
        query, _ = self.save()
        response = create_chart(
            query=query, title=f"{PREFIX} Chart", spec=CHART_SPEC, render="skip"
        )
        chart = _first_backtick(response)
        self.assertIn("Not rendered", response)
        self.assertTrue(frappe.db.exists(DT.CHART, chart))
        # sync still ran: the operations are there, only the execution was skipped.
        data_query = frappe.db.get_value(DT.CHART, chart, "data_query")
        self.assertTrue(frappe.parse_json(frappe.db.get_value(DT.QUERY, data_query, "operations")))

    def test_a_pivot_chart_is_not_rendered_by_default(self):
        """A pivot runs an extra query at build time (`ibis_utils.py:572`), so `auto`
        creates the chart and leaves the render for an explicit call."""
        response = save_query(
            title=f"{PREFIX} Query", spec=TWO_DIMENSION_SPEC, workbook_title=f"{PREFIX} Workbook"
        )
        query = _first_backtick(response)
        spec = dict(CHART_SPEC, split_by={"column": "priority"})

        created = create_chart(query=query, title=f"{PREFIX} Chart", spec=spec)
        self.assertIn("Not rendered", created)
        self.assertIn("pivots", created)

        chart = _first_backtick(created)
        operations = frappe.parse_json(
            frappe.db.get_value(
                DT.QUERY, frappe.db.get_value(DT.CHART, chart, "data_query"), "operations"
            )
        )
        self.assertIn("pivot_wider", [op["type"] for op in operations])

        with db_connections():
            forced = update_chart(chart=chart, rerender_only=True, render="force")
        self.assertIn("Sample rows", forced)

    def test_update_chart_rerender_only_takes_no_other_argument(self):
        query, _ = self.save()
        chart = _first_backtick(
            create_chart(query=query, title=f"{PREFIX} Chart", spec=CHART_SPEC, render="skip")
        )
        with self.assertRaises(ToolError):
            update_chart(chart=chart, rerender_only=True, title="nope")

    def test_update_chart_keeps_styling_a_human_added(self):
        query, _ = self.save()
        chart = _first_backtick(
            create_chart(query=query, title=f"{PREFIX} Chart", spec=CHART_SPEC, render="skip")
        )
        doc = frappe.get_doc(DT.CHART, chart)
        config = frappe.parse_json(doc.config)
        config["show_data_labels"] = True
        doc.config = config
        doc.save()

        update_chart(chart=chart, spec=dict(CHART_SPEC, chart_type="Line"), render="skip")

        config = frappe.parse_json(frappe.get_doc(DT.CHART, chart).config)
        self.assertEqual(frappe.db.get_value(DT.CHART, chart, "chart_type"), "Line")
        self.assertTrue(config["show_data_labels"])

    def test_a_chart_on_a_dashboard_cannot_be_deleted(self):
        """Kept apart from the sequence test on purpose: @transactional rolls the whole
        request back on a ToolError, which in a test discards everything set up before it.
        In production that is exactly right -- one tools/call is one request."""
        query, _ = self.save()
        chart = _first_backtick(
            create_chart(query=query, title=f"{PREFIX} Chart", spec=CHART_SPEC, render="skip")
        )
        create_dashboard(title=f"{PREFIX} Dashboard", items=[{"type": "chart", "chart": chart}])

        with self.assertRaises(ToolError) as ctx:
            delete_item(type="chart", name=chart)
        self.assertIn("update_dashboard", str(ctx.exception))

    def test_delete_item_refuses_a_workbook(self):
        query, _ = self.save()
        workbook = str(frappe.db.get_value(DT.QUERY, query, "workbook"))
        with self.assertRaises(ToolError):
            delete_item(type="workbook", name=workbook)
        self.assertTrue(frappe.db.exists(DT.WORKBOOK, workbook))


def _first_backtick(text: str) -> str:
    return text.split("`")[1]
