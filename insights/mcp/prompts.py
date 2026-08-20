# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""MCP prompts -- one, and it is a human affordance rather than a grounding channel.

Prompts have the same delivery problem resources do: they are user-invoked, not
model-pulled, so nothing here can ground a query the model is about to write. That job
belongs to `describe_table`'s documentation piggy-back (design §5.4).

What a prompt *can* do is turn "call get_docs first" from an instruction the model may
never receive into a slash command the human presses. That is worth ten lines.

Two upstream facts shape this file:
  * argument metadata is inferred as `{name, required}` only (`prompts/__init__.py:52-59`),
    so descriptions are passed explicitly;
  * a prompt raising is a JSON-RPC protocol error, not an `isError` tool result
    (`prompts/handlers.py:38, 53-56`), so the body stays trivial and side-effect free.
"""

from frappe_mcp import PromptMessage, TextContent
from frappe_mcp.server.prompts import PromptArgument

from insights.mcp import mcp


@mcp.prompt(
    name="explore",
    description="Start a grounded exploration of one data source.",
    arguments=[
        PromptArgument(
            name="data_source",
            description="the data source to explore, e.g. datalake",
            required=True,
        )
    ],
)
def explore(data_source: str):
    """Load a data source's documentation and table index before asking anything of it."""
    return [
        PromptMessage(
            role="user",
            content=TextContent(
                text=(
                    f"Call get_docs('{data_source}') and list_tables('{data_source}'), then "
                    f"summarise what this source contains and what its documented quirks "
                    f"are, and ask me what I want to analyse. Do not write a query until I "
                    f"answer."
                )
            ),
        )
    ]
