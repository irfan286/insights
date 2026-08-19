# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""Argument validation.

Upstream declares an `input_schema` in `tools/list` and then never validates against
it. `frappe_mcp/server/tools/__init__.py:84-88` defines `run_tool()`, which does
jsonschema-validate arguments and filter unknown keys -- but `handle_call_tool` never
calls it; `_get_result` invokes `fn(**arguments)` directly
(`server/tools/handlers.py:49`). `run_tool` is dead code.

Without this decorator: an unknown key raises `TypeError: unexpected keyword
argument`, a missing required argument raises `TypeError`, and a wrong-typed value is
passed straight into our code. All of those land as isError results, so nothing is
unsafe -- but the diagnostics are Python-shaped rather than model-shaped, and a
wrong-typed value can get quite far before it fails.
"""

import functools

from jsonschema import Draft202012Validator

from insights.mcp.errors import ToolError


def tool_args(schema: dict):
    """Validate `tools/call` arguments against the tool's declared input_schema.

    Applied UNDER `@mcp.tool` so the registration decorator sees the wrapper, and the
    SAME schema constant is passed to both -- one source of truth, greppable.

    `functools.wraps` is required, not cosmetic: `get_tool` reads `fn.__name__` and
    `getdoc(fn)` to build the tool's name and description
    (`frappe_mcp/server/tools/__init__.py:58-59`).

    Every tool should also accept `**kwargs` so an unexpected key produces the schema
    error below rather than a raw Python TypeError.
    """
    validator = Draft202012Validator(schema)

    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(**kwargs):
            errors = sorted(validator.iter_errors(kwargs), key=lambda e: list(e.absolute_path))
            if errors:
                first = errors[0]
                path = "/".join(str(p) for p in first.absolute_path) or "<root>"
                raise ToolError(
                    "Invalid arguments.",
                    spec_path=path,
                    fix=first.message,
                )
            return fn(**kwargs)

        return wrapper

    return decorator
