# Atlan Homepage — UI/UX Breakdown

**Source video:** *Navigating the Atlan Homepage* — Atlan (YouTube)
**URL:** https://youtu.be/Mnr_kaLhg8c
**Length:** 5:58 · Published Jan 21, 2023
**Screen shown:** `home.atlan.com` (a single, static homepage view narrated throughout — the presenter does not click into other screens)

> **Note on method:** The video is a narrated tour that stays on one homepage screen for its full duration. The details below are transcribed directly from the on-screen UI (captured frame-by-frame) and supplemented with the narration. Where the video does *not* actually demonstrate something (e.g. pop-up modals), that is called out explicitly rather than assumed.

---

## 1. Home page design (layout, not branding)

The homepage uses a **three-zone layout** inside a standard web-app shell:

- **Top:** a slim global **app header/top bar** spanning the full width.
- **Far left:** a narrow, fixed **icon rail navigation**.
- **Body:** a **two-column content area** — a wide primary/center column and a narrower right-hand column.

Structural characteristics observed:

- A **blue gradient "hero" band** sits at the top of the body, containing the greeting and the global search bar. Content below the hero sits on **white rounded cards** against a light background.
- The layout is **card-based** — each functional block (Relevant to you, Personalise your experience, Recent announcements, Newly added resources) is its own bordered/rounded card.
- Cards with long content have their **own internal scrollbars** (the asset list and the announcements list both scroll independently).
- The overall intent, per the narration, is a **personalized landing page** that acts as "a single workspace for all your data assets" and shows each user "information about what's going on in my own little data" world.
- The greeting is **time-aware** ("Good afternoon, Matt Madden!") with a small weather/sun icon.

---

## 2. Left-hand side navigation

A fixed, vertical **icon rail** on the far left. Each item is an icon with a small text label beneath it. Top to bottom:

| Order | Item | Purpose (as labelled) |
|-------|------|-----------------------|
| — | **atlan** logo | Brand mark at top of the rail |
| 1 | **Assets** | Search/discover data assets (compass icon) |
| 2 | **Glossary** | Business glossary — terms, categories (book icon) |
| 3 | **Insights** | Query/SQL workspace (lightning-bolt icon) |
| — | *(divider / spacing)* | |
| 4 | **Workflows** | Automation / pipelines (play-in-circle icon) |
| 5 | **Governance** | Policies & governance (bank/columns icon) |
| 6 | **Admin** | Administration (people/gear icon) |
| 7 | **Reporting** | Reports & metrics (bar-chart icon) |
| — | *(pushed to bottom)* | |
| 8 | **Support** | Help/support (headset icon) |
| 9 | **Docs** | Documentation (open-book icon) |

The rail is **persistent** across the product and groups items into three clusters: core modules (Assets / Glossary / Insights), operational modules (Workflows / Governance / Admin / Reporting), and utility links pinned to the bottom (Support / Docs).

---

## 3. The main content (center column)

From top to bottom:

**Greeting (hero band)**
- Sun/weather icon + **"Good afternoon, Matt Madden!"** (personalized, time-of-day aware).

**Global search bar (hero band)**
- Full-width input with the **atlan** mark and placeholder **"Search all your assets."**
- Right-aligned link: **"Discover all assets →."**
- Narration positions search as **"front and center,"** noting you can *"instantly click"* to search/discover assets from anywhere on the page.

**Card — "Relevant to you"**
- Two sub-tabs: **My drafts** and **Recently verified assets** (active tab underlined).
- A row of **filter chips with live counts**, letting you narrow the list by asset type:
  `View 5 · Column 3 · Query 9 · Term 23 · Category 5 · Glossary 3 · Dashboard 7` (Dashboard shown selected) plus an overflow **"…"**.
- A scrollable **asset list**; each row shows: a colored asset-type icon, the **asset name + green "verified" check badge**, a **type breadcrumb**, and a **"edited N months ago"** timestamp. Rows observed:
  - **Food Beverage Order Analysis** — Dashboard › Food Beverage Order Analysis — edited 3 months ago *(⚠ warning indicator on the right)*
  - **Customer Acquisition Cost Metrics** — Dashboard › Customer Acquisition Cost — edited 3 months ago
  - **Consolidated_dashboard** — Dashboard › Cost Overruns Dashboard — edited 7 months ago
- Narration frames this list around **trust and popularity** — surfacing verified assets and *"data everyone else is using,"* plus **ownership** signals.

