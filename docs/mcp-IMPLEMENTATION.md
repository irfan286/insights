# MCP Server — Implementation Handoff

**Read this file completely at the start of every session. It is deliberately small.**

This is the working document for building the Insights MCP server across multiple Claude Code sessions.
The design lives in a separate file and is **182KB — never read it end to end.** Load only the sections
this file tells you to load for the phase you are on.

| File | What it is | How to read it |
|---|---|---|
| `docs/mcp-IMPLEMENTATION.md` | ← you are here. State, runbook, rules, checklists. | Fully, every session |
| `docs/mcp-server-design.md` | The design. Reference material, section-addressable. | Only the sections named in §4 below |

---

## 1. Current status

**Phase: 1 — feature-complete. All deliverables built and tested; demo gate passes over HTTP.**

Last updated: 2026-08-19 · Updated by: Phase 1 session 1

| Phase | State | Gate to enter |
|---|---|---|
| 0 — Blocking unknowns | ✅ complete | none |
| 1 — Walking skeleton | ✅ complete (see caveats) | Phase 0 complete |
| 2 — Charts & dashboards | ⬜ not started | Phase 1 demo passes |
| 3 — Shareable links | ⬜ not started | Phase 2 demo passes |

**What exists.** `insights/mcp/`: `__init__.py` (transport), `origin.py`, `validate.py`,
`errors.py`, `guards.py`, `schemas.py`, `compiler.py`, `render.py`, `docs.py`, and
`tools/{discovery,query,docs}.py` — **7 tools**: `list_data_sources`, `list_tables`,
`describe_table`, `distinct_values`, `get_docs`, `write_ai_note`, `run_query`.
Plus the `Insights Data Doc` doctype with its zone guard, `insights/api/docs.py`
(`promote_note`, human-only), the `flag_stale_docs` daily hook, and
`mcp_allowed_origins` on `Insights Settings`.

**104 MCP tests passing** across `test_transport` (16), `test_guards` (16),
`test_compiler` (39), `test_compiler_integration` (6), `test_schemas` (6),
`test_docs` (21). Existing suites unaffected: `test_permissions`, `test_ibis_utils`,
`test_warehouse`, `test_basic_workflow` all green.

**Demo gate: PASSES.** Verified end to end over HTTP with a real API key —
`get_docs` → `describe_table` → `distinct_values` → `run_query(dry_run)` → `run_query`,
returning real rows. Honest framing: no documents are created, but execution log rows
are, and each is greppable by its `mcp-` name prefix.

**Two caveats, neither in our code:**
1. **DuckDB segfaults the web worker** — see §8 Q. Use `Site DB` or `datalake` over
   HTTP; `demo_data` is fine for the in-process test suite.
2. When that segfault happens the **whole `bench start` group goes down**, Redis
   included — so an empty MCP response means restart the bench, not debug Redis.

**Owed:** the upstream PR against `frappe/mcp` issue #5 (fork is pushed and pinned),
the claude.ai Connectors protocol-version test (needs a tunnel + `host_name`, §8 E/H),
and a first real documentation upload for `datalake` (§8 A).

---

## 2. Environment

Verified 2026-08-19 on this machine.

```
bench root      /home/irfan/insights-bench
app             /home/irfan/insights-bench/apps/insights   (branch: version-3)
site            development
web port        8001
apps installed  frappe, insights
python tests    insights/tests/
```

```bash
# from the bench root
bench start                                    # all services
bench --site development migrate               # after any doctype/schema change
bench --site development console               # python REPL with frappe context
bench --site development run-tests --app insights --module insights.tests.mcp.test_transport
bench build --app insights                     # rebuild JS/CSS

# frontend (from apps/insights/frontend)
yarn dev                                       # vite dev server, proxies to bench
```

**MCP-specific:**

```bash
# frappe-mcp check --app insights --verbose   # DOES NOT WORK with our architecture -- see §8 J
npx @modelcontextprotocol/inspector            # manual protocol testing
# endpoint will be: http://localhost:8001/api/method/insights.mcp.handle_mcp
```

**Before touching anything:** `git status`. The working tree had only untracked `docs/` at plan time.
Branch is `version-3`, not `main` — do not open PRs against `main`.

---

## 3. Non-negotiables

These were decided after two adversarial reviews. Each cost real analysis. **Do not quietly
re-litigate them.** If you believe one is wrong, say so to the user and stop — do not just do it differently.

| # | Rule | Why | Detail |
|---|---|---|---|
| 1 | **Never `pip install frappe-mcp`.** Fork it, relax two pins, pin to a commit SHA. | PyPI 0.1.0 is a year stale and its `Werkzeug`/`pydantic` pins silently downgrade the WSGI layer Frappe serves every request through. Upstream issue #5, open. | §3.10 |
| 2 | **Write `input_schema=` explicitly on every tool.** Never rely on type-hint inference. | Upstream inference cannot express enums, defaults, or nested objects. `QuerySpec` would collapse to `{"type":"object"}`. | §4.0 |
| 3 | **Every transient `.execute()` goes through `guards.execute_transient`.** | Transient docs fire no permission hooks at all. This is the only gate. CI-grepped. | §8.3 |
| 4 | **Tools return `str`, never `dict`.** | A dict return makes upstream emit `structuredContent` *and* duplicate the same JSON into the text block — double token cost. | §3.8 |
| 5 | **Raise `ToolError`; never return an error-shaped result.** | Upstream sets `isError: true` only by catching a raised exception. Returning an error object produces a *successful* result the model will not recognise as a failure. | §3.7 |
| 6 | **Every write tool carries `@transactional`.** | Upstream swallows tool exceptions, so Frappe never auto-rolls-back a half-finished write. | §3.7 |
| 7 | **Origin validation is ours to build and is not optional.** | Upstream performs no header inspection of any kind. Frappe's CORS layer does not substitute — CORS omits headers rather than rejecting, and DNS rebinding is designed to defeat it. | §3.4 |
| 8 | **Do not build MCP resources.** Documentation reaches the model by piggy-backing on `describe_table` / `list_tables`. | No Claude client auto-loads resources into model context, and upstream does not implement them anyway. | §5.4 |
| 9 | **No MCP tool emits a `code` operation** until the `safe_exec` sandbox policy has been read. | Unreviewed sandbox. Open question 16. | §11 |
| 10 | **`delete_item` never deletes a workbook.** | Cascading force-delete. Queries, charts, dashboards, and AI notes only. | §4.2 |

---

## 4. Reading guide

