# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""QuerySpec -> Insights v3 `operations[]`, plus a symbol table.

`compile(spec, resolver=...) -> (operations, SymbolTable)` is a pure function. The
symbol table is the authority on a query's output columns at each stage; Phase 2
resolves `ChartSpec` columns against it, which is what lets a chart be built before its
query is ever saved.

WHY THIS LAYER EXISTS AT ALL. `perform_operation` is an if/elif chain that falls
through to `return self.query` on an unrecognised `type` (`ibis_utils.py:157`), and
every operation is deep-converted to `frappe._dict` first (`:106`) so a missing key
reads as `None` rather than raising. Handing raw `operations[]` to a model therefore
produces *successful wrong answers*. Every value this module emits is validated against
a literal vocabulary first, and wrong casing is REJECTED rather than corrected, so the
model learns instead of silently mis-querying.

The traps below are all measured, not assumed. See docs/mcp-IMPLEMENTATION.md §8 C/M/N.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

from insights.mcp.errors import ToolError
from insights.mcp.schemas import (
    AGGREGATIONS,
    CAST_TYPES,
    FILTER_OPERATORS,
    GRANULARITIES,
    JOIN_TYPES,
)

# `to_insights_type` (ibis_utils.py:1037-1058) emits exactly these nine.
RESOLVED_TYPES = ("Boolean", "String", "Integer", "Decimal", "Datetime", "Date", "Time", "JSON", "Array")
NUMERIC_TYPES = ("Integer", "Decimal")
TEMPORAL_TYPES = ("Date", "Datetime", "Time")
STRINGY_TYPES = ("String", "Text")
# DimensionDataType / MeasureDataType (query.types.ts:48-49)
MEASURE_TYPES = ("String", "Integer", "Decimal")
# apply_time_granularity (ibis_utils.py:894-911) accepts only these three.
TIME_GRANULARITIES = ("second", "minute", "hour")
# Operators that take no value; emitting a dict here would make the backend resolve it
# as a column reference (ibis_utils.py:389-393) even though the value is unused.
VALUELESS_OPERATORS = ("is_set", "is_not_set", "is_true", "is_false", "is_not_true")
LIST_OPERATORS = ("in", "not_in")
MAX_LIMIT = 1_000_000
# Types `ibis/utils.py::get_ibis_dtype` can actually resolve. Boolean is absent from
# that map, so declaring it produces a null dtype and a crash in validate_types.
_VALIDATOR_TYPES = ("String", "Text", "Integer", "Decimal", "Date", "Datetime", "Time", "JSON", "Array")
MAX_FILTER_GROUP_DEPTH = 8


# --------------------------------------------------------------------------- #
# symbol table
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Col:
    name: str
    data_type: str
    role: str = "raw"          # raw | dimension | measure | derived
    origin: str | None = None  # "demo_data.orders.order_id" -- for error messages
    volatile: bool = False     # backend may rename this; excluded from .names

    def to_json(self) -> dict:
        return {"name": self.name, "data_type": self.data_type, "role": self.role}


@dataclass
class Stage:
    op_index: int
    kind: str
    columns: tuple[Col, ...]


class SymbolTable:
    """Output columns at each stage of the pipeline.

    Stages rather than a flat list, because the single most valuable error message this
    layer can produce is *"`price` existed before the summarize at position 5 but not
    after it"* -- and a flat table cannot say where a column went.
    """

    def __init__(self, columns: list[Col] | None = None):
        self.stages: list[Stage] = []
        self.spec_paths: dict[int, str] = {}
        if columns is not None:
            self.push(-1, "source", columns)

    # -- state ------------------------------------------------------------- #

    @property
    def columns(self) -> tuple[Col, ...]:
        return self.stages[-1].columns if self.stages else ()

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.columns if not c.volatile)

    @property
    def post_aggregate(self) -> bool:
        return any(s.kind in ("summarize", "pivot_wider") for s in self.stages)

    def measures(self) -> tuple[Col, ...]:
        return tuple(c for c in self.columns if c.role == "measure")

    def dimensions(self) -> tuple[Col, ...]:
        return tuple(c for c in self.columns if c.role == "dimension")

    def get(self, name: str) -> Col | None:
        for col in self.columns:
            if col.name == name:
                return col
        return None

    def push(self, op_index: int, kind: str, columns) -> None:
        self.stages.append(Stage(op_index, kind, tuple(columns)))

    # -- validation -------------------------------------------------------- #

    def require(self, name: str, *, spec_path: str) -> Col:
        col = self.get(name)
        if col is not None and not col.volatile:
            return col

        dropped_at = None
        for stage in reversed(self.stages[:-1]):
            if any(c.name == name for c in stage.columns):
                dropped_at = stage
                break

        message = f"Column '{name}' does not exist at this point in the query."
        fix = None
        if dropped_at is not None:
            later = self.stages[self.stages.index(dropped_at) + 1]
            message = (
                f"Column '{name}' existed before the {later.kind} step "
                f"(operation {later.op_index}) but not after it."
            )
            if later.kind in ("summarize", "pivot_wider"):
                fix = (
                    "After aggregation only the group_by and aggregate output aliases "
                    "exist. Reference one of those, or add this column to group_by."
                )
        elif col is not None and col.volatile:
            message = (
                f"Column '{name}' is the right-hand side of a join key. The backend "
                f"renames it unpredictably, so it is not addressable."
            )
            fix = "Reference the left-hand column of the join instead."

        raise ToolError(message, spec_path=spec_path, valid_columns=self.names, fix=fix)

    def to_json(self) -> list[dict]:
        return [c.to_json() for c in self.columns if not c.volatile]

    @classmethod
    def from_json(cls, rows) -> SymbolTable:
        """Rehydrate from a prior run_query response -- the Phase 2 entry point."""
        return cls([
            Col(name=r["name"], data_type=r.get("data_type", "String"), role=r.get("role", "raw"))
            for r in rows
        ])


