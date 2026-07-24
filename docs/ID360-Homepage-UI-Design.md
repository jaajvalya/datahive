# ID360 Homepage — UI Design Document

**Product:** ID360 — personalized data-asset homepage (Atlan-style layout, ACL Digital brand)
**Version:** 1.0
**Owner:** ID360 team
**Related deliverables:** `frontend/index.html` (front-end), `backend/main.py` + `backend/requirements.txt` (back-end)

---

## 1. Purpose & scope

ID360's homepage is a **personalized landing page for a data/asset catalog**. On login, each user lands on a single workspace that surfaces the assets relevant to them, the announcements affecting their data, recent activity, and quick entry points into search and creation.

This document defines the **visual design, layout, components, and interaction states** for that homepage, re-skinned in the **ACL Digital** brand, and specifies the **backend functionalities and dependencies** the page requires.

The reference layout (three-zone shell: top bar, left icon rail, two-column body) is preserved; only the theme, typography, and component styling are ACL-branded.

---

## 2. Brand & design tokens

### 2.1 Color palette

| Token | Value | Usage |
|-------|-------|-------|
| `--navy-900` | `rgb(24, 41, 120)` `#182978` | Hero/section backgrounds, dark panels, primary brand |
| `--navy-700` | `rgb(0, 86, 167)` `#0056A7` | Secondary navy, gradient stop, active nav |
| `--blue-600` | `rgb(4, 107, 210)` `#046BD2` | Links, info accents, illustration gradients |
| `--blue-500` | `rgb(51, 117, 179)` `#3375B3` | Supporting blue |
| `--blue-800` | `rgb(19, 84, 122)` `#13547A` | Deep supporting blue |
| `--orange-500` | `rgb(255, 103, 31)` `#FF671F` | **Primary accent** — CTAs, stats, icons, hover, active underline |
| `--white` | `rgb(255, 255, 255)` `#FFFFFF` | Main background, text on dark |
| `--ink-900` | `rgb(1, 8, 24)` `#010818` | Strongest text |
| `--ink-700` | `rgb(33, 37, 41)` `#212529` | Body copy on light |
| `--gray-100` | `rgb(246, 249, 255)` `#F6F9FF` | Subtle card/section background (cool) |
| `--gray-150` | `rgb(244, 244, 244)` `#F4F4F4` | Section background |
| `--gray-200` | `rgb(241, 241, 241)` `#F1F1F1` | Dividers, chips, borders |

**Semantic colors** (for announcements/status): success/verified `#1FA971`, warning `#F6C445` (amber card `#FFF6DC`), error `#E4572E` (red card `#FDE7E1`).

### 2.2 Typography

- **Font family:** `Manrope` (Google Fonts), fallback `-apple-system, "Segoe UI", Roboto, sans-serif`.
- **Weights:** 400 regular (large hero headings, body), 500 medium (subheadings, buttons, nav), 700 bold (emphasis, stats, privacy/notice text), 800 for the wordmark feel.
- **Scale:** hero greeting 28–32px/600; card titles 18px/700; body 14px/400; meta/caption 12px/500; stat numbers 32–40px/700 in `--orange-500`.

### 2.3 Buttons

- **Primary:** solid `--orange-500` fill, white text, radius 8px, 500 weight (e.g. "Know More", "+ New").
- **Secondary:** transparent fill, `--orange-500` 1.5px outline + orange text (e.g. "Play Now", "Discover all assets →").
- **Ghost/nav:** no border, `--ink-700` text, orange on hover/active.

### 2.4 Elevation & shape

- Cards: white, radius 12px, border `1px solid --gray-200`, soft shadow `0 1px 3px rgba(1,8,24,.06)`.
- Chips/pills: radius 999px, `--gray-150` background, active = orange text + orange ring.
- Icon rail items: 40px hit target, active state = orange icon + left orange indicator bar.

---

## 3. Layout

Three-zone application shell:

```
┌───────────────────────────────────────────────────────────────┐
│  TOP BAR:  [ACL Digital logo] ......... [+New][?][🔔][user ▾]  │
├──────┬────────────────────────────────────────────────────────┤
│ LEFT │  HERO (navy gradient): greeting + global search        │
│ ICON │ ┌───────────────────────────┐ ┌──────────────────────┐ │
│ RAIL │ │ Relevant to you (card)    │ │ Recent announcements │ │
│      │ │  tabs · filter chips      │ │  (alert cards)       │ │
│ Assets│ │  asset list (scroll)     │ ├──────────────────────┤ │
│ Gloss.│ └───────────────────────────┘ │ Newly added resources│ │
│ ...  │ ┌───────────────────────────┐ │  (activity feed)     │ │
│      │ │ Personalise (Persona/Purp)│ └──────────────────────┘ │
│ Docs │ └───────────────────────────┘                          │
└──────┴────────────────────────────────────────────────────────┘
```