Load **only** these sections of `docs/mcp-server-design.md`. Use `sed -n '/^## 3\./,/^## 4\./p'` or
grep for the heading — do not open the whole file.

| Phase | Read | Skip |
|---|---|---|
| **0** | §0.A (changelog), §3.10 (dependency risk), §11 (risks + open questions) | everything else |
| **1** | §3 (transport), §4 (tool catalog), §5 (documentation layer), §6 (QuerySpec + compiler), §8 (permissions), §10 Phase 1 | §7, §9 |
| **2** | §7 (charts + render port), §4.5 (tool specs), §5.7 (prompts), §10 Phase 2 | §9 |
| **3** | §9 (sharing + token gate), §10 Phase 3 | §6, §7 |

Section headings are stable — cite them as `§N.M` when you write notes back into this file.

---

## 5. Phase 0 — blocking unknowns

**Do this before writing implementation code.** Each item is minutes, not days, and each can
invalidate work downstream.

### 5.1 Needs Irfan, not code

These cannot be answered from source. Ask; do not assume.

- [x] **Is `Insights Settings.enable_permissions` on in the target deployment?** → **NO, `0`.** See §8 A. The entire table-access model flips on it. If off (the default), any authenticated MCP caller can read any table in any data source, including Site DB.
- [x] **Is the bench multi-tenant?** → **No**, single site. See §8 A. If yes, `X-Frappe-Site-Name` becomes mandatory on every request.
- [x] **Per-user OAuth, or a shared service account?** → **Per-user API keys.** See §8 A. Security/product decision. Affects §8.4.
- [x] **Who uploads the first data-source documentation, and for which source?** → **`datalake`.** See §8 A. The documentation layer is worthless without a first uploader. Pick a real source before building §5.

### 5.2 Day-one technical checks

- [ ] **Does claude.ai's Connectors flow accept `protocolVersion: "2025-03-26"`?** ← **STILL OPEN — cannot be tested from localhost, needs a tunnel + `host_name`. See §8 E and §8 H.** Hard-coded at upstream `server/handlers.py:9`, not negotiated. Claude Code and MCP Inspector accept it; the browser path is untested. **This is the highest-risk unknown in the whole plan** — it is exactly the "reachable from the web" goal. Fix if it bites: one-line rebind of `handle_initialize`, or handle it in the fork.
- [ ] **Does Frappe's OAuth actually complete end-to-end with claude.ai?** ← still open, same blocker. The code is present in Frappe 16 (RFC 9728 + 8414 + 7591 + PKCE, on by default). The handshake is unproven.
- [x] **`/.well-known/oauth-protected-resource` responds**, verified live on a real 401 —, and a `401` from the endpoint carries `WWW-Authenticate`.
- [x] **Is `jsonschema` resolvable in this bench?** → **It was NOT.** Now installed and declared. See §8 D. Upstream imports it at module scope. Moot if you take the fork path, fatal if you take `--no-deps`.
- [x] **Is join column order deterministic?** → **NO**, and there is a worse related bug. See §8 C. `select_columns` is stored as a list but `get_right_table` builds a Python **set** and calls `.select(set)` (`ibis_utils.py:228-338`). The compiler emits `select_columns` on every join. If order is non-deterministic, `run_query` results reorder between identical calls — confusing, not incorrect. Five minutes.

**Answers are recorded in §8 A.** Phase 0 is closed except the two claude.ai interop items above.

---

## 6. Cross-phase contracts

Phase 2 and 3 build on Phase 1 artifacts. Do not reinvent these; do not change their shape without
updating this section.

| Artifact | Owner | Consumed by | Contract |
|---|---|---|---|
| `mcp.compiler.compile(spec) -> (operations, symbol_table)` | P1 | P2 `ChartSpec` resolution | The symbol table is the authority on a query's output columns. `ChartSpec` resolves against it, **not** against a saved doc — this is what lets charts be built before queries are persisted. |
| `mcp.guards.execute_transient(doc, user)` | P1 | P2, P3 | Sole path for transient execution. Every new execute site routes through it or CI fails. |
| `mcp.errors.ToolError(message, spec_path=, valid_columns=)` | P1 | P2, P3 | Raised, never returned. Carries the fields the model needs to self-correct. |
| `mcp.validate.tool_args(schema)` | P1 | P2, P3 | Upstream never validates `tools/call` arguments (`run_tool` is dead code). Every tool wears this. |
| `mcp.docs.compose(data_source, table=None)` | P1 | P2 | Returns provenance-headed blocks. `describe_table` calls it — that piggy-back is the documentation delivery mechanism. |
| `chart_operations.build_data_query_operations(chart)` | P2 | P3 | **Must set `use_live_connection` from the source query**, not just write operations. Omitting it renders charts blank with no error. |

---

## 7. Checklists

Tick as you go. A tick means *tested*, not *typed*.

### Phase 1 — walking skeleton (≈1.5 weeks)

**Step 0 — dependency (½ day), before any other code**
- [x] Fork `frappe/mcp` → `Irfan234-afif/frappe-mcp`, branch `frappe-v16` — pushed
- [x] Relax `Werkzeug==3.1.3` → `>=3.1.3,<4` and `pydantic~=2.11.7` → `>=2.11.7,<3`  (commit `bef8fbf`, local)
- [x] Upstream's 67 tests pass against Frappe v16's pinned versions  — **67 passed**, reproduced in this bench's venv
- [x] Added to `insights/pyproject.toml` **at a commit SHA** — `bef8fbf…`, `pip check` clean
- [ ] PR opened against upstream issue #5

**Transport**
- [x] `insights/mcp/__init__.py` — `MCP("insights")` + `handle_mcp` wrapper (guest→401, role→403, Origin→403, non-POST→405+`Allow`, tool import, non-dict→`-32600`, protocol-version echo)
- [x] `insights/mcp/origin.py` — allowlist over **`mcp_allowed_origins`** (new field, not `allowed_origins` — §8 B)
- [x] `insights/mcp/validate.py` — `@tool_args(schema)`
- [x] `insights/mcp/guards.py` — `execute_transient` **and** `build_transient` (the no-rows sibling `dry_run`/`distinct_values` need); tables derived internally via `extract_table_deps_from_operations`, not trusted from the caller
- [x] `insights/mcp/errors.py` — `ToolError`, `@transactional`, "position N" → `spec_path` (via toast capture — see §8 K)
- [x] **A real client connects and completes `initialize`** — curl matrix, 2026-08-19 (§8 E)

