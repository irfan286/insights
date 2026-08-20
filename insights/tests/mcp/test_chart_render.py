# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""The doc-state half of the chart render contract.

`test_chart_operations.py` pins the operations. It cannot pin the thing that actually
breaks charts in production: `use_live_connection` on the hidden data_query. Design §7.2
calls this out as the bug the render port would otherwise ship -- a data_query left at the
doctype default of 0 pushes that 0 into the upstream source query
(`ibis_utils.py:170`), so an un-imported table resolves to an empty temp table and the
chart renders zero rows **with no error**.

So this file asserts doc state, and executes once for real.
"""

import frappe

from insights.insights.doctype.insights_data_source_v3.insights_data_source_v3 import (
    db_connections,
)
from insights.tests.base import InsightsIntegrationTestCase
from insights.tests.factories import DT, delete_workbooks

TITLE = "ChartRenderTest Workbook"

BAR_CONFIG = {
    "x_axis": {"dimension": {"column_name": "status", "data_type": "String"}},
    "y_axis": {
        "series": [
            {
                "measure": {
                    "column_name": "count",
                    "data_type": "Integer",
                    "aggregation": "count",
                    "measure_name": "count_of_rows",
                }
            }
        ]
    },
    "limit": 20,
}


class TestRefreshDataQuery(InsightsIntegrationTestCase):
    COMMIT_AFTER_TEST_SETUP = True
    COMMIT_AFTER_TEST_TEARDOWN = True

    def before_test(self):
        self.workbook = frappe.get_doc({"doctype": DT.WORKBOOK, "title": TITLE}).insert()

    def after_test(self):
        delete_workbooks(title_prefix="ChartRenderTest")

    def make_chart(self, use_live_connection=1, config=None):
        query = frappe.get_doc(
            {
                "doctype": DT.QUERY,
                "title": "ChartRenderTest Query",
                "workbook": self.workbook.name,
                "use_live_connection": use_live_connection,
                "is_builder_query": 1,
                "operations": [
                    {
                        "type": "source",
                        "table": {
                            "type": "table",
                            "data_source": "Site DB",
                            "table_name": "tabToDo",
                        },
                    }
                ],
            }
        ).insert()

        chart = frappe.get_doc(
            {
                "doctype": DT.CHART,
                "title": "ChartRenderTest Chart",
                "workbook": self.workbook.name,
                "query": query.name,
                "chart_type": "Bar",
                "config": config if config is not None else BAR_CONFIG,
            }
        ).insert()
        return frappe.get_doc(DT.CHART, chart.name)

    def test_use_live_connection_is_inherited_from_the_source_query(self):
        chart = self.make_chart(use_live_connection=1)
        chart.sync_data_query()

        data_query = frappe.get_doc(DT.QUERY, chart.data_query)
        self.assertEqual(
            data_query.use_live_connection,
            1,
            "the data_query fell back to the warehouse path -- charts would render blank",
        )

    def test_a_warehouse_backed_source_stays_on_the_warehouse_path(self):
        chart = self.make_chart(use_live_connection=0)
        chart.sync_data_query()
        self.assertEqual(frappe.get_doc(DT.QUERY, chart.data_query).use_live_connection, 0)

    def test_operations_are_persisted_on_the_data_query(self):
        chart = self.make_chart()
        chart.sync_data_query()

        operations = frappe.parse_json(frappe.get_doc(DT.QUERY, chart.data_query).operations)
        self.assertEqual([op["type"] for op in operations], ["source", "summarize"])
        self.assertEqual(operations[0]["table"]["query_name"], chart.query)

    def test_refresh_is_idempotent(self):
        chart = self.make_chart()
        chart.sync_data_query()
        first = frappe.get_doc(DT.QUERY, chart.data_query).operations
        chart.sync_data_query()
        self.assertEqual(frappe.get_doc(DT.QUERY, chart.data_query).operations, first)

    def test_an_unbound_chart_refuses_to_refresh(self):
        chart = frappe.get_doc(
            {
                "doctype": DT.CHART,
                "title": "ChartRenderTest Chart",
                "workbook": self.workbook.name,
                "chart_type": "Bar",
                "config": BAR_CONFIG,
            }
        ).insert()
        with self.assertRaises(frappe.ValidationError):
            chart.sync_data_query()

    def test_an_incomplete_config_refuses_to_refresh(self):
        chart = self.make_chart(config={})
        with self.assertRaises(frappe.ValidationError):
            chart.sync_data_query()

    def test_sync_reports_the_page_size_from_the_config(self):
        chart = self.make_chart()
        self.assertEqual(chart.sync_data_query()["page_size"], 20)

    def test_it_actually_returns_rows(self):
        """The only assertion that proves the whole path works end to end."""
        chart = self.make_chart()
        with db_connections():
            result = chart.refresh_data_query()

        self.assertIn("columns", result)
        self.assertEqual(
            [c["name"] for c in result["columns"]],
            ["status", "count_of_rows"],
        )
