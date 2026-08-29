# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""A tripwire on the TypeScript the Python port copies.

`chart_operations.py` is a hand port of a dozen functions in
`frontend/src2/charts/chart.ts` and `frontend/src2/charts/helpers.ts`. Nothing else in the
suite notices when the TypeScript moves: the Python tests keep passing against the old
behaviour, and MCP-created charts quietly stop matching UI-created ones. Design §11 lists
that drift as an accepted risk. This is the mitigation.

It is a fingerprint, not a parity proof. When it fails, the TypeScript changed -- read the
diff, decide whether `chart_operations.py` needs the same change, then update the hash.
A cosmetic edit is a one-line hash bump; a behavioural one is a port.

Comments and whitespace are normalised out, so reformatting and re-commenting do not
trip it. Phase 3 retires this file by pointing `chart.ts:refresh()` at
`refresh_data_query`, leaving one implementation and nothing to drift.
"""

import hashlib
import re
from pathlib import Path

import frappe
from frappe.tests import UnitTestCase

FRONTEND = Path(frappe.get_app_path("insights")).parent / "frontend" / "src2" / "charts"

# function name -> sha256 of its normalised body
FINGERPRINTS = {
    "chart.ts": {
        "refresh": "6e778f47d8d9b37a",
        "validateConfig": "8ee88d3ad624f4f7",
        "addSourceOperation": "1c916bf2120afd97",
        "addFilterOperation": "3c1c429e2f2175cc",
        "addChartOperation": "b280f1489538a5fb",
        "addAxisChartOperation": "a9df612054ea04c5",
        "addNumberChartOperation": "87e7d0f7092cb9bd",
        "addDonutChartOperation": "66e3d53a7cf93dcf",
        "addTableChartOperation": "5a07b4fa59890687",
        "addMapChartOperation": "e14db6383104668e",
        "addBubbleChartOperation": "00dd92e6e16f2274",
        "addOrderByOperation": "0c8b85bee9d9571f",
        "transformChartDoc": "e6346e6826647265",
    },
    "helpers.ts": {
        "setDimensionNames": "36930311d5ed509d",
        "handleOldXAxisConfig": "aea501298cd49e07",
        "handleOldYAxisConfig": "591591232986a6f3",
    },
}


def extract(source: str, name: str) -> str:
    """The body of `function <name>(...)`, brace-matched, comments and strings respected."""
    match = re.search(rf"\bfunction\s+{re.escape(name)}\s*\(", source)
    if not match:
        raise LookupError(f"function {name} not found")

    index = source.index("{", match.end())
    depth, i, n = 0, index, len(source)
    quote = None
    while i < n:
        char = source[i]
        if quote:
            if char == "\\":
                i += 2
                continue
            if char == quote:
                quote = None
        elif char in "\"'`":
            quote = char
        elif source.startswith("//", i):
            i = source.find("\n", i)
            if i == -1:
                break
            continue
        elif source.startswith("/*", i):
            i = source.find("*/", i) + 2
            continue
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[index : i + 1]
        i += 1

    raise LookupError(f"unbalanced braces in {name}")


def normalise(body: str) -> str:
    body = re.sub(r"/\*.*?\*/", " ", body, flags=re.S)
    body = re.sub(r"(?<![:'\"])//[^\n]*", " ", body)
    return re.sub(r"\s+", " ", body).strip()


def fingerprint(body: str) -> str:
    return hashlib.sha256(body.encode()).hexdigest()[:16]


class TestChartTypeScriptFingerprints(UnitTestCase):
    def test_the_ported_functions_have_not_changed(self):
        for filename, functions in FINGERPRINTS.items():
            source = (FRONTEND / filename).read_text()
            for name, expected in functions.items():
                with self.subTest(function=f"{filename}:{name}"):
                    body = normalise(extract(source, name))
                    # Guard the guard: a brace matcher that silently returns "{}" would
                    # give every function a stable hash and catch nothing.
                    self.assertGreater(len(body), 50, f"{name} extracted as {body!r}")
                    actual = fingerprint(body)
                    self.assertEqual(
                        actual,
                        expected,
                        f"\n\n{filename}:{name} changed since chart_operations.py was "
                        f"written from it.\nRe-read it, decide whether the Python port "
                        f"needs the same change, then set the hash to '{actual}'.",
                    )
