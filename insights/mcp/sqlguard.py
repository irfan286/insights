# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""Read-only enforcement for native SQL supplied by a model.

The Insights UI already lets a human run arbitrary SQL, and `_validate_native_sql`
(ibis_utils.py) only blocks multi-statement and EXEC, and only in warehouse mode. On a
live connection with `enable_permissions = 0` -- the default -- raw SQL reaches the
database with no gate at all. That is defensible for a person typing into an editor and
watching what they typed. It is not defensible for a model that may misread the intent
of a sentence and execute the result without a pause.

So this module is the gate, and it is DEFAULT-DENY: only an explicit allowlist of
top-level statement types passes. Anything sqlglot parses into something unexpected is
refused rather than waved through.

Three checks, because measurement showed one is not enough:

  1. Top-level node must be in ALLOWED_STATEMENTS.
     `EXEC sp_who` parses as exp.Alias, not exp.Command, so an allowlist catches it
     while a blocklist of "dangerous node types" would not have.

  2. No `into` argument.
     `SELECT * INTO newtable FROM t` parses as exp.Select and CREATES A TABLE. A check
     that only looked at the top-level type would pass it.

  3. No write node anywhere in the tree.
     `WITH x AS (DELETE FROM t RETURNING *) SELECT * FROM x` also parses as exp.Select
     at the top level, with the DELETE buried in a CTE.

Multi-statement input is refused unconditionally, including when writes are permitted:
it is the standard SQL-injection shape, `apply_sql` cannot bind tables across statements
anyway, and nothing a model legitimately needs requires it.
"""

import frappe

from insights.mcp.errors import ToolError

# Default-deny. A read is one of these at the top level and nothing else.
ALLOWED_STATEMENTS = ("Select", "Union", "Intersect", "Except", "Subquery")

# Names rather than classes: the set differs slightly across sqlglot versions, and a
# missing attribute here would silently shrink the guard.
WRITE_NODES = (
    "Insert", "Update", "Delete", "Drop", "Create", "Alter", "TruncateTable",
    "Grant", "Revoke", "Command", "Copy", "Merge",
)

SETTING = "mcp_allow_sql_writes"


def writes_allowed() -> bool:
    """Insights Settings > Allow MCP SQL Writes. Off unless deliberately turned on."""
    try:
        return bool(frappe.db.get_single_value("Insights Settings", SETTING))
    except Exception:
        # Field missing means the patch has not run. Fail CLOSED, like origin.py.
        return False


def assert_read_only(raw_sql: str, *, dialect: str | None = None) -> None:
    """Raise ToolError unless `raw_sql` is a single read-only statement.

    Skipped entirely when Insights Settings > Allow MCP SQL Writes is on, which is the
    documented escape hatch -- see the tool description for what that costs.
    """
    import sqlglot
    from sqlglot import exp

    if not raw_sql or not raw_sql.strip():
        raise ToolError("`sql` is empty.", spec_path="sql")

    try:
        statements = [s for s in sqlglot.parse(raw_sql, read=dialect) if s is not None]
    except Exception as exc:
        raise ToolError(
            f"Could not parse the SQL: {exc}",
            spec_path="sql",
            fix="Check the syntax. If the dialect is unusual, simplify the statement.",
        ) from exc

    if not statements:
        raise ToolError("No SQL statement found.", spec_path="sql")

    # Unconditional -- see the module docstring.
    if len(statements) > 1:
        raise ToolError(
            f"Multiple SQL statements are not accepted ({len(statements)} found).",
            spec_path="sql",
            fix="Send one statement. Use a CTE if you need several steps.",
        )

    if writes_allowed():
        return

    stmt = statements[0]
    kind = type(stmt).__name__

    if kind not in ALLOWED_STATEMENTS:
        raise ToolError(
            f"Only read-only SQL is accepted; this is a {kind.upper()} statement.",
            spec_path="sql",
            fix=(
                "Send a SELECT. To change data, do it in the database directly — an "
                "administrator can enable Insights Settings > Allow MCP SQL Writes, "
                "but that removes this protection for every MCP caller."
            ),
        )

    # `SELECT ... INTO t` is a Select that writes.
    if stmt.args.get("into"):
        raise ToolError(
            "SELECT ... INTO creates a table, so it is not read-only.",
            spec_path="sql",
            fix="Drop the INTO clause and return the rows instead.",
        )

    # A write can hide inside a CTE while the top level still parses as a Select.
    write_types = tuple(getattr(exp, n) for n in WRITE_NODES if hasattr(exp, n))
    nested = {type(n).__name__ for n in stmt.find_all(*write_types)}
    if nested:
        raise ToolError(
            f"The statement contains {', '.join(sorted(nested)).upper()} inside it, so "
            "it is not read-only.",
            spec_path="sql",
            fix="Remove the writing clause. A CTE that deletes still deletes.",
        )


def dialect_for(data_source: str) -> str | None:
    """The sqlglot dialect for a data source, so parsing matches what will run.

    Parsing MySQL as the generic dialect is usually harmless, but getting it right
    avoids false rejections on backend-specific syntax.
    """
    try:
        doc = frappe.get_cached_doc("Insights Data Source v3", data_source)
        return doc.get_sqlglot_dialect()
    except Exception:
        return None