- **Responsive:** ≥1200px = two-column body; 768–1199px = right column stacks under primary; <768px = single column, left rail collapses to a hamburger drawer.
- **Grid:** 12-col, 24px gutters, max content width 1360px, left rail fixed 72px.

---

## 4. Components

### 4.1 Top bar (global header)
ACL Digital logo (left) · flexible spacer · **"+ New ▾"** (primary orange) · **?** help · **🔔 notifications** with count badge · **user chip** (avatar + name + ▾). Sticky, white, bottom border `--gray-200`, height 56px.

### 4.2 Left icon rail
Fixed vertical rail. Items (icon + label): **Assets, Glossary, Insights** / **Workflows, Governance, Admin, Reporting** / (pinned bottom) **Support, Docs**. Active item = orange icon + 3px orange left indicator; hover = `--gray-100` background.

### 4.3 Hero band
Navy gradient (`--navy-900 → --navy-700`) with subtle isometric tech line motif. Contains:
- **Greeting:** time-aware, "Good afternoon, {firstName}!" + weather glyph.
- **Global search:** full-width input, placeholder "Search all your assets", white field, orange focus ring; right link **"Discover all assets →"** (secondary).

### 4.4 "Relevant to you" card
- **Tabs:** `My drafts` | `Recently verified assets` (active underlined orange).
- **Filter chips with counts:** `View n · Column n · Query n · Term n · Category n · Glossary n · Dashboard n` + overflow `…`. Selected chip = orange ring/text.
- **Asset list (scrollable):** each row = asset-type icon · asset name + verified check · type breadcrumb · "edited N ago"; optional status glyph (⚠) on the right. Row hover highlights; click → asset detail (out of scope here).

### 4.5 "Personalise your experience" card
Tabs **Persona (n)** and **Purpose (n)** controlling what the feed surfaces.

### 4.6 Right column
- **Recent announcements:** stacked alert cards color-coded by severity (amber = warning, red = error, neutral = info). Each: asset chip + verified, severity label, bold title, body, author avatar + name, relative time. Scrollable.
- **Newly added resources:** activity feed — bot/user entries with @mentions and #hashtags, newly added assets, timestamps.

### 4.7 States
Every data region defines: **loading** (skeleton shimmer), **empty** (icon + one-line guidance), **error** (inline retry), **unauthorized** (redirect to login). Search shows a debounced results dropdown; no-results state included.

---

## 5. Interaction & accessibility

- Keyboard: full tab order, `/` focuses global search, `Esc` closes menus, arrow keys move chip focus.
- WCAG 2.1 AA: orange-on-white text only ≥ semibold/large or paired with `--ink-700`; never orange body text on white below AA contrast — use orange for accents/large numerals, `--ink-700` for reading text. All icons have `aria-label`. Focus visible (orange ring).
- Motion respects `prefers-reduced-motion`.

---

## 6. Required backend functionalities

The homepage is data-driven; the backend (FastAPI, per ID360 standards) must expose:

