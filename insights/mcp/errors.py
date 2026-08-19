# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""Error handling for MCP tools.

Two rules, both forced on us by upstream's behaviour.

**Raise, never return.** `frappe_mcp` sets `isError: true` only by catching an
exception (`server/tools/handlers.py:38-43`); the success path hard-codes
`isError=False` (`:62-64`). A tool that *returns* an error-shaped payload produces a
SUCCESSFUL result that the model will read as data. So a domain failure is
`raise ToolError(...)`, always.

**Every write tool needs `@transactional`.** Because upstream swallows the exception
to build the isError result, Frappe never sees an unhandled exception and its
automatic rollback never fires -- the request completes 200 and Frappe commits at end
of request. A write tool that fails halfway would leave a partial write committed.
"""

import functools
import re
from contextlib import contextmanager

import frappe

# insights/insights/doctype/insights_data_source_v3/ibis_utils.py:113
_POSITION_RE = re.compile(r"operation at position (\d+)", re.IGNORECASE)


class ToolError(Exception):
    """A domain failure, rendered for a model that has to correct itself.

    `__str__` is the entire diagnostic. Upstream interpolates it into the isError text
    block as f"Error calling tool '{name}': {e}", so there is no other channel -- no
    second content block, no structuredContent. Everything the model needs to retry
    successfully goes in here.
    """

    def __init__(
        self,
        message: str,
        *,
        spec_path: str | None = None,
        operation_index: int | None = None,
        valid_columns=None,
        fix: str | None = None,
        docs=None,
    ):
        super().__init__(message)
        self.message = message
        self.spec_path = spec_path
        self.operation_index = operation_index
        self.valid_columns = list(valid_columns) if valid_columns else None
        self.fix = fix
        self.docs = docs

    def __str__(self) -> str:
        lines = [self.message]
        if self.spec_path:
            lines.append(f"spec_path: {self.spec_path}")
        if self.operation_index is not None:
            lines.append(f"operation_index: {self.operation_index}")
        if self.valid_columns:
            lines.append(f"valid_columns: {', '.join(str(c) for c in self.valid_columns)}")
        if self.fix:
            lines.append(f"fix: {self.fix}")
        if self.docs:
            # §5.4 rung 1 -- a failure is the moment the model is most receptive to
            # grounding. Appended as text because upstream builds a single TextContent.
            lines.append("")
            lines.append(self.docs if isinstance(self.docs, str) else "\n\n".join(self.docs))
        return "\n".join(lines)


def transactional(fn):
    """Roll back a half-finished write before the exception is swallowed upstream.

    MANDATORY on every write tool. Applied UNDER @mcp.tool so registration sees the
    wrapper. Asserted by insights/tests/mcp/test_guards.py.
    """

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception:
            frappe.db.rollback()
            raise

    return wrapper


@contextmanager
def capture_build_diagnostics():
    """Collect the toast `IbisQueryBuilder.build` emits, so we can name the failure.

    `build()` catches every per-operation exception, calls `create_toast(...)` naming
    "the {Type} operation at position {idx + 1}", and then re-raises the ORIGINAL
    exception unchanged (ibis_utils.py:106-116). So the operation index exists only in
    a realtime notification and is not recoverable from what we catch.

    `create_toast` (insights/__init__.py:48-62) forwards to `frappe.publish_realtime`
    and returns nothing. This captures that call for the duration of our own build.
    It is our own application's diagnostic channel, not a third-party internal, and it
    degrades to "no position" rather than breaking if the message wording changes.
    """
    captured: list[dict] = []
    original = frappe.publish_realtime

    def recorder(event=None, message=None, *args, **kwargs):
        if event == "insights_notification" and isinstance(message, dict):
            captured.append(message)
        return original(event=event, message=message, *args, **kwargs)

    frappe.publish_realtime = recorder
    try:
        yield captured
    finally:
        frappe.publish_realtime = original


def position_from_diagnostics(captured) -> int | None:
    """Return the 0-based operation index named by a captured build toast."""
    for message in captured or []:
        text = f"{message.get('title', '')} {message.get('message', '')}"
        match = _POSITION_RE.search(frappe.utils.strip_html(text) if text else "")
        if match:
            return int(match.group(1)) - 1  # the toast is 1-based
    return None


def as_tool_error(exc: Exception, *, spec_paths=None, captured=None, docs=None) -> ToolError:
    """Translate a backend exception into a model-actionable ToolError.

    `spec_paths` is the compiler's {operation_index: spec_path} map -- free to build at
    emit time and impossible to reconstruct afterwards.
    """
    if isinstance(exc, ToolError):
        return exc

    index = position_from_diagnostics(captured)
    spec_path = (spec_paths or {}).get(index) if index is not None else None
    message = frappe.utils.strip_html(str(exc)) or exc.__class__.__name__

    fix = None
    if isinstance(exc, frappe.PermissionError):
        fix = "You do not have access to this resource. Ask an Insights administrator."
    elif "CircularQueryReference" in exc.__class__.__name__:
        fix = "Break the cycle by sourcing from a table instead of the saved query named above."
    elif isinstance(exc, TypeError):
        fix = "An argument had the wrong type. Check the tool's input schema."

    return ToolError(
        message,
        spec_path=spec_path,
        operation_index=index,
        fix=fix,
        docs=docs,
    )
