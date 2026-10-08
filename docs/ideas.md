# Ideas

Where this project could go after Demo Day (2026-10-26). Nothing here is planned or approved yet.
Each idea says what it needs and what it would change for privacy, so it can be weighed before
anyone builds it. Recorded 2026-10-08 (TR-356).

When an idea is picked up, give it a Jira ticket and an OpenSpec change, then link both here.

| # | Idea | Needs first | Privacy impact | Status |
|---|---|---|---|---|
| 1 | [Link usage to results](#1-link-usage-to-results) | Jira and GitHub access | New data joined to named devs | Idea |
| 2 | [Cost per workflow](#2-cost-per-workflow) | Opt-in terms updated | New field collected | Idea |
| 3 | [Skill quality](#3-skill-quality) | Collector reads more of the transcript | Still metadata only | Idea |
| 4 | [Nudges for outdated versions](#4-nudges-for-outdated-versions) | A Slack or Teams channel | None new | Idea |
| 5 | [A service for clients](#5-a-service-for-clients) | [Decoupling](#decoupling-from-creai-axis) | One privacy setup per client | Idea |
| 6 | [Avoid looking like surveillance](#6-avoid-looking-like-surveillance) | — | Reduces it | Principle for every idea |
| 7 | [Role packs beyond devs](#role-packs-beyond-devs) | A data source for claude.ai | Depends on the source | Idea |

## Telemetry ideas

### 1. Link usage to results

Join skill events with Jira and GitHub data: cycle time per ticket, review rounds per PR, reopened
tickets. That shows whether a skill helps, for example whether branches that used `/audit-pr` need
fewer review rounds. It turns "people use it" into "it helps".

- **Data:** the branch already carries the Jira key (`feature/ORG-123-…`), so events can be joined to
  tickets and PRs without collecting anything new on the dev's machine.
- **Needs:** read access to Jira and GitHub, plus a job that copies the few fields used.
- **Watch out:** correlation is not cause. Devs who use more skills may simply be more experienced.
  Compare like with like (same repo, similar ticket size) and say so on the dashboard.

### 2. Cost per workflow

Claude Code transcripts record token counts on every reply (`usage`: input, output, cache reads and
writes). Summing them per session, ticket or skill would show cost per workflow, and which skills are
expensive for what they deliver.

- **Needs:** the collector reads the `usage` fields; the event contract gets new optional fields.
- **Privacy:** this collects a new kind of data. Update the opt-in text, and ask devs who already
  opted in to opt in again, before the collector sends it.
- **Watch out:** token counts are not money. Prices differ by model and plan, so show tokens, or
  estimates clearly labelled as such.

### 3. Skill quality

Spot skills that people start and drop, run again right away, or follow with a manual fix. That tells
the framework team which skills to improve first.

- **Data:** order and timing of skill calls within a session, which the transcript already has. No
  prompts or code are needed.
- **Watch out:** "ran it again" can also mean "it worked, and I used it twice". Validate the signal
  with a few devs before putting it on the dashboard.

### 4. Nudges for outdated versions

The dashboard already shows each dev's installed plugin version from their latest event. A weekly
message in Slack or Teams could remind anyone who is behind, with the update command.

- **Needs:** a channel webhook and a scheduled job (pg_cron or a small function).
- **Watch out:** send it to the person or as a team summary. Don't publicly call out individuals.

### 5. A service for clients

Measure adoption of the Claude Code setup creai rolls out at clients, starting with Agrizar. This fits
creai's AI-transformation work: "we roll out your agentic framework and show you it's being used".

- **Needs:** the [decoupling](#decoupling-from-creai-axis) work below, then one deployment per client,
  or one shared deployment with an organization per client.
- **Privacy:** each client is its own privacy setup: data owner, retention period, legal notice, and
  hosting in the client's cloud where they require it.

### 6. Avoid looking like surveillance

A principle for every idea above, not a feature. If people feel watched, they won't opt in, and
Product and HR people least of all.

- Keep it opt-in, as it is today.
- Show team-level views by default. Per-person views only where the person agreed to them.
- Let each person see their own data first, before anyone else does.
- Never use these numbers in performance reviews. Say so in the opt-in text.

## Decoupling from creai-axis

Most of the system is already generic: the ingest logic (`handler.ts`) and its `Store` interface
don't mention creai, and the opt-in, metadata-only model fits any company. The creai-specific parts:

| Where | Tied to creai | Make it |
|---|---|---|
| `usage-collector.py` (`MARKETPLACE`, `FALLBACK_PLUGINS`) | `creai-axis` and its plugins | A list of marketplaces to track, set at opt-in |
| `email` check, `is_creai_reader()`, `axis_admin.py`, dashboard sign-in | `@creai.mx` | A table of allowed domains, or one organization per customer |
| `axis_usage_pipeline_steps`; "reached PR" means `creai-create-pr` | creai's 7 steps | Named workflows stored as data, each with its own "done" step |
| Dashboard copy | "Adopción de creai-axis", Spanish only | Product name from config; Spanish and English |
| Names | `creai-telemetry`, `axis_usage_*`, `~/.config/creai-axis` | A neutral product name |

Suggested order: make each row configurable with creai's values as defaults (nothing changes for
us), then publish the event format as a versioned spec (`v1`), so other storage backends (Postgres or
RDS, PostHog, BigQuery) are new `Store` implementations.

## Role packs beyond devs

creai-axis is built for developers. A sibling set of skill packs for other roles would share one core
(company conventions, Jira and Confluence, document standards) and add role plugins on top, the way
`creai-backend` and `creai-frontend` sit on `creai-common`. Separate frameworks would drift apart.

| Order | Pack | Example skills | Why this position |
|---|---|---|---|
| 1 | Product (PM/PO) | requirements doc → epics and stories, refinement, acceptance criteria, sprint review notes, release notes | Feeds straight into the dev pipeline. Similar skills already exist in the Agrizar project (`prd-to-stories`, `agrizar-context`). |
| 2 | Solutions | discovery → diagnostic → proposal → estimate → statement of work; RFP answers | Repetitive, high-value documents with real examples to learn from (the Agrizar diagnostic and proposal). |
| 3 | HR / People | job descriptions, interview guides, onboarding plans, policy Q&A | Personal data and bias risk: drafting only, people decide, privacy sign-off first. |

With a product pack, the funnel can run from idea to merged PR instead of from task to PR, and the
telemetry measures how work flows across roles.

**Open question before promising dashboards for these roles:** they mostly work in claude.ai or
Claude Desktop, not Claude Code. Skills run there too, but this collector only reads Claude Code's
local transcripts, so it can't see that usage. Check what usage data Claude Team or Enterprise
analytics expose per skill before planning telemetry for non-dev roles.