# --------------------------------------------------------------------------- #
# schema resolution
# --------------------------------------------------------------------------- #


class SchemaResolver(Protocol):
    def table_columns(self, data_source: str, table: str) -> tuple[Col, ...]: ...
    def query_columns(self, query_name: str) -> tuple[Col, ...]: ...


class LiveSchemaResolver:
    """Wraps `get_data_source_table_columns`, which emits {column, label, type}.

    NOTE: that endpoint emits key `column`, not `name` -- the `name` key belongs to
    `get_columns_from_schema`, which is the query *result* shape. Design §4.5 conflates
    the two; see docs/mcp-IMPLEMENTATION.md §8 N.
    """

    def table_columns(self, data_source: str, table: str) -> tuple[Col, ...]:
        from insights.api.data_sources import get_data_source_table_columns

        try:
            rows = get_data_source_table_columns(data_source, table)
        except Exception as exc:
            raise ToolError(
                f"Could not read the schema of '{table}' in '{data_source}'.",
                fix="Check the table name with list_tables, and the source with list_data_sources.",
            ) from exc

        return tuple(
            Col(
                name=r["column"],
                data_type=r.get("type") or "String",
                origin=f"{data_source}.{table}.{r['column']}",
            )
            for r in rows
        )

    def query_columns(self, query_name: str) -> tuple[Col, ...]:
        import frappe

        from insights.insights.doctype.insights_data_source_v3.ibis_utils import (
            get_columns_from_schema,
        )

        try:
            doc = frappe.get_doc("Insights Query v3", query_name)
            schema = doc.build().schema()
        except Exception as exc:
            raise ToolError(
                f"Could not resolve the output columns of saved query '{query_name}'.",
            ) from exc

        return tuple(
            Col(name=c["name"], data_type=c["type"], origin=f"query:{query_name}.{c['name']}")
            for c in get_columns_from_schema(schema)
        )


class StaticSchemaResolver:
    """Test double. `{(data_source, table): [(name, type), ...]}`."""

    def __init__(self, tables: dict, queries: dict | None = None):
        self._tables = tables
        self._queries = queries or {}

    def table_columns(self, data_source: str, table: str) -> tuple[Col, ...]:
        try:
            rows = self._tables[(data_source, table)]
        except KeyError:
            raise ToolError(f"Unknown table '{table}' in data source '{data_source}'.") from None
        return tuple(Col(name=n, data_type=t, origin=f"{data_source}.{table}.{n}") for n, t in rows)

    def query_columns(self, query_name: str) -> tuple[Col, ...]:
        try:
            rows = self._queries[query_name]
        except KeyError:
            raise ToolError(f"Unknown saved query '{query_name}'.") from None
        return tuple(Col(name=n, data_type=t, origin=f"query:{query_name}.{n}") for n, t in rows)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _sanitize_name(name: str) -> str:
    """Mirror of `sanitize_name` (ibis_utils.py:1123-1135). It LOWERCASES.

    Every downstream reference must use the sanitized form, because `mutate` and
    `rename` store the sanitized name and the model's original casing will not resolve.
    """
    out = str(name).strip()
    for ch in (" ", "-", ".", "/", "(", ")"):
        out = out.replace(ch, "_")
    return out.lower()


def _column_ref(name: str) -> dict:
    return {"type": "column", "column_name": name}


def _reject_enum(value, allowed, *, spec_path: str, label: str):
    if value in allowed:
        return value
    lowered = {str(a).lower(): a for a in allowed}
    fix = f"Use one of: {', '.join(str(a) for a in allowed)}."
    if isinstance(value, str) and value.lower() in lowered:
        # Deliberately NOT auto-corrected -- §6.3 validation rule 4.
        fix = f"Casing matters here. Did you mean '{lowered[value.lower()]}'?"
    raise ToolError(f"Unsupported {label}: {value!r}.", spec_path=spec_path, fix=fix)


def _measure_data_type(source_type: str | None, fn: str) -> str:
    if fn in ("count", "count_distinct"):
        return "Integer"
    if source_type in MEASURE_TYPES:
        return source_type
    if source_type in NUMERIC_TYPES:
        return source_type
    return "Decimal" if fn in ("sum", "avg") else "String"


def _dimension_data_type(source_type: str | None) -> str:
    if source_type == "Text":
        return "String"
    return source_type or "String"



