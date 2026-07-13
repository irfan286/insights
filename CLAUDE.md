# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

Frappe Insights is a powerful reporting and analytics tool built on the Frappe Framework. It allows users to connect data sources, build queries visually or with SQL, create dashboards, and share reports — without needing to write SQL.

## Development Commands

This app runs inside a Frappe bench environment (Docker-based at `frappe_docker_v15`).

### Backend (Python / Frappe)

```bash
bench start                          # start all services
bench migrate                        # apply DB migrations after schema changes
bench build --app insights           # build JS/CSS assets via bench
bench run-tests --app insights       # run Python tests
bench console                        # Python REPL with frappe context
```

### Frontend (Vue 3 + Vite)

All frontend commands run from the `frontend/` directory (or use root-level aliases):

```bash
yarn dev          # start Vite dev server (proxies API to bench)
yarn build        # production build → copies HTML to insights/www/
yarn test         # run Playwright e2e tests
```

From repo root:
```bash
yarn dev          # delegates to frontend/yarn dev
yarn build        # delegates to frontend/yarn build
```

Frontend linting/formatting uses ESLint (`eslint-plugin-vue`) and Prettier. No explicit lint script — run `npx eslint frontend/src` or `npx prettier --check frontend/src`.

### Running a Single Playwright Test

```bash
cd frontend && npx playwright test tests/<test-file>.spec.ts
```

## Architecture

### Backend

Built on **Frappe Framework** (Python). Key directories:

- `insights/api/` — whitelisted API endpoints called by the frontend. Modules: `queries`, `data_sources`, `dashboards`, `workbooks`, `notebooks`, `alerts`, `data_store`, `permissions`, `setup`, `user`, `shared`, `public`, `subscription`
- `insights/insights/doctype/` — Frappe doctypes (DB schema + business logic). There are v2 and v3 variants of most core doctypes (`insights_data_source_v3`, `insights_query_v3`, `insights_dashboard_v3`, etc.). Active development is on `_v3` doctypes.
- `insights/api/data_store.py` — scheduled syncing of table metadata from connected data sources
- `insights/hooks.py` — app registration, scheduled tasks, fixtures

**Key backend dependency:** [Ibis](https://github.com/ibis-project/ibis) is used to compose SQL queries programmatically with a dataframe-like API, supporting multiple database backends.

Two built-in data sources are always present as fixtures: `Site DB` (the Frappe MariaDB instance) and `Query Store` (queries used as data sources).

### Frontend

Vue 3 SPA with Vite, Pinia for state, Vue Router, Tailwind CSS, and [frappe-ui](https://github.com/frappe/frappe-ui) component library.

- `frontend/src/query/` — query builder UI (visual builder + SQL editor). `query/visual/` is the node-based visual query builder using `@vue-flow/core`.
- `frontend/src/dashboard/` — dashboard builder with drag-and-drop layout (`grid-layout-plus`)
- `frontend/src/notebook/` — notebook feature with TipTap rich text + embedded query blocks
- `frontend/src/datasource/` — data source connection and table browsing
- `frontend/src/components/Charts/` — eCharts-based chart components
- `frontend/src/stores/` — Pinia stores
- `frontend/src/api/` — typed wrappers around Frappe API calls

The frontend is served at `/insights` when built (`yarn build` copies `index.html` to `insights/www/insights.html`). During development, Vite proxies API calls to the running bench server.

### Versioning Note

The codebase contains both legacy (v1/v2) and current (v3) implementations of doctypes. The `_v3` suffix doctypes and corresponding frontend code are the active path. Legacy code under `frontend/src/query/deprecated/` and non-v3 doctypes are kept for backwards compatibility.
