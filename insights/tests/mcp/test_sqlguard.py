# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""Read-only enforcement for native SQL.

Every rejection case below is a statement that a naive guard lets through, and three
of them were found by measuring sqlglot rather than by reasoning about it:

  * `EXEC sp_who` parses as exp.Alias, NOT exp.Command, so a blocklist of "dangerous
    node types" misses it entirely. Only a default-deny allowlist catches it.
  * `SELECT ... INTO t` parses as exp.Select and creates a table.
  * `WITH x AS (DELETE ... RETURNING *) SELECT * FROM x` also parses as exp.Select at
    the top level, with the DELETE buried in a CTE.

If one of these starts failing after a sqlglot upgrade, the guard has a hole -- do not
relax the test.
"""

import frappe
from frappe.tests import UnitTestCase

from insights.mcp.errors import ToolError
from insights.mcp.sqlguard import assert_read_only

READS = [
    "SELECT 1",
    "SELECT * FROM orders WHERE order_status = 'delivered'",
    "WITH a AS (SELECT 1 AS x) SELECT * FROM a",
    "SELECT 1 UNION SELECT 2",
    "SELECT count(*) OVER (PARTITION BY seller_id) FROM orders",
    "SELECT * FROM orders ORDER BY order_id LIMIT 10",
]

WRITES = [
    ("DELETE FROM orders", "DELETE"),
    ("UPDATE orders SET order_status = 'x'", "UPDATE"),
    ("INSERT INTO orders VALUES (1)", "INSERT"),
    ("DROP TABLE orders", "DROP"),
    ("CREATE TABLE t (a int)", "CREATE"),
    ("TRUNCATE TABLE orders", "TRUNCATE"),
    ("ALTER TABLE orders ADD COLUMN z int", "ALTER"),
    ("GRANT ALL ON orders TO someone", "GRANT"),
    ("EXEC sp_who", "stored procedure"),
    ("SELECT * INTO copy_of_orders FROM orders", "SELECT INTO"),
    (
        "WITH x AS (DELETE FROM orders RETURNING *) SELECT * FROM x",
        "DELETE hidden in a CTE",
    ),
]


class TestSqlGuard(UnitTestCase):
    def setUp(self):
        frappe.db.set_single_value("Insights Settings", "mcp_allow_sql_writes", 0)

    def tearDown(self):
        frappe.db.set_single_value("Insights Settings", "mcp_allow_sql_writes", 0)

    def test_reads_pass(self):
        for sql in READS:
            with self.subTest(sql=sql):
                assert_read_only(sql)

    def test_writes_are_refused(self):
        for sql, label in WRITES:
            with self.subTest(label=label):
                with self.assertRaises(ToolError, msg=f"{label} was NOT refused: {sql}"):
                    assert_read_only(sql)

    def test_multiple_statements_are_refused(self):
        with self.assertRaises(ToolError):
            assert_read_only("SELECT 1; DROP TABLE orders")

    def test_empty_is_refused(self):
        for sql in ("", "   ", None):
            with self.subTest(sql=repr(sql)):
                with self.assertRaises(ToolError):
                    assert_read_only(sql)

    def test_unparseable_is_refused(self):
        with self.assertRaises(ToolError):
            assert_read_only("SELECT FROM WHERE ((((")

    def test_error_names_the_statement_type(self):
        """The model only ever sees ToolError.__str__, so the diagnostic has to say
        what was wrong and what to do instead."""
        try:
            assert_read_only("DELETE FROM orders")
        except ToolError as e:
            text = str(e)
            self.assertIn("DELETE", text.upper())
            self.assertIn("SELECT", text.upper())
        else:
            self.fail("DELETE was not refused")

    def test_flag_relaxes_statement_type_but_not_multi_statement(self):
        """Allow MCP SQL Writes is the documented escape hatch for write statements.

        It deliberately does NOT re-enable multi-statement input: that is the classic
        injection shape, apply_sql cannot bind tables across statements anyway, and
        nothing a model legitimately needs requires it.
        """
        frappe.db.set_single_value("Insights Settings", "mcp_allow_sql_writes", 1)

        assert_read_only("DELETE FROM orders")  # permitted now

        with self.assertRaises(ToolError):
            assert_read_only("SELECT 1; DROP TABLE orders")

    def test_guard_fails_closed_when_the_field_is_missing(self):
        """origin.py fails closed before its field exists; so does this. A pre-migrate
        site must not silently permit writes."""
        from unittest.mock import patch

        from insights.mcp import sqlguard

        with patch.object(
            sqlguard.frappe.db, "get_single_value", side_effect=Exception("no such field")
        ):
            self.assertFalse(sqlguard.writes_allowed())