def filter_rule(rule: dict, symbols: SymbolTable, path: str) -> dict:
    """One filter, resolved against `symbols`.

    Module level, not a method, because `chartspec.py` needs the identical operator
    semantics for a chart's own filters. A second copy would drift on exactly the cases
    that are already subtle -- the valueless operators, `between`'s pair, `within`'s
    timespan string.
    """
    name = rule.get("column")
    col = symbols.require(name, spec_path=f"{path}.column")

    operator = _reject_enum(
        rule.get("op"), FILTER_OPERATORS, spec_path=f"{path}.op", label="filter operator"
    )
    value = rule.get("value")

    if operator in VALUELESS_OPERATORS:
        # The backend still reads `value` (ibis_utils.py:381) and, if it is a dict
        # with column_name, resolves it as a column -- which can throw for an
        # operator that does not use it. Always emit null.
        value = None
    elif operator in LIST_OPERATORS:
        if not isinstance(value, list):
            raise ToolError(
                f"Operator '{operator}' needs a list of values.",
                spec_path=f"{path}.value",
            )
    elif operator == "between":
        if not isinstance(value, list) or len(value) != 2:
            raise ToolError(
                "Operator 'between' needs exactly two values: [start, end].",
                spec_path=f"{path}.value",
            )
    elif operator == "within":
        if not isinstance(value, (str, list)):
            raise ToolError(
                "Operator 'within' needs a timespan string such as 'Last 7 days'.",
                spec_path=f"{path}.value",
            )
    elif value is None:
        raise ToolError(
            f"Operator '{operator}' needs a value.",
            spec_path=f"{path}.value",
            fix=f"Use one of {', '.join(VALUELESS_OPERATORS)} to test for emptiness.",
        )
    elif operator in ("contains", "not_contains") and not isinstance(value, str):
        # apply_filter does value.replace("%", "") -> AttributeError on a number.
        raise ToolError(
            f"Operator '{operator}' needs a string value.", spec_path=f"{path}.value"
        )

    # The operation key is `operator`; the spec key is `op`. Not a typo.
    return {"column": _column_ref(col.name), "operator": operator, "value": value}


# --------------------------------------------------------------------------- #
# the compiler
# --------------------------------------------------------------------------- #


def compile(spec: dict, *, resolver: SchemaResolver | None = None) -> tuple[list[dict], SymbolTable]:
    return _Compiler(spec, resolver or LiveSchemaResolver()).run()