**Card — "Personalise your experience"**
- Two tabs: **Persona (2)** and **Purpose (5)** — the levers that tailor what the homepage surfaces. Narration: *"logically I'm part of that marketing Persona and…"*, i.e. the feed is filtered to the user's persona/purpose.

---

## 4. Different functionalities

Functionality demonstrated or described:

- **Personalized homepage** driven by **Persona** and **Purpose** — the same URL renders a different, role-relevant view per user ("a single workspace for all your data assets").
- **Global asset search** — instant, always-available search plus a "Discover all assets" entry point.
- **"Relevant to you" feed** — surfaces the user's **drafts** and **recently verified assets**, filterable by asset type (Views, Columns, Queries, Terms, Categories, Glossary, Dashboards) via counted chips.
- **Trust & verification** — green verified badges on assets; emphasis on popular / widely-used data and on asset ownership.
- **Recent announcements** — asset-level alerts and warnings (e.g. an **Airflow DAG failure** flagged as a Warning), each tied to an asset, an author, and a timestamp, warning of downstream impact.
- **Newly added resources / activity feed** — a social-style stream of **conversations** (@mentions, hashtags such as `#does-anyone-know`), newly added assets, and questions about data quality.
- **Metadata depth** — narration references bringing together *"business and technical metadata"* plus *"conversations,"* and organizing terms *"into categories"* and *"multi-level hierarchies"* (the Glossary).
- **Creation entry point** — the green **"+ New"** control in the header for creating new items.
- **Notifications & help** — header bell/gift icon (badge "5") and a **?** help control.
- **Left-rail modules** — Assets, Glossary, Insights, Workflows, Governance, Admin, Reporting, Support, Docs.

### Top bar (global header) controls
Left → right: **atlan** logo · page title **"Atlan"** · … · green **"+ New ▾"** button · **?** help · **gift/notifications** icon with red **"5"** badge · user chip **"👤 Matt Madden ▾"**.

### Right-hand column
- **Recent announcements** card — scrollable stack of alert cards. Example (amber = Warning): asset `beverages_order_customer ✓`, **⚠ Warning — "Airflow DAG failed!"**, body explaining the failed DAG and that *"data is not refreshed on this table and all downstream assets,"* authored by **rohan**, *2 hours ago*. A second (pink/red-tinted) card is partly visible below it, implying a severity color scheme.
- **Newly added resources** card — activity feed:
  - Atlan (bot), yesterday: *"@andrew what's the status of this data source with the failure? #does-anyone-know"* → `INSTACART_BEVERAGES_ORDER_CUSTOMER ✓`
  - *"Instacart Revenue and Usage Stati…"* — added by **ravi**, yesterday → `Spend Overview ✓`
  - Atlan (bot), yesterday: *"@andrew why is Sparkling Grapefruit #1 — that looks wrong"*

---

## 5. Modal windows

**No pop-up modal/dialog windows are opened in this video.** It is a narrated overview that remains on the homepage the entire time; the presenter never triggers a separate modal or overlay on screen.

The interactive elements present on the homepage that would normally launch a **modal, overlay, or side panel** (not shown opening here) are:

- **Global search** ("Search all your assets") — typically opens a full search experience/overlay.
- **"+ New ▾"** — a create dropdown/modal for new assets, announcements, etc.
- **Notifications** (bell/gift icon with badge) — opens a notifications panel.
- **Help (?)** — opens a help/support overlay.
- **Persona / Purpose** under "Personalise your experience" — opens personalization settings to change what the homepage surfaces.
- **Individual asset rows / announcement cards** — clicking through opens the asset detail (a new view rather than a modal).

*If you need the actual modal designs, they are not in this particular video — they would be shown in companion Atlan videos such as "Search and Discover Assets in Atlan."*

---

## Narration highlights (captured captions)

Approximate timeline of the voice-over (auto-captions, lightly cleaned):

- ~0:10 — "…[bring] it into a single workspace for all your data assets"
- ~0:20 — "…information about what's going on in my own little data [world]"
- ~1:11 — "…front and center, and at any point during this process I can instantly click and [search]…"
- ~1:47 — "…been working on a project and I noticed that, hey, there's a new [announcement]…"
- ~2:23 — "…logically I'm part of that marketing Persona, and…"
- ~2:58 — "…data everyone else is using — this gives me a…"
- ~3:34 — "…ownership. As I mentioned, the Atlan homepage is a…"
- ~4:10 — "…my data asset — things like business and technical metadata, to conversations…"
- ~4:46 — "…into categories, and create things like multi-level hierarchies that…"
- ~5:22 — "…the last few tabs here are…"
