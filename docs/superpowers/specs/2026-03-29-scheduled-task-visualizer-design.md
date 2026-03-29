# Scheduled Task Visualizer

> A first-class UI for viewing, understanding, and manually triggering dispatch tasks — modeled after Claude Cowork's scheduled tasks but focused on information density rather than conversational interaction.

## Core Concept

The prompt file IS the task. The schedule view is a richer lens that layers schedule metadata, run history, and actions on top of the file. Three entry points reach the same task view:

- `/{group}/schedule` — all tasks listed
- `/{group}/agents/{agent}` — that agent's tasks in their profile
- `/{group}/documents/view?path=...prompt.md` — banner linking to task view

## Data Model

Prompt files gain optional YAML frontmatter:

```yaml
---
description: Why this task exists and what it accomplishes
expected_output: What a successful run should produce
---

# Actual prompt content below...
```

- Both fields are optional — prompts without frontmatter work everywhere
- System prompts (prefixed `_`) are excluded from the schedule view
- Frontmatter is stripped before passing prompt content to agents during dispatch

Parsing reuses the same frontmatter extraction pattern used for observations/proposals.

Note: The dispatch runner passes `prompt_path` to `integration.run()`, which reads the file directly. Frontmatter stripping should happen in a shared helper that the runner calls before passing content, or integrations should be taught to ignore YAML frontmatter. The implementation plan should determine the cleanest approach.

## Routes

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/{group}/schedule` | Task list — all scheduled prompts with agent assignments |
| GET | `/{group}/schedule/{slug}` | Task detail — prompt content, schedule, history, run button |
| POST | `/{group}/schedule/{slug}/run` | Trigger run — accepts `agent` form field, spawns background |

## Schedule List Page

`/{group}/schedule` — card grid of scheduled prompts.

Each card shows:
- **Task name** — humanized prompt filename (e.g. `product-routine.md` → "Product Routine")
- **Description** — from frontmatter, if present
- **Agent pills** — who runs this task, with schedule (`@ 07:30`, `every 6h`)
- **Last run status** — timestamp + success/error indicator from most recent matching log
- **Condition badge** — amber pill if condition-triggered (e.g. "post-pipeline")

Sorting: tasks with recent errors float to top, then by next scheduled time.

Empty state: "No dispatch rules configured" with link to prompts page.

No filtering initially — lists should be short enough per group.

## Task Detail Page

`/{group}/schedule/{slug}` — the core view. Sections top to bottom:

### Header
Humanized task name + editable description field (saves to frontmatter).

### Schedule Table
One row per agent assignment:
- Agent name (linked to profile)
- Type (`@ 09:00` or `every 6h`)
- Condition badge if any
- "Run now" button per row

### Expected Output
Editable text area from frontmatter. Collapsible if empty.

### Prompt Content
Rendered markdown of the file body. "Edit" button switches to textarea (same pattern as document view).

### Run History
Table of past runs for this prompt across all assigned agents:
- Date, time, agent, status (success/error based on `.err` file size > 0), output size
- Each row links to existing log view at `/{group}/logs/view?path=...`
- Capped at last 20 runs

All edits (description, expected output, prompt body) save back to the same `.md` file.

## Agent Profile Enhancement

Replace current schedule pills with task cards. Each card shows:
- Task name (linked to `/{group}/schedule/{slug}`)
- Schedule (`@ 07:30` or `every 6h`)
- Last run — relative time + status dot (green/red)
- Condition badge if applicable

No scheduled tasks → "No scheduled tasks" text (same as current).

## Document Browser Integration

When `/{group}/documents/view?path=` points to a file inside `shared/prompts/` that has dispatch rules, render a banner at the top: "This file is a scheduled task — View as task" linking to the schedule detail view. No hard redirect — raw file editing remains accessible.

## Run Now Endpoint

`POST /{group}/schedule/{slug}/run`:
- Accepts form field `agent` — which agent to run
- Validates agent is assigned to this prompt in dispatch config
- Calls `_run_agent()` from `dispatch/run.py` in a background thread (same pattern as decision execution in app.py)
- Creates log files in today's date directory like normal dispatch
- Redirects back to task detail with `?triggered={agent}` query param
- Task detail shows a brief "Run triggered for {agent}" banner when param is present

## Sidebar Navigation

New "Schedule" item between "Prompts" and "Logs" in the sidebar.

## New Files

- `agency/templates/schedule.html` — task list page
- `agency/templates/schedule_detail.html` — task detail page

## Modified Files

- `agency/app.py` — new routes, prompt frontmatter parsing, run-history helper, run-now endpoint
- `agency/templates/base.html` — sidebar nav item
- `agency/templates/agent_profile.html` — replace schedule pills with task cards
- `agency/templates/document_view.html` — banner for scheduled prompt files
- `agency/dispatch/run.py` — extract `_run_agent()` for reuse (may need to make it importable)

## Helpers Needed

- `parse_prompt_frontmatter(path)` — extract description/expected_output from prompt file
- `humanize_prompt_name(filename)` — `product-routine.md` → "Product Routine"
- `collect_task_runs(group, prompt_stem)` — scan log dirs for matching files, return sorted list with status
- `build_schedule_cards(group)` — combine dispatch config + last run status for list page