class _Compiler:
    def __init__(self, spec: dict, resolver: SchemaResolver):
        self.spec = spec or {}
        self.resolver = resolver
        self.ops: list[dict] = []
        self.symbols = SymbolTable()
        self.data_source: str | None = None
        self.emitted_join = False

    # -- emission ---------------------------------------------------------- #

    def emit(self, op: dict, *, spec_path: str, kind: str, columns) -> None:
        self.symbols.spec_paths[len(self.ops)] = spec_path
        self.symbols.push(len(self.ops), kind, columns)
        self.ops.append(op)

    def run(self) -> tuple[list[dict], SymbolTable]:
        self._source()
        self._joins()
        self._casts()
        self._filters()
        self._derive()
        self._rename()
        # Auto-cast runs HERE, not with the explicit casts at step 3: §6.3 says
        # "immediately before the summarize", and a group_by may legitimately target a
        # column produced by `derive` or renamed by `rename`, neither of which exists
        # yet at step 3. Caught by test_compiler_integration.
        self._auto_cast()
        self._aggregate()
        self._having()
        self._sort()
        self._limit()
        self._select()
        return self.ops, self.symbols

    # -- 1. source --------------------------------------------------------- #

    def _source(self) -> None:
        frm = self.spec.get("from")
        if not isinstance(frm, dict) or not frm:
            raise ToolError(
                "`from` is required.",
                spec_path="from",
                fix='Supply {"data_source": ..., "table": ...} or {"query": "<saved query name>"}.',
            )

        if frm.get("query"):
            columns = self.resolver.query_columns(frm["query"])
            # `workbook` is declared in the TS type but never read by the backend
            # (ibis_utils.py:169-171). Emitted as "" for shape compatibility.
            op = {"type": "source", "table": {"type": "query", "query_name": frm["query"], "workbook": ""}}
        else:
            data_source, table = frm.get("data_source"), frm.get("table")
            if not data_source or not table:
                raise ToolError(
                    "`from` needs both `data_source` and `table` (or a `query`).",
                    spec_path="from",
                    fix="Call list_data_sources then list_tables to get valid names.",
                )
            self.data_source = data_source
            columns = self.resolver.table_columns(data_source, table)
            op = {
                "type": "source",
                "table": {"type": "table", "data_source": data_source, "table_name": table},
            }

        self.emit(op, spec_path="from", kind="source", columns=columns)

    # -- 2. joins ---------------------------------------------------------- #

    def _joins(self) -> None:
        for i, join in enumerate(self.spec.get("joins") or []):
            path = f"joins[{i}]"
            table = join.get("table")
            if not table:
                raise ToolError("A join needs a `table`.", spec_path=f"{path}.table")

            data_source = join.get("data_source") or self.data_source
            if self.data_source and data_source != self.data_source:
                # Cross-source joins fail at EXECUTE time with a hard-coded Indonesian
                # message (ibis_utils.py:963-973), long after compilation succeeds --
                # so this must be caught here or not at all. See §8 N.
                raise ToolError(
                    f"Cannot join '{table}' from '{data_source}' to a query on "
                    f"'{self.data_source}'. Joins across data sources are not supported "
                    f"unless both tables are synced to the warehouse.",
                    spec_path=f"{path}.data_source",
                    fix="Query one data source at a time.",
                )

            how = _reject_enum(join.get("how", "left"), JOIN_TYPES, spec_path=f"{path}.how", label="join type")
            left_on, right_on = join.get("left_on"), join.get("right_on")
            if not left_on or not right_on:
                raise ToolError(
                    "A join needs both `left_on` and `right_on`.", spec_path=path
                )
            self.symbols.require(left_on, spec_path=f"{path}.left_on")

            right = self.resolver.table_columns(data_source, table)
            right_by_name = {c.name: c for c in right}
            if right_on not in right_by_name:
                raise ToolError(
                    f"Column '{right_on}' does not exist on '{table}'.",
                    spec_path=f"{path}.right_on",
                    valid_columns=[c.name for c in right],
                )

            wanted = join.get("select")
            kept = [right_by_name[c] for c in wanted] if wanted else list(right)
            if wanted:
                for c in wanted:
                    if c not in right_by_name:
                        raise ToolError(
                            f"Column '{c}' does not exist on '{table}'.",
                            spec_path=f"{path}.select",
                            valid_columns=[x.name for x in right],
                        )

            left_names = set(self.symbols.names)
            # The join key always collides (same name on both sides is the normal case)
            # and the backend force-adds it anyway (ibis_utils.py:250-251), so it is
            # handled separately below. Any OTHER collision is unpredictable, because
            # the renamed form depends on get_ibis_table_name, which is not the remote
            # table name for warehouse-backed tables.
            clashes = sorted({c.name for c in kept} & left_names - {right_on})
            if clashes:
                raise ToolError(
                    f"Joining '{table}' would produce duplicate column names: "
                    f"{', '.join(clashes)}.",
                    spec_path=f"{path}.select",
                    fix=(
                        "List only the right-table columns you need in `select`, or "
                        "rename the colliding ones first."
                    ),
                )

            op = {
                "type": "join",
                "join_type": how,
                "table": {"type": "table", "data_source": data_source, "table_name": table},
                "select_columns": [_column_ref(c.name) for c in kept],
                "join_condition": {
                    "left_column": _column_ref(left_on),
                    "right_column": _column_ref(right_on),
                },
            }

            # MEASURED (§8 C): the backend force-adds the right join key to the
            # projection and renames it "{righttable}_{col}". It appears in the output
            # even though nobody asked for it. Marked volatile so it is invisible to
            # the model, and dropped by the trailing select in _select().
            columns = list(self.symbols.columns)
            columns += [c for c in kept if c.name != right_on]
            columns.append(Col(name=f"{_sanitize_name(table)}_{right_on}", data_type=right_by_name[right_on].data_type, volatile=True))
            self.emit(op, spec_path=path, kind="join", columns=columns)
            self.emitted_join = True

    # -- 3. casts (explicit + auto) ---------------------------------------- #

    def _casts(self) -> None:
        """Explicit `cast[]` only. The auto-cast rule runs later -- see run()."""
        for i, cast in enumerate(self.spec.get("cast") or []):
            path = f"cast[{i}]"
            column = cast.get("column")
            col = self.symbols.require(column, spec_path=f"{path}.column")
            data_type = _reject_enum(
                cast.get("data_type"), CAST_TYPES, spec_path=f"{path}.data_type", label="data type"
            )
            self._emit_cast(col.name, data_type, path)

    def _emit_cast(self, name: str, data_type: str, spec_path: str) -> None:
        # "Auto" maps to "" in get_ibis_dtype and apply_cast passes it straight into
        # query.cast({col: ""}) with no falsy guard (ibis_utils.py:500) -> ibis error.
        # CAST_TYPES excludes it, so this is belt-and-braces.
        if data_type == "Auto":
            raise ToolError("`Auto` is not a valid cast target.", spec_path=spec_path)
        op = {"type": "cast", "column": _column_ref(name), "data_type": data_type}
        columns = [
            Col(name=c.name, data_type=data_type if c.name == name else c.data_type,
                role=c.role, origin=c.origin, volatile=c.volatile)
            for c in self.symbols.columns
        ]
        self.emit(op, spec_path=spec_path, kind="cast", columns=columns)

    def _auto_cast(self) -> None:
        """§6.3, all four branches. Runs immediately before summarize/pivot_wider."""
        for i, group in enumerate(self.spec.get("group_by") or []):
            granularity = group.get("granularity")
            if not granularity:
                continue  # branch 4: nothing to strip -- we simply never emit it

            path = f"group_by[{i}]"
            col = self.symbols.require(group.get("column"), spec_path=f"{path}.column")

            if col.data_type in TEMPORAL_TYPES:
                continue  # branch 2: already temporal, casting would be a wasted op

            if col.data_type in NUMERIC_TYPES:
                # branch 3: do not guess
                raise ToolError(
                    f"Column '{col.name}' is {col.data_type}, so it cannot be grouped by "
                    f"'{granularity}'.",
                    spec_path=f"{path}.granularity",
                    fix="Remove `granularity`, or group by a Date/Datetime/Time column.",
                )

            if col.data_type in STRINGY_TYPES:
                self._emit_cast(col.name, "Datetime", f"{path}.granularity")
                continue

            raise ToolError(
                f"Column '{col.name}' is {col.data_type}, which cannot carry a granularity.",
                spec_path=f"{path}.granularity",
                fix="Remove `granularity`.",
            )

    # -- 4. filters -------------------------------------------------------- #

    def _filters(self) -> None:
        groups = []
        if self.spec.get("where"):
            groups.append(("And", self.spec["where"], "where"))
        if self.spec.get("where_any"):
            groups.append(("Or", self.spec["where_any"], "where_any"))

        for logical_operator, filters, path in groups:
            rules = [self._filter_rule(f, f"{path}[{i}]") for i, f in enumerate(filters)]
            op = {"type": "filter_group", "logical_operator": logical_operator, "filters": rules}
            self.emit(op, spec_path=path, kind="filter_group", columns=self.symbols.columns)

    def _filter_rule(self, rule: dict, path: str) -> dict:
        return filter_rule(rule, self.symbols, path)

    # -- 5. derive --------------------------------------------------------- #

    def _derive(self) -> None:
        for i, derive in enumerate(self.spec.get("derive") or []):
            path = f"derive[{i}]"
            raw_name = derive.get("name")
            expression = derive.get("expression")
            if not raw_name or not expression:
                raise ToolError("`derive` needs `name` and `expression`.", spec_path=path)

            self._validate_expression(expression, f"{path}.expression")

            # sanitize_name lowercases; the stored column uses the sanitized form, so
            # the symbol table must too or every later reference misses.
            name = _sanitize_name(raw_name)
            data_type = derive.get("data_type", "Auto")
            if data_type not in (*CAST_TYPES, "Auto"):
                _reject_enum(data_type, (*CAST_TYPES, "Auto"), spec_path=f"{path}.data_type", label="data type")

            op = {
                "type": "mutate",
                "new_name": name,
                # `data_type` is mandatory for mutate -- get_ibis_dtype(None) is a
                # KeyError. "Auto" is the only value that skips the cast.
                "data_type": data_type,
                "expression": {"type": "expression", "expression": expression},
            }
            columns = [*self.symbols.columns, Col(name=name, data_type=data_type if data_type != "Auto" else "String", role="derived")]
            self.emit(op, spec_path=path, kind="mutate", columns=columns)

    def _validate_expression(self, expression: str, spec_path: str) -> None:
        """Syntax + unknown-column check. Type checking is unavailable here -- see §8 M.

        `validate_expression` runs the expression through `safe_exec`, and server
        scripts are disabled on this deployment, so the type stage fails for EVERY
        expression including valid ones. Trusting that verdict would reject every
        `derive`. Syntax and name errors are raised before that stage and are reliable.
        """
        from frappe.utils.safe_exec import is_safe_exec_enabled

        from insights.insights.doctype.insights_data_source_v3.ibis.utils import (
            validate_expression,
        )

        # NOTE the key is `description`, not `data_type`. get_ibis_dtype reads
        # col["description"]; the wrong key yields an empty schema and validation
        # silently passes everything (ibis/utils.py:213-230, 368-371).
        #
        # And `description` is OMITTED for any type that map does not know --
        # notably Boolean, which it simply lacks (ibis/utils.py:214-225). Sending it
        # yields dtype None, and `ibis.table(schema)` then raises a wall of signature
        # noise instead of a usable error. `parse_column_metadata` only requires
        # `value`, so omitting the type keeps the column visible to NAME validation
        # while excusing it from the type stage. Measured: two chained `derive`s
        # producing Booleans crashed before this.
        options = json.dumps([
            {"value": c.name, **({"description": c.data_type} if c.data_type in _VALIDATOR_TYPES else {})}
            for c in self.symbols.columns
        ])
        try:
            result = validate_expression(expression, options)
        except Exception:
            return  # never let the validator's own failure block a query

        if result.get("is_valid"):
            return

        errors = result.get("errors") or []
        message = errors[0].get("message") if errors else "Invalid expression."
        if not is_safe_exec_enabled() and "Server Scripts are disabled" in str(message):
            return  # type stage unavailable; syntax and names already passed

        raise ToolError(
            f"Invalid expression: {message}",
            spec_path=spec_path,
            valid_columns=self.symbols.names,
            fix=errors[0].get("hint") if errors and errors[0].get("hint") else None,
        )

    # -- 6. rename --------------------------------------------------------- #

    def _rename(self) -> None:
        for i, ren in enumerate(self.spec.get("rename") or []):
            path = f"rename[{i}]"
            col = self.symbols.require(ren.get("column"), spec_path=f"{path}.column")
            new_name = ren.get("as")
            if not new_name:
                raise ToolError("`rename` needs `as`.", spec_path=f"{path}.as")
            sanitized = _sanitize_name(new_name)

            op = {"type": "rename", "column": _column_ref(col.name), "new_name": sanitized}
            columns = [
                Col(name=sanitized if c.name == col.name else c.name, data_type=c.data_type,
                    role=c.role, origin=c.origin, volatile=c.volatile)
                for c in self.symbols.columns
            ]
            self.emit(op, spec_path=path, kind="rename", columns=columns)

    # -- 7. aggregate ------------------------------------------------------ #

    def _aggregate(self) -> None:
        group_by = self.spec.get("group_by") or []
        aggregate = self.spec.get("aggregate") or []
        pivot_on = self.spec.get("pivot_on")

        if not group_by and not aggregate:
            if pivot_on:
                raise ToolError(
                    "`pivot_on` needs `group_by` and `aggregate` as well.", spec_path="pivot_on"
                )
            return

        dimensions = [self._dimension(g, f"group_by[{i}]") for i, g in enumerate(group_by)]
        measures = [self._measure(a, f"aggregate[{i}]") for i, a in enumerate(aggregate)]

        seen = {}
        for m in measures:
            # apply_summary keys aggregates by measure_name (ibis_utils.py:527), so
            # duplicates silently collapse -- last one wins, no error.
            if m["measure_name"] in seen:
                raise ToolError(
                    f"Two aggregates would both be called '{m['measure_name']}'.",
                    spec_path="aggregate",
                    fix="Give one of them a distinct `as`.",
                )
            seen[m["measure_name"]] = True

        out = [Col(name=d["dimension_name"], data_type=d["data_type"], role="dimension") for d in dimensions]
        out += [Col(name=m["measure_name"], data_type=m["data_type"], role="measure") for m in measures]

        if pivot_on:
            self._pivot(pivot_on, dimensions, measures, out)
            return

        op = {"type": "summarize", "measures": measures, "dimensions": dimensions}
        self.emit(op, spec_path="aggregate", kind="summarize", columns=out)

    def _dimension(self, group: dict, path: str) -> dict:
        col = self.symbols.require(group.get("column"), spec_path=f"{path}.column")
        alias = group.get("as") or col.name
        data_type = _dimension_data_type(col.data_type)

        dimension = {
            "dimension_name": alias,
            "column_name": col.name,
            "data_type": data_type,
        }

        granularity = group.get("granularity")
        if granularity:
            _reject_enum(granularity, GRANULARITIES, spec_path=f"{path}.granularity", label="granularity")
            if data_type == "Time" and granularity not in TIME_GRANULARITIES:
                raise ToolError(
                    f"Time columns only support {', '.join(TIME_GRANULARITIES)} granularity, "
                    f"not '{granularity}'.",
                    spec_path=f"{path}.granularity",
                )
            # Granularity on a non-temporal dimension is SILENTLY IGNORED by
            # translate_dimension (ibis_utils.py:829-835) -- a wrong-but-successful
            # result. _auto_cast has already made stringy columns Datetime, so
            # anything still non-temporal here is a bug we refuse to emit.
            if data_type in TEMPORAL_TYPES:
                dimension["granularity"] = granularity

        return dimension

    def _measure(self, agg: dict, path: str) -> dict:
        fn = agg.get("fn")
        column = agg.get("column")
        expression = agg.get("expression")
        alias = agg.get("as")

        if expression:
            if not alias:
                raise ToolError(
                    "An expression aggregate needs an `as` name.", spec_path=f"{path}.as"
                )
            self._validate_expression(expression, f"{path}.expression")
            # ExpressionMeasure is detected by KEY PRESENCE (`"expression" in measure`,
            # ibis_utils.py:816), so the key must be absent -- never null -- on a
            # column measure. data_type IS applied here, unguarded.
            return {
                "measure_name": alias,
                "expression": {"type": "expression", "expression": expression},
                "data_type": agg.get("data_type") if agg.get("data_type") in MEASURE_TYPES else "Decimal",
            }

        if not fn:
            raise ToolError("An aggregate needs `fn`.", spec_path=f"{path}.fn")
        _reject_enum(fn, AGGREGATIONS, spec_path=f"{path}.fn", label="aggregation")

        if not column:
            if fn != "count":
                raise ToolError(
                    f"`{fn}` needs a `column`.",
                    spec_path=f"{path}.column",
                    fix="Only `count` may omit the column (it counts rows).",
                )
            # The count-star sentinel the backend special-cases
            # (ibis_utils.py:811-814). Caveat: it counts the FIRST column, not *, so
            # nulls there undercount. Nothing we can do from here.
            return {
                "measure_name": alias or "count_of_rows",
                "column_name": "count",
                "aggregation": "count",
                "data_type": "Integer",
            }

        col = self.symbols.require(column, spec_path=f"{path}.column")
        if col.data_type in ("JSON", "Array"):
            raise ToolError(
                f"Column '{col.name}' is {col.data_type} and cannot be aggregated.",
                spec_path=f"{path}.column",
            )
        return {
            "measure_name": alias or f"{fn}_of_{col.name}",
            "column_name": col.name,
            "aggregation": fn,
            "data_type": _measure_data_type(col.data_type, fn),
        }

    def _pivot(self, pivot_on: dict, dimensions, measures, out) -> None:
        column = pivot_on.get("column")
        if not column:
            raise ToolError("`pivot_on` needs a `column`.", spec_path="pivot_on.column")

        pivot_dim = next((d for d in dimensions if d["dimension_name"] == column or d["column_name"] == column), None)
        if pivot_dim is None:
            raise ToolError(
                f"`pivot_on.column` must also appear in `group_by`. '{column}' does not.",
                spec_path="pivot_on.column",
                valid_columns=[d["dimension_name"] for d in dimensions],
            )
        rows = [d for d in dimensions if d is not pivot_dim]
        if not rows:
            raise ToolError(
                "A pivot needs at least one other `group_by` column to form the rows.",
                spec_path="group_by",
            )

        max_values = pivot_on.get("max_values", 10)
        # apply_pivot does int(max_column_values) with no clamp() safety, so an explicit
        # null is a TypeError. rows/columns/values are bracket-accessed -> KeyError if
        # absent. Emit all three, never null.
        op = {
            "type": "pivot_wider",
            "rows": rows,
            "columns": [pivot_dim],
            "values": measures,
            "max_column_values": int(max_values) if max_values is not None else 10,
        }
        # Output column names become "{value}___{measure_name}" (names_sep="___"), which
        # we cannot know without executing. The symbol table keeps the row dimensions
        # only, and downstream sort/having against pivoted columns is refused.
        columns = [c for c in out if c.role == "dimension" and c.name != pivot_dim["dimension_name"]]
        self.emit(op, spec_path="pivot_on", kind="pivot_wider", columns=columns)

    # -- 8. having --------------------------------------------------------- #

    def _having(self) -> None:
        having = self.spec.get("having") or []
        if not having:
            return
        if not self.symbols.post_aggregate:
            raise ToolError(
                "`having` filters aggregated results, but this query has no `aggregate`.",
                spec_path="having",
                fix="Use `where` for filters that apply before aggregation.",
            )
        rules = [self._filter_rule(f, f"having[{i}]") for i, f in enumerate(having)]
        op = {"type": "filter_group", "logical_operator": "And", "filters": rules}
        self.emit(op, spec_path="having", kind="filter_group", columns=self.symbols.columns)

    # -- 9. sort ----------------------------------------------------------- #

    def _sort(self) -> None:
        for i, sort in enumerate(self.spec.get("sort") or []):
            path = f"sort[{i}]"
            col = self.symbols.require(sort.get("column"), spec_path=f"{path}.column")
            # apply_order_by tests `direction == "asc"` and sends EVERYTHING else to
            # descending (ibis_utils.py:535) -- so this must be exactly "asc"/"desc".
            direction = "desc" if sort.get("desc") else "asc"
            op = {"type": "order_by", "column": _column_ref(col.name), "direction": direction}
            self.emit(op, spec_path=path, kind="order_by", columns=self.symbols.columns)

    # -- 10. limit --------------------------------------------------------- #

    def _limit(self) -> None:
        limit = self.spec.get("limit")
        if limit is None:
            return
        try:
            value = int(limit)
        except (TypeError, ValueError):
            raise ToolError("`limit` must be a whole number.", spec_path="limit") from None
        if value < 1:
            raise ToolError("`limit` must be at least 1.", spec_path="limit")
        # clamp() swallows TypeError/ValueError and returns the LOWER bound, so a bad
        # limit silently becomes LIMIT 1 (ibis_utils.py:539, 934-938). Validated above
        # so that can never happen through this path.
        op = {"type": "limit", "limit": min(value, MAX_LIMIT)}
        self.emit(op, spec_path="limit", kind="limit", columns=self.symbols.columns)

    # -- 11. select -------------------------------------------------------- #

    def _select(self) -> None:
        wanted = self.spec.get("select")
        if wanted:
            cols = [self.symbols.require(n, spec_path=f"select[{i}]") for i, n in enumerate(wanted)]
            op = {"type": "select", "column_names": [c.name for c in cols]}
            self.emit(op, spec_path="select", kind="select", columns=cols)
            return

        # No explicit select. A summarize/pivot already projects to exactly its outputs,
        # so ordering is pinned and nothing junk survives. After a bare join, though,
        # the backend leaves behind the force-added right join key (§8 C) AND builds the
        # projection from a Python set, so column order is non-deterministic between
        # identical runs (ibis_utils.py:245, 267). One extra operation fixes both.
        if self.emitted_join and not self.symbols.post_aggregate:
            names = list(self.symbols.names)
            op = {"type": "select", "column_names": names}
            self.emit(op, spec_path="joins", kind="select",
                      columns=[c for c in self.symbols.columns if not c.volatile])