**Query layer**
- [x] `insights/mcp/schemas.py` — `QuerySpec` (incl. `having` / `cast` / `rename`), validated as Draft 2020-12; `Filter` is a local `$defs` so each tool schema stays self-contained
- [x] `insights/mcp/compiler.py` — `compile(spec, resolver=) -> (operations, SymbolTable)`; staged symbol table, all four auto-cast branches, `SchemaResolver` injection (Live/Static)
- [x] `insights/mcp/tools/query.py` — `run_query` incl. `dry_run`, `raw_operations` whitelist, zero-row diagnosis

**Documentation layer**
- [x] `insights_data_doc` doctype + `validate()` zone guard — both layers tested
- [x] `insights/mcp/docs.py` — `compose`, `blocks_for`, `block`, `render_blocks`, `erd`, `fingerprint`, `flag_stale_docs`
- [x] `insights/mcp/tools/docs.py` — `get_docs`, `write_ai_note` (incl. `supersedes`, `propose_promotion`)
- [x] `insights/api/docs.py` — `promote_note` + `list_promotion_queue`; a test asserts no MCP tool matches /promote/
- [x] `hooks.py` — `insights.mcp.docs.flag_stale_docs` added to `daily`

**Discovery**
- [x] `insights/mcp/tools/discovery.py` — all four, incl. `distinct_values` table + query mode

**Tests**
- [x] `test_transport.py` — **16 tests, all passing.** notification→202, non-POST→405+`Allow`, five Origin cases + suffix-attack + spoofed-Host regression, unauth→401+`WWW-Authenticate`, role-missing→403, `initialize` round trip, protocol-version echo, array body→`-32600`, scalar body rejected, `tools/list` schema shape
- [x] `test_compiler.py` — **34 tests**, golden §6.4 example matched operation-for-operation, all four auto-cast branches, `having`/`sort` symbol-table validation, join-collision, filter and enum-casing rejection
- [x] `test_compiler_integration.py` — **6 tests** against real `demo_data`; asserts the symbol table equals the executed query's actual output columns
- [x] `test_guards.py` — **16 tests, all passing** (11 AST + 5 behavioural): rules 2, 3, 4, 6, 7, 8, 9 + no `ignore_permissions`, guest-check ordering, `@tool_args` coverage, and the `allow_header_override=False` regression guard

**Demo gate: ✅ PASSED 2026-08-19.** *"Who are our top 10 customers by invoice total this year?"* → Claude calls `get_docs`, `describe_table`, emits a `QuerySpec`, `run_query(dry_run)`, then `run_query`, and shows rows.
Honest framing: no documents are created, but execution logs **are** written and a warehouse import may be enqueued.

### Phase 2 — charts & dashboards (≈3 weeks)

- [ ] `chart_operations.py` — port of `chart.ts:66-92, 220-380` ← **the long pole**
- [ ] `use_live_connection` propagation (omitting it is the blank-chart bug)
- [ ] `insights/mcp/chartspec.py` — `ChartSpec` → config, post-`transformChartDoc` shape
- [ ] `insights_chart_v3.py` — add `refresh_data_query(force=False)`
- [ ] Tools: `save_query`, `list_workbooks`, `get_item`, `create_chart`, `update_chart`, `create_dashboard`, `update_dashboard`, `delete_item`
- [ ] `test_chart_operations.py` — golden tests over all 10 chart types asserting operations **and** `use_live_connection`
- [ ] `test_chartspec_roundtrip.py` — MCP-written config → `transformChartDoc` → no diff
- [ ] *(optional)* heading-based doc auto-slicing at upload; MCP prompts (§5.7)

**Demo gate:** *"Build a sales dashboard from the site DB and give me a link."*

### Phase 3 — shareable links (≈2 weeks)

- [ ] `insights_dashboard_v3.json` — `share_token`, `share_expires_on`, `share_revoked`
- [ ] `insights/api/shared.py` — `_shareable_dashboard_names()` routed through **all three** guest paths
- [ ] Patch — leave `share_token` NULL on existing public dashboards so current links keep working
- [ ] `resource.ts` interceptor — inject `X-Insights-Share-Token` from `?t=` (**not** `SharedDashboard.vue`)
- [ ] `dashboard.ts` — `getShareLink()` appends `?t=`
- [ ] `test_share_token.py` — the four acceptance tests in §9.3
- [ ] `insights/mcp/tools/share.py` — `expires_in_days`, `rotate_token`, `revoke`
- [ ] *(scoped)* point `chart.ts:refresh()` at `refresh_data_query` to remove TS/Python drift

**Demo gate:** a link that expires in 7 days and dies on one `share_dashboard(revoke: true)` — verified dead for the dashboard, its charts, **and** its queries.

---

## 8. Answers & decisions log

Append here as questions get settled. Include the date. This is how a future session learns what
this one found out.

### A. Phase 0 answers — all closed, 2026-08-19

Verified against bench `/home/irfan/insights-bench`, site `development`.

| Question | Answer |
|---|---|
| §5.1 `enable_permissions` on? | **No — `0`.** Table ACLs are OFF. |
| §5.1 multi-tenant? | **No.** One site, `serve_default_site: true`. `X-Frappe-Site-Name` not needed. |
| §5.1 per-user OAuth or service account? | **Per-user Frappe API keys.** Decided by Irfan. |
| §5.1 first documentation source? | **`datalake`.** Decided by Irfan. |
| §5.2 `jsonschema` resolvable? | **It was NOT installed.** Now installed *and* declared in `insights/pyproject.toml` — see D. |
| §5.2 join column order deterministic? | **No.** `ibis_utils.py:245` builds a `set`, `.select()`s it at `:267`. See C. |
| §5.2 OAuth stack present? | **Yes.** All three `OAuth Settings` flags = `1`. `WWW-Authenticate` confirmed live on a real 401. |
| Design OQ **#6** — `_build_table_permission_query` permissive-for-all? | **YES**, and *not* for the reason the design guessed. `permissions.py:147-150` returns **every** table row when `team_permissions_enabled` is falsy, and `:49-50` shows that property is just `enable_permissions`. Nothing to do with `Insights Resource Permission` being empty. |
| Design OQ **#7** — can `dry_run` avoid `enqueue_import`? | **Moot for Phase 1.** `get_ibis_table` (`insights_table_v3.py:136-155`) only takes the warehouse path when `use_live_connection` is falsy. We default it true, and **zero tables are warehouse-stored** on any source, so the default path never enqueues. Do NOT thread `import_if_not_exists` through. |
| Design OQ **#19** — `jsonschema` present? | **No.** Confirmed the `--no-deps` path fails at *import*, not at call. |
| Design OQ **#22** — does `get_json(force=True)` raise something upstream misses? | **Closed, but the premise was wrong.** See F. |

