# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""Origin validation for the MCP endpoint.

`frappe_mcp` performs no HTTP header inspection of any kind -- verified by grep over
every module in the package at 11d5076. The MCP Streamable-HTTP spec says a server
MUST validate `Origin`, so this control is ours to build and is not optional.

Frappe's CORS layer does not substitute. `frappe/app.py` `set_cors_headers` is a
*response* mechanism: when an origin is not allowed it simply omits the CORS headers
and lets the request proceed. DNS rebinding does not need CORS at all -- the attacker
rebinds a hostname it controls to the target IP and the browser believes same-origin.
"""

import re
from urllib.parse import urlsplit

import frappe

SETTINGS_FIELD = "mcp_allowed_origins"

# A hostname is labels of [A-Za-z0-9-] joined by dots, or an IPv6 literal in brackets.
# urlsplit happily returns "not a url" as a hostname, so an allowlist typo would
# otherwise be stored as a plausible-looking entry.
# urlsplit.hostname strips the brackets from an IPv6 literal, so "[::1]" arrives as "::1".
_HOSTNAME = re.compile(
    r"^(?:"
    r"[0-9a-f:]*:[0-9a-f:.]*"                                        # IPv6 literal, de-bracketed
    r"|[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*"  # dotted labels
    r")$"
)


def origin_allowed(origin: str | None) -> bool:
    """Return True when `origin` may drive the MCP endpoint.

    Absent Origin -> allowed. Non-browser callers (curl, mcp-remote, the Claude Code
    HTTP transport, server-to-server) send none, and a request with no Origin cannot
    be a DNS-rebinding attack, which is a browser attack. Refusing header-less
    requests would break every real client and buy nothing.

    Present Origin -> must equal the site's own origin, or appear in
    `Insights Settings.mcp_allowed_origins`.
    """
    if not origin:
        return True

    candidate = _normalize(origin)
    if not candidate:
        return False

    return candidate in _allowed_origins()


def _allowed_origins() -> set[str]:
    allowed = set()

    # allow_header_override=False is load-bearing, not a style choice.
    #
    # frappe.utils.get_url() defaults to allow_header_override=True and, when
    # `host_name` is absent from site config, derives the origin from the REQUEST's
    # own Host header (frappe/utils/data.py:1845-1849). Trusting that here would mean
    # "the site's own origin" is whatever the caller says it is -- so a DNS-rebinding
    # attacker sending `Host: evil.example` + `Origin: http://evil.example` would match
    # itself and walk straight through the one control that exists to stop it.
    #
    # With override off we get conf.host_name when the operator has declared one, and
    # otherwise `http://<site>`. On a bench with no host_name that will NOT match a
    # browser at http://localhost:8001, which is correct and fail-closed: list the
    # origin in mcp_allowed_origins, or set host_name. Non-browser clients (Claude
    # Code, mcp-remote, server-side connectors) send no Origin and are unaffected.
    site_origin = _normalize(frappe.utils.get_url(allow_header_override=False))
    if site_origin:
        allowed.add(site_origin)

    for entry in _split(_configured_origins()):
        normalized = _normalize(entry)
        if normalized:
            allowed.add(normalized)

    return allowed


def _configured_origins() -> str:
    """Read the allowlist, tolerating a bench that has not migrated yet."""
    try:
        return frappe.get_cached_value("Insights Settings", "Insights Settings", SETTINGS_FIELD) or ""
    except Exception:
        # Field absent before `bench migrate`. Fail closed to the site's own origin
        # rather than fail open.
        return ""


def _split(value: str) -> list[str]:
    """Accept newline- or comma-separated entries.

    The neighbouring `allowed_origins` field (CSP frame-ancestors) is comma-separated,
    so an operator copy-pasting between the two must not silently get an allowlist of
    one long unparseable string.
    """
    return [part.strip() for part in re.split(r"[,\n\r]+", value or "") if part.strip()]


def _normalize(url: str) -> str | None:
    """Reduce to `scheme://host[:port]`, lowercased.

    Comparison is on the whole normalized triple, never `startswith` -- an allowlist
    entry of `https://good.example` must not admit `https://good.example.evil.com`,
    and a scheme or port difference must be a mismatch.
    """
    if not url:
        return None

    raw = url.strip()
    if "://" not in raw:
        raw = f"https://{raw}"

    try:
        parts = urlsplit(raw)
    except ValueError:
        return None

    if not parts.scheme or not parts.hostname:
        return None

    try:
        port = parts.port
    except ValueError:
        # Malformed port, e.g. "https://host:notaport"
        return None

    host = parts.hostname.lower()
    if not _HOSTNAME.match(host):
        return None

    origin = f"{parts.scheme.lower()}://{host}"
    if port:
        origin = f"{origin}:{port}"

    return origin