# --------------------------------------------------------------------------- #
# operations -> QuerySpec  (the inverse, best effort and honest about it)
# --------------------------------------------------------------------------- #

DECOMPILABLE_TYPES = (
    "source", "join", "cast", "filter_group", "mutate", "rename",
    "summarize", "pivot_wider", "order_by", "limit", "select",
)

# Canonical emission order, from §6.3. A query whose operations do not follow it may still
# be perfectly valid -- it is just not something QuerySpec can express, because the DSL
# encodes the order in its field names.
_RANK = {
    "source": 0, "join": 1, "cast": 2, "filter_group": 3, "mutate": 4, "rename": 5,
    "summarize": 6, "pivot_wider": 6, "order_by": 8, "limit": 9, "select": 10,
}


def decompile(operations) -> tuple[dict | None, str | None]:
    """`operations[]` -> `(QuerySpec, None)` or `(None, reason)`.

    Deliberately eager to give up. A lossy spec is worse than no spec here: the model
    edits what it is handed and submits it back, so an approximation becomes a wrong
    query rather than a wrong reading. The raw operations are returned alongside either
    way, so `None` costs the caller nothing.
    """
    if isinstance(operations, str):
        # `json`, not `frappe.parse_json`: this module imports no frappe at all, which is
        # what makes test_compiler.py a fixture-free UnitTestCase. Malformed JSON comes
        # back as a reason rather than an exception -- decompile never raises.
        try:
            operations = json.loads(operations)
        except ValueError:
            return None, "the stored operations are not valid JSON"

    ops = operations or []
    if not ops:
        return None, "the query has no operations"

    for op in ops:
        kind = op.get("type")
        if kind not in DECOMPILABLE_TYPES:
            return None, f"contains a {kind} operation -- not expressible in QuerySpec"

    if ops[0]["type"] != "source":
        return None, "does not begin with a source"

    aggregations = [op for op in ops if op["type"] in ("summarize", "pivot_wider")]
    if len(aggregations) > 1:
        return None, "contains two aggregation steps"

    spec = {}
    aggregated = False
    rank = -1

    for op in ops:
        kind = op["type"]
        current = _RANK[kind] + (4 if kind == "filter_group" and aggregated else 0)
        if current < rank:
            return None, "the operations are not in an order QuerySpec can express"
        rank = current

        if kind == "source":
            table = op.get("table") or {}
            if table.get("type") == "query":
                spec["from"] = {"query": table.get("query_name")}
            else:
                spec["from"] = {
                    "data_source": table.get("data_source"),
                    "table": table.get("table_name"),
                }

        elif kind == "join":
            table = op.get("table") or {}
            condition = op.get("join_condition") or {}
            spec.setdefault("joins", []).append({
                "table": table.get("table_name"),
                "data_source": table.get("data_source"),
                "how": op.get("join_type", "left"),
                "left_on": (condition.get("left_column") or {}).get("column_name"),
                "right_on": (condition.get("right_column") or {}).get("column_name"),
                "select": [c["column_name"] for c in op.get("select_columns") or []],
            })

        elif kind == "cast":
            spec.setdefault("cast", []).append({
                "column": (op.get("column") or {}).get("column_name"),
                "data_type": op.get("data_type"),
            })

        elif kind == "filter_group":
            rules, reason = _decompile_filters(op)
            if reason:
                return None, reason
            if aggregated:
                spec["having"] = rules
            elif op.get("logical_operator") == "Or":
                spec["where_any"] = rules
            else:
                spec["where"] = rules

        elif kind == "mutate":
            spec.setdefault("derive", []).append({
                "name": op.get("new_name"),
                "expression": (op.get("expression") or {}).get("expression"),
                "data_type": op.get("data_type", "Auto"),
            })

        elif kind == "rename":
            spec.setdefault("rename", []).append({
                "column": (op.get("column") or {}).get("column_name"),
                "as": op.get("new_name"),
            })

        elif kind in ("summarize", "pivot_wider"):
            aggregated = True
            dimensions = op["dimensions"] if kind == "summarize" else op.get("rows") or []
            measures = op["measures"] if kind == "summarize" else op.get("values") or []
            spec["group_by"] = [_group_by_ref(d) for d in dimensions]
            spec["aggregate"] = [_aggregate_ref(m) for m in measures]
            if any(a is None for a in spec["aggregate"]):
                return None, "contains an expression measure -- QuerySpec has no expression form"
            if kind == "pivot_wider":
                columns = op.get("columns") or []
                if len(columns) != 1:
                    return None, "pivots on more than one column"
                # `_pivot` lifts the pivot column OUT of the dimensions to form `columns`,
                # so it has to go back into `group_by` or the recompiled spec pivots on a
                # column it never grouped by. It lands last; the original position is not
                # recoverable and does not affect the compiled output.
                spec["group_by"] += [_group_by_ref(c) for c in columns]
                spec["pivot_on"] = {
                    "column": columns[0].get("dimension_name") or columns[0].get("column_name"),
                    "max_values": op.get("max_column_values", 10),
                }

        elif kind == "order_by":
            spec.setdefault("sort", []).append({
                "column": (op.get("column") or {}).get("column_name"),
                "desc": op.get("direction") == "desc",
            })

        elif kind == "limit":
            spec["limit"] = op.get("limit")

        elif kind == "select":
            spec["select"] = op.get("column_names") or []

    _drop_auto_casts(spec)
    return spec, None


