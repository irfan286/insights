# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""Tool registration.

Importing this package runs every `@mcp.tool` decorator in its siblings. It is
imported from `handle_mcp` on first request, not at module scope, so a worker that
never serves MCP never pays for it.

Add each new tool module to the import list below. A module missing from it registers
nothing and fails no test on its own, so `test_guards.py::test_every_tool_module_is_imported`
watches this line.
"""

from insights.mcp.tools import chart, dashboard, discovery, docs, query, sql, workbook  # noqa: F401

__all__ = ["chart", "dashboard", "discovery", "docs", "query", "sql", "workbook"]