**Environment:** Python **3.14.6**, frappe **16.29.0**, Werkzeug **3.1.6**, pydantic **2.12.5**,
ibis **11.0.0**. Data sources: `datalake` (PostgreSQL, 846 tables, **0 links**), `demo_data`
(DuckDB, 8 tables, 8 links), `Site DB` (MariaDB, 285 tables, 438 links). **0 tables stored.**

> **Say this out loud in any security discussion.** With `enable_permissions = 0`, any
> authenticated caller holding any Insights role can read **all 1,139 tables** — including
> `Site DB`'s `tabUser`, `tabOAuth Bearer Token`, `tabAccess Log` and every business doctype.
> `guards.execute_transient`'s table-ACL step will therefore gate **nothing** on this
> deployment beyond role membership. Build it anyway (non-negotiable #3) — it is the one
> place to tighten later — but do not let a future session read "choke point" as "secured".
> One control *is* live: `apply_user_permissions = 1`, so row-level restrictions still bite.

### B. Deviation from §3.4 — new field instead of reusing `allowed_origins`

`origin.py` reads a **new `Insights Settings.mcp_allowed_origins`** (`Small Text`), not
`allowed_origins`. The design assumed `allowed_origins` was a newline-separated CORS-ish list;
it is a **single-line, comma-separated** CSP `frame-ancestors` list whose only consumer is
`insights/utils.py:215-224`. Reusing it would have made "may speak MCP to us" and "may iframe
our dashboards" one indivisible grant. `_split()` accepts **both** commas and newlines so a
copy-paste between the two fields still parses.

### C. Join key collisions — design §6.3/§6.4 gap, now MEASURED not inferred

`apply_join` (`ibis_utils.py:228-238`) calls `rename_duplicate_columns` on the right table
before joining; `get_right_table` **force-adds** `join_condition.right_column` to
`select_columns` (`:250-251`) even when the caller did not ask for it; and
`rename_duplicate_columns` (`:311-338`) renames any right-side name already present on the left
to `f"{sanitize_name(get_ibis_table_name(right))}_{col}"`.

**Measured on `demo_data`** — join `orders` → `orderitems` on `order_id`/`order_id`, with
`select_columns: [price]` only:

```
order_id, customer_id, order_status, order_purchase_timestamp, order_approved_at,
order_delivered_carrier_date, order_delivered_customer_date, order_estimated_delivery_date,
price, orderitems_order_id
                                    ^^^^^^^^^^^^^^^^^^^^ nobody asked for this
```

**Get the failure mode right.** The left `order_id` keeps its name — it does NOT disappear or
get renamed. What happens is that an **extra, unrequested column** (`orderitems_order_id`)
appears in the output schema. So the hazard is schema pollution, not a missing column:
`run_query` would report a column the model never asked for, the symbol table would carry it,
and Phase 2's `ChartSpec` could resolve against it. `select_columns` is not honoured as a
closed set.

**Compiler handling:** emit an explicit trailing `select` after any join, listing exactly the
symbol table's columns and **excluding the force-added right-side join key**. That kills the
junk column and simultaneously pins output order despite the `set` at `:245`. Any *other*
collision (both tables carrying `name`, `status`, …) is detected at compile time from the
resolver's two column lists and raises `ToolError` naming them, since the renamed form depends
on `get_ibis_table_name`, which is not the remote table name for warehouse-backed tables and
therefore is not reliably predictable.

**Fix verified on the same query:** adding `{"type":"select","column_names":["order_id",
"order_status","price"]}` yields exactly `['order_id', 'order_status', 'price']` — junk column
gone, order pinned.

**Consequence for golden tests:** the compiler's output for design §6.4's flagship example will
carry ONE MORE operation than the §6.4 "before" JSON shows — the trailing `select`. That is a
deliberate divergence, not a mismatch; §6.4 was written without knowing about the junk column.

**Do not** patch `ibis_utils.py:245` inside an MCP change — it would silently reorder columns
for every existing saved v3 join query. Separate PR, with its own before/after evidence.

### D. Dependency: interim install, fork still owed

`frappe_mcp` is installed **manually and is deliberately NOT in `pyproject.toml`**:

```bash
./env/bin/pip install "jsonschema>=4.24.0,<5"
./env/bin/pip install --no-deps \
  "frappe-mcp @ git+https://github.com/frappe/mcp@11d5076b1bf4483b2ff6751a13e0736f5396b1e6"
```

Verified after install: Werkzeug **3.1.6**, pydantic **2.12.5** — no downgrade. `pip check`
reports the two metadata conflicts; **that is the expected state**, and it is exactly what the
fork fixes. `rpds-py` ships a real cp314 wheel, so `jsonschema` needed no build.

**Putting the upstream URL in `pyproject.toml` would be a live footgun** — any
`bench setup requirements` / `bench update` would re-resolve against upstream's pins and
silently downgrade the WSGI layer, then fail to build `pydantic-core` on py3.14. Only the
**fork** SHA may go in. `pyproject.toml` carries a comment saying so.

`jsonschema` is declared as **our own** direct dependency because `validate.py` imports
`Draft202012Validator` directly — relying on it transitively via `frappe_mcp` would break the
moment we vendor `frappe_mcp`, which is §3.10's documented exit strategy.

**Fork status — DONE. Repo is `Irfan234-afif/frappe-mcp`** (note the name: `frappe-mcp`, not
`mcp`). Fork of `frappe/mcp`; branch `frappe-v16` pushed 2026-08-19 at
**`bef8fbf8f1e2ad053c3956712612c18d355bde1d`**, one file changed (`pyproject.toml`), two lines.

The clone lives at **`/home/irfan/python/frappe-mcp`** — a clone of the FORK, unrelated to
`apps/insights`. PR body staged at `~/python/frappe-mcp-PR_BODY.md`.

**Verification, run in this bench's own virtualenv — not taken on trust from issue #5:**

```
67 passed in 0.43s
python 3.14.6 · Werkzeug 3.1.6 · pydantic 2.12.5 · pydantic-core 2.41.5
jsonschema 4.26.0 · click 8.3.3
```

(`pytest` was installed into the bench env for this; dry-run confirmed it pulls only `iniconfig`
+ `pluggy` and touches neither Werkzeug nor pydantic.)

**Now pinned properly.** `insights/pyproject.toml` carries the fork **at the commit SHA, never a
branch name**, so `bench update` cannot move the transport underneath us. The interim
`--no-deps` install has been uninstalled and replaced by a normal
`pip install -e apps/insights`. Post-install state:

| | before (upstream, `--no-deps`) | after (fork, pinned) |
|---|---|---|
| Werkzeug | 3.1.6 | **3.1.6** |
| pydantic | 2.12.5 | **2.12.5** |
| `frappe-mcp` metadata | `Werkzeug==3.1.3`, `pydantic~=2.11.7` | `werkzeug<4,>=3.1.3`, `pydantic<3,>=2.11.7` |
| `pip check` | 2 conflicts reported | **clean** |

`pip check` going clean is the observable difference — the metadata no longer lies about what
the package needs. Endpoint re-verified and all 27 MCP tests still pass after the swap.

**Still owed:** open the PR (`frappe-v16` → `frappe/mcp:main`) against issue #5.

**If the fork ever needs another commit** (e.g. the `handle_initialize` rebind, should claude.ai
reject `2025-03-26`): commit in `~/python/frappe-mcp`, push, then update the SHA in
`insights/pyproject.toml` and re-run `pip install -e apps/insights`. A pinned SHA does not float.

### E. Transport acceptance — measured live, 2026-08-19

Endpoint `http://localhost:8001/api/method/insights.mcp.handle_mcp`, per-user API key.

| Case | Result |
|---|---|
| no auth | `401` + `WWW-Authenticate: Bearer resource_metadata="…/.well-known/oauth-protected-resource"` ✅ |
| `GET` with auth | `405` + `Allow: POST` ✅ |
| `POST`, no Origin | `200`, `protocolVersion 2025-03-26`, `MCP-Protocol-Version` echoed ✅ |
| `POST`, foreign Origin | `403 {"error":"origin_not_allowed"}` ✅ |
| notification | `202`, empty body ✅ |
| array body | `400` + JSON-RPC `-32600` (upstream would have 500'd) ✅ |
| `tools/list` | returns `ping_insights` with our explicit `inputSchema` and annotations ✅ |
| `tools/call` | returns a text block, `isError: false` ✅ |

**Open question #18 — HALF ANSWERED, 2026-08-19.** A real MCP client now connects:

```
$ claude mcp list
insights: http://localhost:8001/api/method/insights.mcp.handle_mcp (HTTP) - ✔ Connected
```

**Claude Code 2.1.235 accepts `protocolVersion: "2025-03-26"`** and completes `initialize`
against this server, so the hard-coded version is not a problem for the local/CLI path.
Registered with:

```bash
claude mcp add --transport http insights \
  http://localhost:8001/api/method/insights.mcp.handle_mcp \
  --header "Authorization: token <api_key>:<api_secret>"
```

Scope is `local` (stored in `~/.claude.json`, keyed to this project) — the credential does
NOT enter the repo. Claude Code sends no `Origin` header, so `origin.py` admits it without
any allowlist entry, which is the intended behaviour for non-browser clients.

**Still open: the claude.ai browser Connectors path.** It cannot be tested from `localhost`
— it needs a public tunnel plus `host_name` (see H). Do it while the fork PR is still in
flight, because the fix (a one-line `handle_initialize` rebind) is only cheap until it merges.

### F. Malformed JSON never reaches us — design OQ #22, resolved differently than expected

`frappe/app.py:326-332` `make_form_dict` runs `orjson.loads` during `init_request`, **before any
handler**, and `frappe.throw`s `DataError` on failure. So a malformed body with
`Content-Type: application/json` returns Frappe's `417` error envelope, not our `-32700` —
our parse branch never runs. Confirmed live.

With a non-JSON content type Frappe skips the parse (`if request_data and request.is_json`) and
our `-32700` **does** fire, verified. So the branch is reachable, not dead code — just not for
the case one would assume. Accepted as cosmetic: no conformant MCP client sends malformed JSON,
and the response is still JSON with a clear message. Do not spend effort here.

**The body-shape split, precisely** (`frappe/app.py:338-345`) — worth knowing before anyone
"simplifies" the `isinstance(body, dict)` guard away:

| Parsed body | Frappe does | Reaches `handle_mcp`? |
|---|---|---|
| `dict` | `form_dict = args` | yes — normal path |
| `list` | `form_dict["data"] = args` | **yes** — our `-32600` fires. This is the JSON-RPC batch shape, and the one upstream would `AttributeError` into a 500 on (`server.py:145`). **This is why the guard exists.** |
| scalar / `null` | `frappe.throw("Invalid request arguments")` → `417` | no |
| unparseable + `Content-Type: application/json` | `frappe.throw("Invalid request body")` → `417` | no |
| unparseable + any other Content-Type | nothing | yes — our `-32700` fires |

`test_transport.py` asserts "rejected and no tool ran" for the scalar case rather than a
specific status, so the suite is not coupled to a Frappe internal.

### G. ⚠️ SECURITY — a real hole in §3.4 as written, found and fixed

**The design's `origin.py` calls `frappe.utils.get_url()` to compute "the site's own origin".
That defaults to `allow_header_override=True`** (`frappe/utils/data.py:1837`), and when
`host_name` is absent from site config — as it is here — it **derives the origin from the
request's own `Host` header** (`:1845-1849`).

That makes "the site's own origin is always allowed" mean *"whatever origin the caller claims
is allowed"*. A DNS-rebinding attacker sends `Host: evil.example` + `Origin: http://evil.example`,
they match each other, and the request passes — defeating the exact attack the control exists
to prevent. Measured: before the fix, `Origin: http://localhost:8001` returned `200` against an
**empty** allowlist, which is only possible via the Host header.

**Fixed:** `origin.py` calls `get_url(allow_header_override=False)`. Re-measured — the rebinding
simulation now returns `403`. **Never remove that argument.** There is a test owed for it in
`test_transport.py`.

Consequence, and it is correct fail-closed behaviour: with no `host_name` set, a browser at
`http://localhost:8001` is now **denied** unless listed in `mcp_allowed_origins`. Non-browser
clients (Claude Code, `mcp-remote`, server-side connectors) send no `Origin` and are unaffected.

### H. `host_name` is not set on this site — needed before the claude.ai test

`sites/development/site_config.json` has no `host_name`, so `frappe.utils.get_url()` outside a
request returns `http://development`. This is fine today but blocks two things later:
the public tunnel for the claude.ai Connectors test, and correct OAuth discovery URLs for any
external client. When that test happens:

```bash
bench --site development set-config host_name https://<tunnel-host>
# then add the tunnel origin to Insights Settings > MCP Allowed Origins
```

**This is Irfan's call, not a silent change** — `host_name` also affects email links and other
generated URLs.

### J. `frappe-mcp check` cannot work with our architecture — do not chase it

The design (§3.3, §10) recommends `frappe-mcp check --app insights --verbose` as a Phase 1
smoke test. **It structurally cannot pass here, and that is expected, not a defect.**

`cli/utils.py:24` skips any `MCP` instance whose `_mcp_entry_fn` is `None`, and that attribute
is set in exactly one place — inside `MCP.register()` (`server.py:106`). §3.9 deliberately does
**not** use `@mcp.register()`, because `register()`'s wrapper calls the decorated function purely
for its import side-effect and **discards the return value** (`server.py:112`), which would make
it impossible to return our `401` / `403` / `405`. Those status codes are the whole point of the
wrapper, so `register()` is not an option.

Worse, faking the attribute would not help: `cli/utils.py:35` *calls* `_mcp_entry_fn()` outside
any request context to trigger registration, then `continue`s past the tool report if it raises.
`handle_mcp` would raise immediately on `frappe.request`.

**Replacement for the lint it would have given us** (every tool has a non-trivial `input_schema`
— non-negotiable #2): `test_transport.py::test_tools_list_exposes_explicit_input_schema`, which
asserts it over the wire. Strengthen that test as real tools land — assert non-empty
`properties` and that `annotations` are set. **Remove `frappe-mcp check` from the Phase 1
verification steps.**

### K. The failing operation index is NOT in the exception — §4.4 needs amending

Design §4.4 says *"Build failures emit a realtime toast naming only 'position N'
(`ibis_utils.py:108-117`); `errors.py` maps index N back to the spec field."* The first half is
right and the second half is not reachable the way it implies: `build()` catches each
per-operation exception, calls `create_toast(...)`, and then **re-raises the original exception
unchanged** (`ibis_utils.py:106-116`). `create_toast` (`insights/__init__.py:48-62`) forwards to
`frappe.publish_realtime` and returns nothing. So a synchronous caller catching that exception
has no access to the index at all.

**How `errors.py` solves it:** `capture_build_diagnostics()` is a context manager that swaps
`frappe.publish_realtime` for a recorder **for the duration of our own build call only**, and
collects `insights_notification` events. `position_from_diagnostics()` then parses
`"operation at position N"` (1-based in the toast, returned 0-based) and `as_tool_error()` maps
it through the compiler's `{op_index: spec_path}` map.

This is capturing *our own application's* diagnostic channel, not monkeypatching a third-party
internal, and it degrades to "no position, still the exception message" if the wording changes.
Verified against the real HTML-bolded toast string.

### L. `guards.py` deviates from the design in two places, both deliberate

1. **Tables are derived, not passed in.** The design's signature takes
   `resolved_tables=` as an argument. `guards.resolved_tables()` instead derives them
   from the operations via `insights.insights.query_utils.extract_table_deps_from_operations`
   (`query_utils.py:55`). A caller that forgot to list a table would otherwise skip its
   check silently — precisely the failure a choke point exists to prevent.
2. **A real `frappe.new_doc`, for both entry points.** The design suggested the
   `frappe._dict` duck-type from `test_ibis_utils.py:8-13` for the build path. Rejected:
   a real doc keeps us in lockstep with `InsightsQueryv3.execute()`'s paging, result
   cache, Date coercion and column extraction (~45 lines we do not want to re-implement),
   and it cannot silently miss an attribute `build()` reads. It is never inserted, so no
   hook fires and nothing is written.

`build_transient` is the second entry point — an expression, no rows — for `dry_run` and
`distinct_values`. Both wrap the call in `capture_build_diagnostics()` (see K) so a backend
failure comes back with an operation index instead of a bare traceback.

**Verified against real data** (`demo_data`, DuckDB): both entry points work, a user with no
Insights role is refused, and the execution log row carries the `mcp-` prefix — so every
MCP-originated execution is greppable in `Insights Query Execution Log`.

### M. `validate_expression` is broken BOTH ways on this bench — design §6.3 step 2 needs care

Design §6.3 validation step 2 says every `derive.expression` is checked via
`validate_expression(expression, column_options)`. Two traps, both measured.

**Trap 1 — the column-metadata key is `description`, not `data_type`.**
`get_ibis_dtype` (`ibis/utils.py:213-230`) reads `col.get("description")` for the type.
Pass `data_type` and it returns `{}`; `validate_types` then short-circuits on
`if not schema: return {"is_valid": True, "errors": []}` (`:368-371`) and **type checking is
silently skipped — everything looks valid.** Note the sibling endpoint
`get_code_completions` (`utils.py:131-133`) reads `data_type` instead; the two disagree.

**Trap 2 — with the CORRECT key, every expression is rejected when safe_exec is off.**
`validate_types` runs the expression through `safe_exec`.

> **STATUS CHANGED 2026-08-19: `server_script_enabled: 1` is now set in
> `common_site_config.json`, so `is_safe_exec_enabled()` is `True` and the type stage
> works properly here.** Measured after enabling: `amount.sum()`, `amount * 2` and
> `(amount > 100) & (status == 'C')` all validate; `amount + 'abc'` is caught as
> *"Type error: unsupported operand type(s)"*; `amount.nonexistent_method()` is caught.
> The table below records the DISABLED behaviour, which still applies to any deployment
> that has not turned server scripts on — the compiler handles both and both are tested.

Measured while it was disabled:

| `column_options` key | `amount.sum()` (valid) | `amount + 'abc'` (type error) | `foo + 1` (unknown column) |
|---|---|---|---|
| `description` (correct) | ❌ *"Server Scripts are disabled"* | ❌ same message | ✅ `Column 'foo' not found.` |
| `data_type` (wrong) | ✅ valid | ✅ **valid — wrong!** | ✅ `Column 'foo' not found.` |

Name and syntax validation run BEFORE `validate_types` and work in both columns — those are
the parts we can actually rely on.

**How the compiler uses it** (`compiler.py::_validate_expression`): passes the **`description`**
key, and treats a failure carrying the server-scripts marker as *"type validation unavailable"*
rather than invalid — without that, every `derive` is rejected on a bench with server scripts
off. Gated on `is_safe_exec_enabled()` so the intent is explicit rather than a string match.
Syntax and unknown-column errors stay blocking in both worlds.

Both branches are pinned by `test_compiler.py::TestExpressionValidation`: the type-error case
runs for real (skipped only when safe_exec is off), and the disabled-bench fallback is exercised
through a test double, so a bench-config change cannot silently turn either into a no-op.

Related: `hint` is **conditionally present** in error dicts (`create_error`, `utils.py:206-210`
only sets it when truthy) — do not treat it as a guaranteed key. And note the design's §11
open question #16 about the `safe_exec` sandbox is partly answered: safe_exec is **off** on this
bench, so a `code` operation would fail anyway. Non-negotiable #9 still stands.

### N. Two more design-doc corrections from the vocabulary sweep

1. **`get_data_source_table_columns` does NOT emit `name`.** §4.5 says one endpoint emits
   `column` and another emits `name`, and tells the tool to normalise. Both
   `get_data_source_table_columns` (`api/data_sources.py:361-368`) and `get_schema`
   (`:466-473`) emit the **same three keys: `column`, `label`, `type`**. The `name` key belongs
   to `QueryResultColumn` (`query.types.ts:197-200`), which is the query *result* shape, not the
   schema shape. Do not conflate them; there is no normalisation to do.
2. **Cross-data-source joins fail at EXECUTE time, not build time.** The Indonesian throw is
   inside `execute_ibis_query` (`ibis_utils.py:963-973`), *after* `ibis.to_sql()` has already
   succeeded, and the condition is a substring match on `"Multiple backends"` in an `IbisError`.
   There is **no `stored == 1` test** in that path — the relationship is indirect: with
   `use_live_connection` falsy every table resolves through the one DuckDB warehouse, so there
   is only one backend and the check never trips. Consequence: the compiler cannot rely on a
   build step to catch a cross-source join and **must compare `data_source` itself, up front**.

**Granularity rules the design does not state** (`ibis_utils.py:827-836, 857-911`):
`Time` dimensions accept only `second`/`minute`/`hour`; `Date`/`Datetime` accept all nine. An
unknown granularity is a hard `frappe.throw`, not a silent no-op. But granularity on a
**non-date** dimension is silently ignored — `translate_dimension` only consults it for Time or
date types — which is why the design's "strip granularity for String dimensions" mirror rule
matters. `week` and `fiscal_year` depend on `Insights Settings.week_starts_on` and
`fiscal_year_start`, so their output is site-configurable, not ISO-fixed.

### O. Compiler decisions worth not re-litigating

1. **Auto-cast runs immediately before `summarize`, NOT with the explicit `cast[]` at
   step 3 of §6.3's table.** A `group_by` may target a column produced by `derive` or
   renamed by `rename`, neither of which exists at step 3. Caught by
   `test_compiler_integration`, not by the golden tests — which is the entire argument
   for having both.
2. **The compiler emits a trailing `select` after a bare join** (no summarize, no
   explicit `select`). Kills the force-added junk column (§8 C) *and* pins output order
   despite the `set` at `ibis_utils.py:245`. Suppressed when a `summarize` follows,
   because aggregation already projects to exactly its outputs.
3. **Wrong enum casing is rejected, never corrected** (§6.3 rule 4). The error says
   *"Casing matters here. Did you mean 'month'?"* so the model learns. Silent correction
   would train it to keep guessing.
4. **`SchemaResolver` is injected.** `LiveSchemaResolver` wraps
   `get_data_source_table_columns`; `StaticSchemaResolver` makes `test_compiler.py` a
   pure unit test — no DB, no ibis, no data source, 34 tests in 4ms.
5. **The symbol table is staged, not flat.** That is what lets `require()` say *"`price`
   existed before the summarize at operation 3 but not after it"* and list the valid
   aliases. A flat table cannot say where a column went, and that is the #1 semantic
   error the model makes.
6. **`SymbolTable.to_json()` / `from_json()` is the Phase 2 contract.** `run_query`
   already returns `columns[]`, so that IS the wire format; a `ChartSpec` resolves
   against either a live table or one rehydrated from a prior response. Verified against
   the real backend: the symbol table equals the executed query's schema exactly.

### P. `test_ibis_utils` was red for a config reason, now green

For the record, because it cost a diagnosis: `insights.tests.test_ibis_utils` failed with 7
`ServerScriptNotEnabled` errors while server scripts were off. The suite fabricates its
fixtures with a `{"type": "code"}` source operation (`test_ibis_utils.py:14-33`), which needs
`safe_exec`. **Pre-existing and unrelated to the MCP work** — nothing under `insights/mcp/` is
imported by it.

`server_script_enabled: 1` was set on 2026-08-19 and it is green again (5 tests). Re-verified
after the change: all 77 MCP tests, plus `test_permissions` (8) and `test_warehouse` (3).

**Security note that survives the change.** Server Scripts let a role-holder execute Python
server-side; Frappe keeps the switch in `common_site_config.json`, outside the UI, for that
reason. Turning it on removed an ambient mitigation that was quietly backing non-negotiable #9
(no MCP tool emits a `code` operation until the sandbox policy is reviewed). **Rule #9 now
stands on its own, enforced only by
`test_guards.py::test_rule_9_no_tool_emits_a_code_operation`.** Open question #16 is still open.

### Q. ⚠️ DuckDB segfaults the web process on this bench — NOT an MCP bug

**Symptom.** Any live query against the DuckDB-backed `demo_data` source, served over
HTTP, kills the web worker with **SIGSEGV**. The request returns an empty body and
`bench serve` dies. Reproduced twice; core dump at
`/var/crash/_usr_bin_python3.14.1009.crash` (`Signal: 11`, `VmRSS: 300 MB` — not OOM).

**Fault site,** from the `faulthandler` dump:

```
guards.py:87 execute_transient
  insights_query_v3.py:165 execute
    ibis_utils.py:991 execute_ibis_query
      frappe/concurrency_limiter.py:108
        ibis_utils.py:1023 _execute_live_query
          ibis/backends/duckdb/__init__.py:1424 execute   <-- SIGSEGV
```

**It is not ours, and it is not MCP-specific.** Everything from
`insights_query_v3.execute` downward is the exact path the Insights UI uses via
`run_doc_method`; the only MCP frames are above it. Versions: **duckdb 1.4.5 on Python
3.14.6**, ibis 11.0.0, pyarrow 25.0.0. Python 3.14 is new enough that a native
extension built against ≤3.13 is the obvious suspect.

**Scope, measured:**

| source | backend | over HTTP | in-process |
|---|---|---|---|
| `demo_data` | DuckDB | **SIGSEGV** | works (0.24s) |
| `Site DB` | MariaDB | works (0.033s) | works |

The same demo query runs fine in-process and inside a plain `threading.Thread`, so a
naive "DuckDB is not thread-safe" reading does not explain it; only the werkzeug
request-handler context reproduces it. Root cause not isolated further — it is a
third-party native crash, not something this layer can fix.

**Consequences for Phase 1:**
* **DECIDED 2026-08-19: Phase 2 chart testing uses `datalake`.** Charts and dashboards
  mean many renders and many queries, so `demo_data` over HTTP would take the bench down
  repeatedly. `demo_data` remains fine for the test suite, which runs in-process.
  `datalake` also has the documentation corpus (§8 R) behind it, which is what makes a
  realistic end-to-end demo possible.
* Do not read an empty MCP response as a transport bug — check whether the web process
  is still alive first.
* Worth retesting under gunicorn (production serving) before concluding it affects
  deployment; `bench serve` is a development server.

**CORRECTION — an earlier draft of this note claimed Redis was independently broken on
this bench. That was wrong.** Redis starts correctly and binds both ports:

```
redis_cache.1 | Running mode=standalone, port=13001 ... Ready to accept connections
redis_queue.1 | Running mode=standalone, port=11001 ... Ready to accept connections
```

The observation behind the mistake was a snapshot taken *after* the whole `bench start`
group had already gone down. The only process with a crash dump is `web.1`; a process
manager of honcho's kind stops the remaining group members when one exits, so Redis went
with it. **Redis is a symptom, not a second cause.** Do not go looking for a Redis
misconfiguration — there isn't one, and the DuckDB SIGSEGV is the single root cause.

Practical consequence: after this crash the ENTIRE bench is down, not just the web
worker. An empty MCP response means `bench start` needs restarting.

### R. The `datalake` documentation is loaded — 892 blocks, and how it got sliced

Irfan's existing documentation at `~/datalake/docs/database/` (11 markdown files,
~600KB, plus `erd.html`) is imported. **797 of 846 tables (94%) now carry
table-scoped documentation.**

| | blocks |
|---|---|
| Table-scoped | 864 |
| Data-source-scoped | 28 (11 file overviews + 17 extracted diagrams) |
| **Total** | **892**, all zone `Documentation` |

**Import path: `insights/api/docs.py`, human-only by construction.** Two whitelisted
functions, `import_markdown_docs` and `import_html_diagrams`, both `frappe.only_for(
"Insights Admin")` and both writing `zone = "Documentation"` — which
`InsightsDataDoc.validate()` refuses whenever `frappe.flags.insights_mcp_write` is set.
No MCP tool can reach them. Both default to `dry_run=True` so a person confirms the
proposed split before anything is written (design §5.5's requirement).

**Slicing rule — backticks only.** A heading becomes a table block only when it names a
table *in backticks* and that name resolves to a real `Insights Table v3` row. An
earlier attempt matched bare words against schema-stripped table names and scored a
flattering 77% that was mostly garbage: the heading *"Pemetaan sumber → PT/brand"* bound
to `staging.netsuite__brand` because the word "brand" appeared. Do not loosen this.

**Pattern expansion.** `bronze-legacy.md` documents 29 table PATTERNS replicated across
9 source systems, so its headings name no single table — `ms_kdbrg` is a pattern;
`bronze.legacy_909sromorbit_ms_kdbrg` and eight siblings are the tables. With
`expand_patterns=True` (the default) each pattern attaches to every matching table,
taking `bronze.legacy_*` from 0 to 253/253 documented. The shared text is prefixed with
a line saying so, so anyone correcting it knows there are 8 siblings. **Accepted cost:
~9× duplication.** Irfan chose this over a single source-level blob.

**`erd.html` was extraction, not conversion** — it already embeds 17 Mermaid diagrams in
`<pre class="mermaid">`, with titles in the surrounding `<details class="panel">`
markup. They import as data-source blocks with `sort_order` 100+, so the written
narrative is served first: prose grounds a model better per byte than a 15KB flowchart.

### S. Two `compose()` bugs the real corpus found that the fixtures could not

Both surfaced immediately on calling `get_docs("datalake")` through the MCP client, and
both would have shipped: `test_docs.py`'s fixtures were small enough that neither could
fire. Regression tests are now in `TestCompositionBudget`.

1. **Empty husks.** With 28 source-scoped blocks against one 8KB narrative budget, the
   first block consumed nearly all of it and the remaining eleven came back as heading +
   provenance line + "…truncated" with *no body*. To a model that reads as "the
   documentation is empty". Now `compose()` stops cleanly and returns an index of the
   omitted blocks with their ids, and `render_blocks` prints
   *"N further block(s) not shown — fetch with get_docs(block_id=…)"*.
2. **The paragraph trim could eat the whole block.** `_to_block` trimmed back to the
   last `\n\n` so a truncation would not end mid-sentence. For a body shaped
   `"Section 2.\n\n<3KB of prose>"` the last break sits at offset 10, so the trim
   returned **ten bytes** — and because `spent` then barely grew, the budget never
   engaged and the cap silently did nothing. The boundary is now honoured only when it
   keeps >60% of the allowed text, and a truncated body below `MIN_USEFUL_BLOCK` (400B)
   is dropped rather than emitted.

The lesson worth carrying into Phase 2: **the tier caps need a realistic corpus to
test against.** Every one of these paths passed its unit tests.

### I. Housekeeping

An API key/secret was generated for `irfan@sosco.id` for MCP testing (none existed before).
Separately and unrelated: the `upstream` git remote contains a **plaintext GitHub PAT**;
it needs rotating and the remote re-adding without credentials.

---

## 9. Starting a new session

Paste this, filling in the phase:

```
Read docs/mcp-IMPLEMENTATION.md completely first — it is the working doc for this project
and it tells you what is already built and what the rules are.

I'm working on Phase <N> of the Insights MCP server. Follow the reading guide in §4:
load only the listed sections of docs/mcp-server-design.md, not the whole file (it is 182KB).

Respect the non-negotiables in §3 — those were decided after two adversarial reviews.
If you think one is wrong, tell me and stop; don't silently do it differently.

Work through the §7 checklist for this phase. Update §1 and §7 before you finish.
```

**Before you end a session:** update §1 (status + date), tick §7 honestly, and append anything
you learned to §8. The next session has none of your context — this file is all it gets.