def _decompile_filters(op: dict) -> tuple[list | None, str | None]:
    rules = []
    for rule in op.get("filters") or []:
        if rule.get("type") == "filter_group":
            return None, "contains a nested filter group -- use raw_operations"
        if "expression" in rule:
            return None, "contains an expression filter -- use raw_operations"
        column = (rule.get("column") or {}).get("column_name")
        if not column:
            return None, "contains a column-to-column filter -- use raw_operations"
        out = {"column": column, "op": rule.get("operator")}
        if rule.get("value") is not None:
            out["value"] = rule["value"]
        rules.append(out)
    return rules, None


def _group_by_ref(dimension: dict) -> dict:
    ref = {"column": dimension.get("column_name")}
    if dimension.get("granularity"):
        ref["granularity"] = dimension["granularity"]
    if dimension.get("dimension_name") and dimension["dimension_name"] != dimension.get("column_name"):
        ref["as"] = dimension["dimension_name"]
    return ref


def _aggregate_ref(measure: dict) -> dict | None:
    if "expression" in measure:
        return None

    fn = measure.get("aggregation")
    if measure.get("column_name") == "count" and fn == "count":
        ref = {"fn": "count"}
        if measure.get("measure_name") != "count_of_rows":
            ref["as"] = measure["measure_name"]
        return ref

    ref = {"column": measure.get("column_name"), "fn": fn}
    if measure.get("measure_name") != f"{fn}_of_{measure.get('column_name')}":
        ref["as"] = measure.get("measure_name")
    return ref


def _drop_auto_casts(spec: dict) -> None:
    """Remove the casts the compiler would re-emit itself.

    `_auto_cast` emits `{cast -> Datetime}` for a String column that a `group_by` gives a
    granularity to (§6.3 branch 1). Echoing it back into `spec.cast` would make the next
    compile emit it twice.
    """
    if not spec.get("cast"):
        return

    granular = {
        g["column"] for g in spec.get("group_by") or [] if g.get("granularity")
    }
    spec["cast"] = [
        c for c in spec["cast"]
        if not (c["data_type"] == "Datetime" and c["column"] in granular)
    ]
    if not spec["cast"]:
        del spec["cast"]