1. **Authentication & session** — credential login issuing short-lived JWT access tokens; token verification; logout. RBAC roles (`viewer`, `editor`, `admin`).
2. **Current user / profile** — `me` endpoint returning display name, avatar, roles, and the time-aware greeting context.
3. **Relevant assets** — list of assets relevant to the user, filterable by `tab` (`my_drafts` | `recently_verified`) and by asset `type`, returning per-type **counts** for the filter chips.
4. **Global search** — debounced query endpoint over assets (name, type, description) with pagination and result ranking.
5. **Announcements** — recent announcements scoped to the user's assets, with **severity** (info/warning/error), linked asset, author, and timestamp.
6. **Newly added resources / activity feed** — recent activity items (new assets, conversations with @mentions/#hashtags).
7. **Personalization** — list Personas and Purposes; get/set the user's active persona & purpose (drives what the feed surfaces).
8. **Reference/lookup** — asset types and counts used to build filter chips.
9. **Health & readiness** — liveness/readiness probes.

### 6.1 Enterprise / non-functional requirements (ID360 mandate)

- **Security in transit:** TLS 1.2+ only; HSTS; secure cookies; strict CORS allow-list.
- **Security at rest:** secrets from environment/secret manager (never in code); SQLite/DB file on an encrypted volume (LUKS/FDE) or use a DB with TDE; password hashing with bcrypt; PII minimization.
- **No data leakage:** field-level response models (Pydantic) so only whitelisted fields are returned; generic error messages; no stack traces to clients.
- **Auditability:** append-only audit log for every security-relevant and data-access action (who, what, when, from where, result).
- **Traceability:** per-request correlation/`X-Request-ID`, structured JSON logs, timing, and outcome for every request.
- **Abuse protection:** per-client rate limiting; request size limits; input validation.
- **Observability:** health endpoints, structured logs suitable for shipping to a SIEM.

### 6.2 API surface (implemented in `backend/main.py`)

| Method | Path | Function |
|--------|------|----------|
| `GET`  | `/api/v1/health` | Liveness/readiness |
| `POST` | `/api/v1/auth/login` | Login → JWT |
| `POST` | `/api/v1/auth/logout` | Invalidate session (audit) |
| `GET`  | `/api/v1/me` | Current user + greeting |
| `GET`  | `/api/v1/assets/relevant` | Relevant assets (`tab`, `type`) + type counts |
| `GET`  | `/api/v1/assets/search` | Global search (`q`, `limit`, `offset`) |
| `GET`  | `/api/v1/announcements` | Recent announcements |
| `GET`  | `/api/v1/resources` | Newly added resources / activity feed |
| `GET`  | `/api/v1/personas` | Personas + active |
| `GET`  | `/api/v1/purposes` | Purposes + active |
| `PUT`  | `/api/v1/personalization` | Set active persona/purpose |

All data endpoints require a valid Bearer token; responses are shaped by Pydantic models so the front-end contract is stable.

---

## 7. Dependencies

### 7.1 Backend (Python 3.11+, `backend/requirements.txt`)

| Package | Purpose |
|---------|---------|
| `fastapi` | Web framework (ID360 standard for API connectors) |
| `uvicorn[standard]` | ASGI server |
| `pydantic` (v2) | Request/response models, validation, field whitelisting |
| `pydantic-settings` | Typed config from environment (secrets, CORS, TTLs) |
| `python-jose[cryptography]` | JWT signing/verification |
| `passlib[bcrypt]` | Password hashing at rest |
| `python-multipart` | Form parsing for login |
| `slowapi` | Rate limiting |
| `SQLModel` *(or `SQLAlchemy`)* | ORM over SQLite/Postgres |

Standard-library only: `sqlite3`, `logging`, `uuid`, `datetime`, `secrets`, `hmac` — used for the audit log, request tracing, and the zero-extra-dependency fallback store.

> The provided `main.py` runs with **only `fastapi`, `uvicorn`, `pydantic`, `python-jose`, `passlib[bcrypt]`, `python-multipart`, and `slowapi`**; the ORM row is optional and noted where a production DB would replace the built-in SQLite layer.

### 7.2 Frontend (`frontend/index.html`)

Zero build tooling — a single self-contained HTML file:

- **Manrope** via Google Fonts `<link>`.
- Vanilla **HTML/CSS/JS** (no framework, no bundler) so it is trivially pluggable.
- Configuration: a single `API_BASE` constant points at the FastAPI backend; a built-in **mock dataset** lets the page render standalone if the API is unreachable.
- Talks to the backend with `fetch` + `Authorization: Bearer <token>`.

### 7.3 Integration contract (how the pieces plug together)

1. Run the backend: `uvicorn main:app` (defaults to `http://localhost:8000`).
2. In `index.html`, set `const API_BASE = "http://localhost:8000/api/v1"`.
3. The front-end logs in (`/auth/login`), stores the JWT in memory, and calls the data endpoints. CORS on the backend is restricted to the front-end origin.
4. If `API_BASE` is empty or unreachable, the front-end falls back to its embedded mock data so the design is always demoable.

---

## 8. Deliverable map

| Deliverable | File |
|-------------|------|
| UI design document (this file) | `docs/ID360-Homepage-UI-Design.md` |
| Complete front-end script | `frontend/index.html` |
| Complete back-end script (pluggable) | `backend/main.py` |
| Back-end dependencies | `backend/requirements.txt` |
