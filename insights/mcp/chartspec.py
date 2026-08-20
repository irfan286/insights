# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""`ChartSpec` -> a chart's stored `config`, and back again.

Why a spec at all. The stored config is a different shape per chart type, Bubble's keys
are camelCase while every other chart's are snake_case (`chart.types.ts:141-151`), and a
measure carries four interdependent keys (`measure_name`, `column_name`, `aggregation`,
`data_type`) of which the backend silently mis-reads three if you get one wrong. None of
that is something to ask a model for. `ChartSpec` is one flat object in QuerySpec's
vocabulary; this module owns the translation, exactly as `compiler.py` owns
QuerySpec -> operations.

Columns resolve against a `SymbolTable`, never against a saved document (the §6
cross-phase contract). That is what lets a chart be specified against a query's output
before anything has been persisted, and it is what gives the model
`SymbolTable.require`'s "that column existed before the summarize but not after it"
diagnostic for free.
"""

from insights.insights.doctype.insights_chart_v3.chart_operations import (
    AXIS_CHARTS,
    CHARTS,
    count_measure,
    normalize_config,
    validate_config,
)

# Private by name, shared by intent: these are the same functions `compiler._dimension`
# and `compiler._measure` use. Restating the type-mapping table here would let chart
# measures and query measures disagree about what `sum(Integer)` produces, and nothing
# would catch it.
from insights.mcp.compiler import (
    NUMERIC_TYPES,
    TEMPORAL_TYPES,
    TIME_GRANULARITIES,
    _dimension_data_type,
    _measure_data_type,
    _reject_enum,
    filter_rule,
)
from insights.mcp.errors import ToolError
from insights.mcp.schemas import AGGREGATIONS, GRANULARITIES

COMMON_KEYS = {"chart_type", "filters", "order_by", "limit", "options"}

# Which spec keys each chart type reads. Anything else is rejected by name rather than
# ignored -- a silently dropped key is a chart that renders something the caller did not
# ask for.
TYPE_KEYS = {
    "Bar": {"x", "y", "split_by"},
    "Line": {"x", "y", "split_by"},
    "Row": {"x", "y", "split_by"},
    "Number": {"x", "y"},
    "Donut": {"x", "y"},
    "Funnel": {"x", "y"},
    "Table": {"rows", "columns", "values"},
    "Map": {"x", "y"},
    "Bubble": {"x_measure", "y_measure", "size", "group", "quadrant"},
    "Sankey": {"x", "target", "y"},
}

# Config keys this module sets structurally. `options` may not overwrite one of these --
# that is how a cosmetic passthrough stays cosmetic.
STRUCTURAL_CONFIG_KEYS = {
    "x_axis", "y_axis", "split_by", "number_columns", "date_column",
    "label_column", "value_column", "rows", "columns", "values",
    "location_column", "xAxis", "yAxis", "size_column", "dimension",
    "quadrant_column", "source_column", "target_column",
    "filters", "order_by", "limit",
}

PRE_AGGREGATED_PREFIXES = tuple(f"{fn}_" for fn in AGGREGATIONS)


# --------------------------------------------------------------------------- #
# building blocks
# --------------------------------------------------------------------------- #


def _dimension(ref: dict, symbols, path: str) -> dict:
    """A `Dimension` in the shape `makeDimension` writes (`query/helpers.ts:224-231`)."""
    if not isinstance(ref, dict):
        raise ToolError(f"`{path}` must be an object with a `column`.", spec_path=path)

    col = symbols.require(ref.get("column"), spec_path=f"{path}.column")
    data_type = _dimension_data_type(col.data_type)

    dimension = {
        "column_name": col.name,
        "data_type": data_type,
        "dimension_name": ref.get("as") or col.name,
    }

    granularity = ref.get("granularity")
    if granularity:
        _reject_enum(
            granularity, GRANULARITIES, spec_path=f"{path}.granularity", label="granularity"
        )
        if data_type not in TEMPORAL_TYPES:
            raise ToolError(
                f"Column '{col.name}' is {data_type}, so a granularity does not apply to it.",
                spec_path=f"{path}.granularity",
                fix=(
                    "Drop `granularity`. The backend ignores it on non-date columns, so "
                    "leaving it in would produce a wrong answer that looks right."
                ),
            )
        if data_type == "Time" and granularity not in TIME_GRANULARITIES:
            raise ToolError(
                f"A Time column only supports {', '.join(TIME_GRANULARITIES)}.",
                spec_path=f"{path}.granularity",
                valid_columns=list(TIME_GRANULARITIES),
            )
        dimension["granularity"] = granularity
    elif data_type in ("Date", "Datetime"):
        dimension["granularity"] = "month"
    elif data_type == "Time":
        dimension["granularity"] = "hour"

    return dimension


def _is_pre_aggregated(col) -> bool:
    """The half of `MeasurePicker.isPreAggregatedMeasure` we can actually evaluate.

    The backend never returns `is_measure` -- `get_columns_from_schema` emits only
    `{name, type}` (`ibis_utils.py:1027-1034`), and the UI computes the flag client-side.
    So on a live schema this is the name-prefix test alone; on a rehydrated symbol table
    `role` makes it exact. Used only to SUGGEST `fn: "none"` in an error, never to change
    a caller's `fn` silently.
    """
    return col.role == "measure" or col.name.startswith(PRE_AGGREGATED_PREFIXES)


def _measure(ref: dict, symbols, path: str, notes: list) -> dict:
    """A `Measure` in the shape `makeMeasure` writes (`query/helpers.ts:210-217`)."""
    if not isinstance(ref, dict):
        raise ToolError(f"`{path}` must be an object with an `fn`.", spec_path=path)

    fn = _reject_enum(
        ref.get("fn"), [*AGGREGATIONS, "none"], spec_path=f"{path}.fn", label="aggregation"
    )
    column = ref.get("column")

    if fn == "count" and not column:
        # The count-star sentinel. `translate_measure` (`ibis_utils.py:810-814`) matches on
        # BOTH column_name == "count" AND aggregation == "count"; get either wrong and it
        # counts a column named "count" instead, successfully.
        measure = count_measure()
        if ref.get("as"):
            measure["measure_name"] = ref["as"]
        return measure

    col = symbols.require(column, spec_path=f"{path}.column")

    if fn == "none":
        # There is no "no aggregation" on the wire: apply_aggregate is an if-chain that
        # frappe.throws on anything outside the six functions (`ibis_utils.py:841-855`).
        # MeasurePicker's rule for an already-aggregated column is to keep the column's own
        # name and fill the function with `sum` (`MeasurePicker.vue:100-123`), so that is
        # what "none" compiles to -- and the caller is told, because summing a column that
        # is NOT already one row per group silently adds values together.
        aggregation = "sum"
        measure_name = ref.get("as") or col.name
        notes.append(
            f"`fn: none` on `{col.name}`: the chart still emits a GROUP BY and sums the "
            "column. That is the identity only if the bound query already produces one "
            "row per group. If it does not, name a real aggregation instead."
        )
    else:
        aggregation = fn
        measure_name = ref.get("as") or f"{fn}_of_{col.name}"

        if fn in ("sum", "avg") and col.data_type not in NUMERIC_TYPES:
            hint = (
                f" `{col.name}` looks pre-aggregated -- try fn: \"none\"."
                if _is_pre_aggregated(col)
                else ""
            )
            raise ToolError(
                f"Cannot {fn} '{col.name}': it is {col.data_type}, not a number.",
                spec_path=f"{path}.fn",
                fix=f"Use count, count_distinct, min or max on a non-numeric column.{hint}",
            )

    return {
        "aggregation": aggregation,
        "column_name": col.name,
        "measure_name": measure_name,
        "data_type": _measure_data_type(col.data_type, aggregation),
    }


def _one_measure(spec: dict, key: str, symbols, notes: list, *, label: str) -> dict:
    values = spec.get(key) or []
    if not values:
        raise ToolError(
            f"`{key}` is required for this chart type ({label}).",
            spec_path=f"spec.{key}",
        )
    if len(values) > 1:
        raise ToolError(
            f"This chart type takes exactly one measure ({label}); {len(values)} were given.",
            spec_path=f"spec.{key}",
        )
    return _measure(values[0], symbols, f"spec.{key}[0]", notes)


def _require_dimension(spec: dict, key: str, symbols, *, label: str) -> dict:
    if not spec.get(key):
        raise ToolError(
            f"`{key}` is required for this chart type ({label}).", spec_path=f"spec.{key}"
        )
    return _dimension(spec[key], symbols, f"spec.{key}")


# --------------------------------------------------------------------------- #
# ChartSpec -> config
# --------------------------------------------------------------------------- #


def _reject_foreign_keys(chart_type: str, spec: dict) -> None:
    allowed = COMMON_KEYS | TYPE_KEYS[chart_type]
    for key in spec:
        if key in allowed:
            continue
        owners = sorted(t for t, keys in TYPE_KEYS.items() if key in keys)
        raise ToolError(
            f"`{key}` does not apply to a {chart_type} chart.",
            spec_path=f"spec.{key}",
            valid_columns=sorted(TYPE_KEYS[chart_type]),
            fix=(
                f"`{key}` belongs to {', '.join(owners)}."
                if owners
                else f"A {chart_type} chart uses: {', '.join(sorted(TYPE_KEYS[chart_type]))}."
            ),
        )


def _axis_config(spec, symbols, notes):
    measures = [
        _measure(m, symbols, f"spec.y[{i}]", notes) for i, m in enumerate(spec.get("y") or [])
    ]
    config = {
        "x_axis": {"dimension": _require_dimension(spec, "x", symbols, label="the x-axis")},
        # An empty series is a count, not an error (`chart.ts:249-250`).
        "y_axis": {"series": [{"measure": m} for m in (measures or [count_measure()])]},
    }
    if spec.get("split_by"):
        split = spec["split_by"]
        config["split_by"] = {
            "dimension": _dimension(split, symbols, "spec.split_by"),
            "max_split_values": split.get("max_values") or 10,
        }
    return config


def _number_config(spec, symbols, notes):
    measures = [
        _measure(m, symbols, f"spec.y[{i}]", notes) for i, m in enumerate(spec.get("y") or [])
    ]
    if not measures:
        raise ToolError("A Number chart needs at least one measure in `y`.", spec_path="spec.y")
    config = {"number_columns": measures}
    if spec.get("x"):
        config["date_column"] = _dimension(spec["x"], symbols, "spec.x")
    return config


def _label_value_config(spec, symbols, notes, *, label_key, value_key, label):
    return {
        label_key: _require_dimension(spec, "x", symbols, label=label),
        value_key: _one_measure(spec, "y", symbols, notes, label=label),
    }


def _table_config(spec, symbols, notes):
    rows = [
        _dimension(d, symbols, f"spec.rows[{i}]") for i, d in enumerate(spec.get("rows") or [])
    ]
    if not rows:
        raise ToolError("A Table chart needs at least one dimension in `rows`.", spec_path="spec.rows")
    return {
        "rows": rows,
        "columns": [
            _dimension(d, symbols, f"spec.columns[{i}]")
            for i, d in enumerate(spec.get("columns") or [])
        ],
        "values": [
            _measure(m, symbols, f"spec.values[{i}]", notes)
            for i, m in enumerate(spec.get("values") or [])
        ],
    }


def _bubble_config(spec, symbols, notes):
    config = {
        "xAxis": _measure(
            spec.get("x_measure") or _missing("x_measure"), symbols, "spec.x_measure", notes
        ),
        "yAxis": _measure(
            spec.get("y_measure") or _missing("y_measure"), symbols, "spec.y_measure", notes
        ),
    }
    if spec.get("size"):
        config["size_column"] = _measure(spec["size"], symbols, "spec.size", notes)
    if spec.get("group"):
        config["dimension"] = _dimension(spec["group"], symbols, "spec.group")
    if spec.get("quadrant"):
        config["quadrant_column"] = _dimension(spec["quadrant"], symbols, "spec.quadrant")
    return config


def _sankey_config(spec, symbols, notes):
    return {
        "source_column": _require_dimension(spec, "x", symbols, label="the source node"),
        "target_column": _require_dimension(spec, "target", symbols, label="the target node"),
        "value_column": _one_measure(spec, "y", symbols, notes, label="the flow value"),
    }


def _missing(key: str):
    raise ToolError(f"`{key}` is required for a Bubble chart.", spec_path=f"spec.{key}")


CONFIG_BUILDERS = {
    "Bar": _axis_config,
    "Line": _axis_config,
    "Row": _axis_config,
    "Number": _number_config,
    "Donut": lambda s, sym, n: _label_value_config(
        s, sym, n, label_key="label_column", value_key="value_column", label="the slice label"
    ),
    "Funnel": lambda s, sym, n: _label_value_config(
        s, sym, n, label_key="label_column", value_key="value_column", label="the stage label"
    ),
    "Table": _table_config,
    "Map": lambda s, sym, n: _label_value_config(
        s, sym, n, label_key="location_column", value_key="value_column", label="the location"
    ),
    "Bubble": _bubble_config,
    "Sankey": _sankey_config,
}


def resolve(spec: dict, symbols) -> tuple[str, dict, list[str]]:
    """`ChartSpec` -> `(chart_type, config, notes)`.

    The config comes back already normalized, i.e. carrying every default the UI's
    `transformChartDoc` would inject on load. A config without them is silently rewritten
    the first time a human opens the chart, and an MCP-written chart would never compare
    equal to a UI-written one.
    """
    if not isinstance(spec, dict):
        raise ToolError("`spec` must be a ChartSpec object.", spec_path="spec")

    chart_type = _reject_enum(
        spec.get("chart_type"), list(CHARTS), spec_path="spec.chart_type", label="chart type"
    )
    _reject_foreign_keys(chart_type, spec)

    notes = []
    config = CONFIG_BUILDERS[chart_type](spec, symbols, notes)

    if spec.get("filters"):
        config["filters"] = {
            "logical_operator": "And",
            "filters": [
                filter_rule(f, symbols, f"spec.filters[{i}]")
                for i, f in enumerate(spec["filters"])
            ],
        }

    if spec.get("order_by"):
        config["order_by"] = [
            {
                "column": {"type": "column", "column_name": _sort_column(s, config, f"spec.order_by[{i}]")},
                "direction": "desc" if s.get("desc") else "asc",
            }
            for i, s in enumerate(spec["order_by"])
        ]

    if spec.get("limit"):
        config["limit"] = spec["limit"]

    for key, value in (spec.get("options") or {}).items():
        if key in STRUCTURAL_CONFIG_KEYS:
            notes.append(f"Ignored `options.{key}` -- that key is set from the spec itself.")
            continue
        if key == "stacked":
            if chart_type == "Bar" and value:
                config["y_axis"]["stack"] = True
            continue
        config[key] = value

    for dimension in _all_dimensions(config):
        if dimension["data_type"] in NUMERIC_TYPES:
            # The UI's DimensionPicker only offers text/date/boolean columns
            # (`FIELDTYPES.DIMENSION`, `constants.ts:11-20`), so a numeric dimension is
            # valid SQL the renderer was never designed around -- an axis chart draws it
            # as a second series. Grouping by a numeric year or month is a real thing to
            # want, so this is a warning rather than a refusal.
            notes.append(
                f"`{dimension['column_name']}` is {dimension['data_type']}, which the UI "
                "does not offer as a dimension. It will group correctly, but an axis "
                "chart also plots it as a series. Cast it to String in the bound query "
                "if that looks wrong."
            )

    if chart_type == "Sankey":
        notes.append(
            "Sankey adds no aggregation of its own: the bound query must already emit one "
            "row per source/target pair."
        )

    config = normalize_config(chart_type, config)

    errors = validate_config(chart_type, config, query="bound-later")
    if errors:
        raise ToolError(
            "The resulting chart configuration is incomplete: " + "; ".join(errors),
            spec_path="spec",
        )

    return chart_type, config, notes


def _sort_column(sort: dict, config: dict, path: str) -> str:
    """Chart sorts name an OUTPUT column, which the symbol table does not know about."""
    name = sort.get("column")
    if not name:
        raise ToolError("A sort needs a `column`.", spec_path=f"{path}.column")

    outputs = _output_names(config)
    if outputs and name not in outputs:
        raise ToolError(
            f"'{name}' is not one of this chart's output columns.",
            spec_path=f"{path}.column",
            valid_columns=sorted(outputs),
            fix="Sort by a dimension label or a measure name, not by a source column.",
        )
    return name


def _output_names(config: dict) -> set:
    names = set()
    for key in ("date_column", "label_column", "location_column", "source_column",
                "target_column", "dimension", "quadrant_column"):
        if isinstance(config.get(key), dict) and config[key].get("dimension_name"):
            names.add(config[key]["dimension_name"])
    if isinstance(config.get("x_axis"), dict):
        dimension = config["x_axis"].get("dimension") or {}
        if dimension.get("dimension_name"):
            names.add(dimension["dimension_name"])
    for key in ("rows", "columns"):
        for dimension in config.get(key) or []:
            names.add(dimension.get("dimension_name") or dimension.get("column_name"))
    for key in ("value_column", "xAxis", "yAxis", "size_column"):
        if isinstance(config.get(key), dict) and config[key].get("measure_name"):
            names.add(config[key]["measure_name"])
    for measure in config.get("number_columns") or []:
        names.add(measure.get("measure_name"))
    for measure in config.get("values") or []:
        names.add(measure.get("measure_name"))
    for series in (config.get("y_axis") or {}).get("series") or []:
        measure = series.get("measure") or {}
        if measure.get("measure_name"):
            names.add(measure["measure_name"])
    return {n for n in names if n}


# --------------------------------------------------------------------------- #
# config -> ChartSpec
# --------------------------------------------------------------------------- #


def _dimension_ref(dimension: dict) -> dict:
    ref = {"column": dimension["column_name"]}
    if dimension.get("granularity"):
        ref["granularity"] = dimension["granularity"]
    if dimension.get("dimension_name") and dimension["dimension_name"] != dimension["column_name"]:
        ref["as"] = dimension["dimension_name"]
    return ref


def _measure_ref(measure: dict) -> dict:
    if measure.get("column_name") == "count" and measure.get("aggregation") == "count":
        ref = {"fn": "count"}
        if measure.get("measure_name") != "count_of_rows":
            ref["as"] = measure["measure_name"]
        return ref

    fn = measure.get("aggregation")
    ref = {"column": measure["column_name"], "fn": fn}
    default_name = f"{fn}_of_{measure['column_name']}"
    if measure.get("measure_name") == measure.get("column_name") and fn == "sum":
        # What `fn: "none"` compiles to. Round-tripping it back to "sum" would be lossy in
        # the direction that matters: the model would resubmit it and get a renamed column.
        ref["fn"] = "none"
    elif measure.get("measure_name") != default_name:
        ref["as"] = measure["measure_name"]
    return ref


COSMETIC_ONLY = {"read_only", "column_widths", "text_wrap", "sticky_columns"}

# Injected by `normalize_config` for these chart types, not chosen by the caller. Echoing
# them back into `options` would make every decompiled spec carry noise the caller never
# wrote -- and the model would then keep resubmitting it.
NORMALIZED_DEFAULTS = {
    "Donut": {"legend_position": "bottom"},
    "Funnel": {"label_position": "left"},
}


def decompile(chart_type: str, config) -> tuple[dict | None, str | None]:
    """Best effort, and honest when it fails.

    A lossy approximation is worse than `None` here: the model edits what it is given and
    writes it back, so an approximation becomes a wrong chart rather than a wrong reading.
    """
    from frappe import parse_json

    config = parse_json(config) if isinstance(config, str) else (config or {})

    if chart_type not in CHARTS:
        return None, f"unknown chart type '{chart_type}'"

    for measure in _all_measures(config):
        if "expression" in measure:
            return None, "config contains an expression measure -- ChartSpec has no expression form"
        if not measure.get("aggregation"):
            return None, "config contains a measure with no aggregation"

    spec = {"chart_type": chart_type}
    try:
        if chart_type in AXIS_CHARTS:
            spec["x"] = _dimension_ref(config["x_axis"]["dimension"])
            spec["y"] = [
                _measure_ref(s["measure"]) for s in (config.get("y_axis") or {}).get("series") or []
            ]
            if config.get("split_by", {}).get("dimension"):
                split = _dimension_ref(config["split_by"]["dimension"])
                split["max_values"] = config["split_by"].get("max_split_values") or 10
                spec["split_by"] = split
        elif chart_type == "Number":
            spec["y"] = [_measure_ref(m) for m in config.get("number_columns") or []]
            if config.get("date_column", {}).get("column_name"):
                spec["x"] = _dimension_ref(config["date_column"])
        elif chart_type in ("Donut", "Funnel"):
            spec["x"] = _dimension_ref(config["label_column"])
            spec["y"] = [_measure_ref(config["value_column"])]
        elif chart_type == "Map":
            spec["x"] = _dimension_ref(config["location_column"])
            spec["y"] = [_measure_ref(config["value_column"])]
        elif chart_type == "Table":
            spec["rows"] = [_dimension_ref(d) for d in config.get("rows") or []]
            if config.get("columns"):
                spec["columns"] = [_dimension_ref(d) for d in config["columns"]]
            if config.get("values"):
                spec["values"] = [_measure_ref(m) for m in config["values"]]
        elif chart_type == "Bubble":
            spec["x_measure"] = _measure_ref(config["xAxis"])
            spec["y_measure"] = _measure_ref(config["yAxis"])
            for spec_key, config_key in (
                ("size", "size_column"),
                ("group", "dimension"),
                ("quadrant", "quadrant_column"),
            ):
                if config.get(config_key):
                    builder = _measure_ref if spec_key == "size" else _dimension_ref
                    spec[spec_key] = builder(config[config_key])
        elif chart_type == "Sankey":
            spec["x"] = _dimension_ref(config["source_column"])
            spec["target"] = _dimension_ref(config["target_column"])
            spec["y"] = [_measure_ref(config["value_column"])]
    except (KeyError, TypeError) as exc:
        return None, f"config is missing {exc} -- it was not written through create_chart"

    if config.get("filters", {}).get("filters"):
        rules = config["filters"]["filters"]
        if any("expression" in r or r.get("type") == "filter_group" for r in rules):
            return None, "config contains a nested or expression filter -- use the raw config"
        spec["filters"] = [
            {
                "column": r["column"]["column_name"],
                "op": r["operator"],
                **({"value": r["value"]} if r.get("value") is not None else {}),
            }
            for r in rules
        ]

    if config.get("order_by"):
        spec["order_by"] = [
            {"column": s["column"]["column_name"], "desc": s["direction"] == "desc"}
            for s in config["order_by"]
        ]

    if config.get("limit") and config["limit"] != 100:
        spec["limit"] = config["limit"]

    defaults = NORMALIZED_DEFAULTS.get(chart_type, {})
    options = {
        k: v
        for k, v in config.items()
        if k not in STRUCTURAL_CONFIG_KEYS
        and k not in COSMETIC_ONLY
        and defaults.get(k) != v
    }
    if (config.get("y_axis") or {}).get("stack"):
        options["stacked"] = True
    if options:
        spec["options"] = options

    return spec, None


def _all_dimensions(config: dict) -> list:
    dimensions = []
    for key in ("date_column", "label_column", "location_column", "source_column",
                "target_column", "dimension", "quadrant_column"):
        if isinstance(config.get(key), dict) and config[key].get("column_name"):
            dimensions.append(config[key])
    for key in ("x_axis", "split_by"):
        dimension = (config.get(key) or {}).get("dimension")
        if isinstance(dimension, dict) and dimension.get("column_name"):
            dimensions.append(dimension)
    for key in ("rows", "columns"):
        dimensions.extend(d for d in config.get(key) or [] if d.get("column_name"))
    return dimensions


def _all_measures(config: dict) -> list:
    measures = []
    for key in ("value_column", "xAxis", "yAxis", "size_column"):
        if isinstance(config.get(key), dict):
            measures.append(config[key])
    measures.extend(config.get("number_columns") or [])
    measures.extend(config.get("values") or [])
    for series in (config.get("y_axis") or {}).get("series") or []:
        if isinstance(series.get("measure"), dict):
            measures.append(series["measure"])
    return measures
