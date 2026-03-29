# Scheduled Task Visualizer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a first-class schedule UI that treats dispatch prompt files as tasks — with schedule metadata, run history, manual trigger, and three entry points (schedule page, agent profile, document browser).

**Architecture:** New `schedule` routes + templates layered on existing dispatch config and log files. A `collect_task_runs()` helper scans log directories for prompt-matching files. A `run_task_now()` endpoint spawns agent runs via `BackgroundTasks`. Agent profile schedule pills are replaced with richer task cards. Document browser gains a banner for scheduled prompts.

**Tech Stack:** FastAPI, Jinja2, Tailwind CSS (CDN), existing `parse_frontmatter()` + `_run_agent()` from dispatch.

---

### Task 1: Helper functions — `parse_prompt_frontmatter`, `humanize_prompt_name`, `collect_task_runs`

**Files:**
- Modify: `agency/app.py` (add helpers after `collect_logs()` at ~line 824)
- Create: `tests/test_schedule.py`

- [ ] **Step 1: Write failing tests for helpers**

Create `tests/test_schedule.py`:

```python
"""Tests for scheduled task visualizer helpers."""
import pytest
from pathlib import Path
from agency.app import parse_prompt_frontmatter, humanize_prompt_name, collect_task_runs


class TestParsePromptFrontmatter:
    def test_extracts_description_and_expected_output(self, tmp_path):
        p = tmp_path / "task.md"
        p.write_text("---\ndescription: Why this runs\nexpected_output: A report\n---\n# Do the thing\n")
        meta, body = parse_prompt_frontmatter(p)
        assert meta["description"] == "Why this runs"
        assert meta["expected_output"] == "A report"
        assert body.strip() == "# Do the thing"

    def test_no_frontmatter_returns_empty_meta(self, tmp_path):
        p = tmp_path / "task.md"
        p.write_text("# Just a prompt\nDo stuff.\n")
        meta, body = parse_prompt_frontmatter(p)
        assert meta == {}
        assert "Just a prompt" in body

    def test_missing_file_returns_empty(self, tmp_path):
        p = tmp_path / "nonexistent.md"
        meta, body = parse_prompt_frontmatter(p)
        assert meta == {}
        assert body == ""


class TestHumanizePromptName:
    def test_basic_conversion(self):
        assert humanize_prompt_name("product-routine.md") == "Product Routine"

    def test_strips_md_suffix(self):
        assert humanize_prompt_name("qa-morning-check.md") == "Qa Morning Check"

    def test_no_suffix(self):
        assert humanize_prompt_name("daily-report") == "Daily Report"

    def test_underscores(self):
        assert humanize_prompt_name("my_task_name.md") == "My Task Name"


class TestCollectTaskRuns:
    def test_finds_matching_logs(self, tmp_path):
        logs_dir = tmp_path / "shared" / "logs"
        day = logs_dir / "2026-03-29"
        day.mkdir(parents=True)
        (day / "product-routine-070001.out").write_text("output")
        (day / "product-routine-070001.err").write_text("")
        (day / "qa-routine-080000.out").write_text("other")
        (day / "qa-routine-080000.err").write_text("err")

        g = {"shared": tmp_path / "shared"}
        runs = collect_task_runs(g, "routine")
        assert len(runs) == 2

    def test_detects_error_status(self, tmp_path):
        logs_dir = tmp_path / "shared" / "logs"
        day = logs_dir / "2026-03-29"
        day.mkdir(parents=True)
        (day / "product-routine-070001.out").write_text("output")
        (day / "product-routine-070001.err").write_text("something went wrong")

        g = {"shared": tmp_path / "shared"}
        runs = collect_task_runs(g, "routine")
        error_run = [r for r in runs if r["agent"] == "product"][0]
        assert error_run["status"] == "error"

    def test_success_status_when_err_empty(self, tmp_path):
        logs_dir = tmp_path / "shared" / "logs"
        day = logs_dir / "2026-03-29"
        day.mkdir(parents=True)
        (day / "product-routine-070001.out").write_text("output")
        (day / "product-routine-070001.err").write_text("")

        g = {"shared": tmp_path / "shared"}
        runs = collect_task_runs(g, "routine")
        assert runs[0]["status"] == "success"

    def test_returns_sorted_newest_first(self, tmp_path):
        logs_dir = tmp_path / "shared" / "logs"
        day1 = logs_dir / "2026-03-28"
        day1.mkdir(parents=True)
        (day1 / "product-routine-070001.out").write_text("old")
        (day1 / "product-routine-070001.err").write_text("")
        day2 = logs_dir / "2026-03-29"
        day2.mkdir(parents=True)
        (day2 / "product-routine-080000.out").write_text("new")
        (day2 / "product-routine-080000.err").write_text("")

        g = {"shared": tmp_path / "shared"}
        runs = collect_task_runs(g, "routine")
        assert runs[0]["date"] == "2026-03-29"

    def test_caps_at_20_runs(self, tmp_path):
        logs_dir = tmp_path / "shared" / "logs"
        for i in range(25):
            day = logs_dir / f"2026-03-{i+1:02d}"
            day.mkdir(parents=True)
            (day / f"product-routine-{i:06d}.out").write_text("x")
            (day / f"product-routine-{i:06d}.err").write_text("")

        g = {"shared": tmp_path / "shared"}
        runs = collect_task_runs(g, "routine")
        assert len(runs) <= 20

    def test_no_logs_dir_returns_empty(self, tmp_path):
        g = {"shared": tmp_path / "shared"}
        runs = collect_task_runs(g, "routine")
        assert runs == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_schedule.py -v`
Expected: ImportError — functions don't exist yet.

- [ ] **Step 3: Implement the three helpers in app.py**

Add after `collect_logs()` (around line 824) in `agency/app.py`:

```python
def parse_prompt_frontmatter(path: Path) -> tuple[dict, str]:
    """Parse YAML frontmatter from a prompt file. Returns (meta, body)."""
    if not path.exists():
        return {}, ""
    text = path.read_text()
    return parse_frontmatter(text)


def humanize_prompt_name(filename: str) -> str:
    """Convert prompt filename to human-readable title."""
    name = filename.removesuffix(".md")
    return name.replace("-", " ").replace("_", " ").title()


def collect_task_runs(g: dict, prompt_stem: str, limit: int = 20) -> list[dict]:
    """Collect log entries matching a prompt stem, newest first."""
    logs_dir = g["shared"] / "logs"
    if not logs_dir.exists():
        return []
    runs = []
    for date_dir in sorted(logs_dir.iterdir(), reverse=True):
        if not date_dir.is_dir():
            continue
        for f in sorted(date_dir.iterdir(), reverse=True):
            if not f.name.endswith(".out"):
                continue
            # Pattern: {agent}-{stem}-{HHMMSS}.out
            # Match files where stem appears in the name
            parts = f.stem.rsplit("-", 1)  # split off timestamp
            if len(parts) != 2:
                continue
            prefix = parts[0]  # "agent-stem"
            ts = parts[1]      # "070001"
            # Find the prompt stem in the prefix
            if f"-{prompt_stem}-" not in f"-{prefix}-" and not prefix.endswith(f"-{prompt_stem}"):
                # Try: prefix = "agent-promptstem", we want "promptstem" == prompt_stem
                idx = prefix.find(f"-{prompt_stem}")
                if idx == -1:
                    continue
                agent = prefix[:idx]
            else:
                idx = prefix.find(f"-{prompt_stem}")
                if idx == -1:
                    continue
                agent = prefix[:idx]

            err_file = f.with_suffix(".err")
            err_size = err_file.stat().st_size if err_file.exists() else 0

            runs.append({
                "date": date_dir.name,
                "time": f"{ts[:2]}:{ts[2:4]}:{ts[4:6]}",
                "agent": agent,
                "status": "error" if err_size > 0 else "success",
                "out_size": f.stat().st_size,
                "out_path": str(f),
                "err_path": str(err_file) if err_file.exists() else "",
            })
            if len(runs) >= limit:
                return runs
    return runs
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_schedule.py -v`
Expected: All pass.

- [ ] **Step 5: Commit**

```bash
git add agency/app.py tests/test_schedule.py
git commit -m "feat(schedule): add helper functions for task visualizer"
```

---

### Task 2: `build_schedule_cards()` helper

**Files:**
- Modify: `agency/app.py` (add after `collect_task_runs()`)
- Modify: `tests/test_schedule.py` (add test class)

- [ ] **Step 1: Write failing tests**

Add to `tests/test_schedule.py`:

```python
from agency.app import build_schedule_cards


class TestBuildScheduleCards:
    def test_builds_cards_from_dispatch_config(self, tmp_path):
        prompts_dir = tmp_path / "shared" / "prompts"
        prompts_dir.mkdir(parents=True)
        (prompts_dir / "routine.md").write_text("---\ndescription: Daily check\n---\n# Run checks\n")

        g = {
            "key": "test",
            "shared": tmp_path / "shared",
        }
        dispatch_cfg = {
            "enabled": True,
            "agents": {
                "product": [{"prompt": "routine.md", "at": "07:30"}],
                "qa": [{"prompt": "routine.md", "every": "6h"}],
            },
        }
        cards = build_schedule_cards(g, dispatch_cfg)
        assert len(cards) == 1
        card = cards[0]
        assert card["slug"] == "routine"
        assert card["name"] == "Routine"
        assert card["description"] == "Daily check"
        assert len(card["assignments"]) == 2

    def test_multiple_prompts_become_multiple_cards(self, tmp_path):
        prompts_dir = tmp_path / "shared" / "prompts"
        prompts_dir.mkdir(parents=True)
        (prompts_dir / "morning.md").write_text("# Morning\n")
        (prompts_dir / "cleanup.md").write_text("# Cleanup\n")

        g = {
            "key": "test",
            "shared": tmp_path / "shared",
        }
        dispatch_cfg = {
            "enabled": True,
            "agents": {
                "product": [
                    {"prompt": "morning.md", "at": "07:30"},
                    {"prompt": "cleanup.md", "at": "23:00"},
                ],
            },
        }
        cards = build_schedule_cards(g, dispatch_cfg)
        assert len(cards) == 2
        slugs = {c["slug"] for c in cards}
        assert slugs == {"morning", "cleanup"}

    def test_excludes_system_prompts(self, tmp_path):
        prompts_dir = tmp_path / "shared" / "prompts"
        prompts_dir.mkdir(parents=True)
        (prompts_dir / "_system.md").write_text("# System\n")
        (prompts_dir / "routine.md").write_text("# Routine\n")

        g = {
            "key": "test",
            "shared": tmp_path / "shared",
        }
        dispatch_cfg = {
            "enabled": True,
            "agents": {
                "product": [
                    {"prompt": "_system.md", "at": "06:00"},
                    {"prompt": "routine.md", "at": "07:30"},
                ],
            },
        }
        cards = build_schedule_cards(g, dispatch_cfg)
        assert len(cards) == 1
        assert cards[0]["slug"] == "routine"

    def test_empty_dispatch_returns_empty(self, tmp_path):
        g = {"key": "test", "shared": tmp_path / "shared"}
        cards = build_schedule_cards(g, {})
        assert cards == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_schedule.py::TestBuildScheduleCards -v`
Expected: ImportError.

- [ ] **Step 3: Implement `build_schedule_cards()`**

Add after `collect_task_runs()` in `agency/app.py`:

```python
def build_schedule_cards(g: dict, dispatch_cfg: dict) -> list[dict]:
    """Build task cards by inverting agent-centric dispatch config to prompt-centric view."""
    agents_cfg = dispatch_cfg.get("agents", {})
    prompts_dir = g["shared"] / "prompts"

    # Invert: agent→rules into prompt→assignments
    prompt_map: dict[str, list[dict]] = {}
    for agent_name, rules in agents_cfg.items():
        if not isinstance(rules, list):
            continue
        for rule in rules:
            prompt_file = rule.get("prompt", "")
            if not prompt_file or prompt_file.startswith("_"):
                continue
            prompt_map.setdefault(prompt_file, []).append({
                "agent": agent_name,
                "at": rule.get("at"),
                "every": rule.get("every"),
                "condition": rule.get("condition"),
            })

    cards = []
    for prompt_file, assignments in prompt_map.items():
        slug = prompt_file.removesuffix(".md")
        prompt_path = prompts_dir / prompt_file
        meta, _ = parse_prompt_frontmatter(prompt_path)

        # Get last run across all agents for this prompt
        all_runs = collect_task_runs(g, slug, limit=1)
        last_run = all_runs[0] if all_runs else None

        cards.append({
            "slug": slug,
            "name": humanize_prompt_name(prompt_file),
            "filename": prompt_file,
            "description": meta.get("description", ""),
            "expected_output": meta.get("expected_output", ""),
            "assignments": assignments,
            "last_run": last_run,
        })

    # Sort: errors first, then by slug
    cards.sort(key=lambda c: (
        0 if c["last_run"] and c["last_run"]["status"] == "error" else 1,
        c["slug"],
    ))
    return cards
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_schedule.py -v`
Expected: All pass.

- [ ] **Step 5: Commit**

```bash
git add agency/app.py tests/test_schedule.py
git commit -m "feat(schedule): add build_schedule_cards helper"
```

---

### Task 3: Schedule list page — route + template

**Files:**
- Modify: `agency/app.py` (add route after prompts routes, around line 2940)
- Create: `agency/templates/schedule.html`

- [ ] **Step 1: Add the schedule list route to app.py**

Add after the prompts routes (around line 2940) in `agency/app.py`:

```python
# ── Schedule (task visualizer) ──────────────────────────────────────────
@app.get("/{group}/schedule", response_class=HTMLResponse)
async def schedule_list(request: Request, group: str):
    """Task-oriented view of dispatch schedule."""
    g = get_group(group)
    group_cfg = GROUPS.get(g["key"], {})
    dispatch_cfg = group_cfg.get("dispatch", {})
    cards = build_schedule_cards(g, dispatch_cfg)
    return templates.TemplateResponse("schedule.html", {
        "request": request,
        **group_context(g),
        "active": "schedule",
        "cards": cards,
        "dispatch_enabled": dispatch_cfg.get("enabled", False),
    })
```

- [ ] **Step 2: Create the schedule list template**

Create `agency/templates/schedule.html`:

```html
{% extends "base.html" %}
{% block title %}Schedule — {{ group_name }}{% endblock %}
{% block content %}
<div class="max-w-5xl mx-auto">
  <div class="flex items-center justify-between mb-6">
    <h1 class="text-2xl font-bold text-gray-900">Schedule</h1>
    {% if not dispatch_enabled %}
    <span class="inline-flex items-center gap-1.5 px-3 py-1 rounded-full text-xs font-medium bg-gray-100 text-gray-500">
      <span class="w-1.5 h-1.5 rounded-full bg-gray-300"></span>
      Dispatch disabled
    </span>
    {% endif %}
  </div>

  {% if cards %}
  <div class="grid gap-4">
    {% for card in cards %}
    <a href="/{{ group }}/schedule/{{ card.slug }}" class="block bg-white rounded-xl border border-gray-200 p-5 hover:border-indigo-300 hover:shadow-sm transition-all">
      <div class="flex items-start justify-between gap-4">
        <div class="min-w-0 flex-1">
          <h3 class="text-base font-semibold text-gray-900">{{ card.name }}</h3>
          {% if card.description %}
          <p class="text-sm text-gray-500 mt-0.5">{{ card.description }}</p>
          {% endif %}

          <div class="flex flex-wrap items-center gap-1.5 mt-3">
            {% for a in card.assignments %}
            <span class="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-mono
              {% if a.condition %}bg-amber-50 text-amber-700 border border-amber-200{% elif dispatch_enabled %}bg-indigo-50 text-indigo-700{% else %}bg-gray-100 text-gray-500{% endif %}">
              {{ a.agent }}
              {% if a.condition %}
              · {{ a.condition }}
              {% elif a.at %}
              @ {{ a.at }}
              {% elif a.every %}
              every {{ a.every }}
              {% endif %}
            </span>
            {% endfor %}
          </div>
        </div>

        {% if card.last_run %}
        <div class="shrink-0 text-right">
          <span class="inline-flex items-center gap-1.5 text-xs {% if card.last_run.status == 'error' %}text-red-600{% else %}text-gray-500{% endif %}">
            <span class="w-1.5 h-1.5 rounded-full {% if card.last_run.status == 'error' %}bg-red-500{% else %}bg-emerald-400{% endif %}"></span>
            {{ card.last_run.date }} {{ card.last_run.time }}
          </span>
          <div class="text-xs text-gray-400 mt-0.5">{{ card.last_run.agent }}</div>
        </div>
        {% else %}
        <div class="shrink-0">
          <span class="text-xs text-gray-300">No runs yet</span>
        </div>
        {% endif %}
      </div>
    </a>
    {% endfor %}
  </div>
  {% else %}
  <div class="text-center py-16 text-gray-400">
    <p class="text-lg font-medium">No dispatch rules configured</p>
    <p class="mt-1 text-sm">Set up agent schedules on the <a href="/{{ group }}/prompts" class="text-indigo-600 hover:underline">Prompts</a> page.</p>
  </div>
  {% endif %}
</div>
{% endblock %}
```

- [ ] **Step 3: Verify the route works**

Run: `.venv/bin/python3 -m agency.app &`
Then: `curl -s http://127.0.0.1:8500/{your-default-group}/schedule | head -20`
Expected: HTML response with "Schedule" heading. Kill the server after.

- [ ] **Step 4: Commit**

```bash
git add agency/app.py agency/templates/schedule.html
git commit -m "feat(schedule): add schedule list page with task cards"
```

---

### Task 4: Schedule detail page — route + template

**Files:**
- Modify: `agency/app.py` (add route after schedule_list)
- Create: `agency/templates/schedule_detail.html`

- [ ] **Step 1: Add the schedule detail route to app.py**

Add after `schedule_list` in `agency/app.py`:

```python
@app.get("/{group}/schedule/{slug}", response_class=HTMLResponse)
async def schedule_detail(request: Request, group: str, slug: str):
    """Task detail view — schedule, prompt content, run history."""
    g = get_group(group)
    group_cfg = GROUPS.get(g["key"], {})
    dispatch_cfg = group_cfg.get("dispatch", {})

    prompt_file = f"{slug}.md"
    prompt_path = g["shared"] / "prompts" / prompt_file
    if not prompt_path.exists():
        raise HTTPException(404, "Prompt not found")

    meta, body = parse_prompt_frontmatter(prompt_path)
    content_html = render_md(body)

    # Build assignments for this prompt
    assignments = []
    for agent_name, rules in dispatch_cfg.get("agents", {}).items():
        if not isinstance(rules, list):
            continue
        for rule in rules:
            if rule.get("prompt") == prompt_file:
                assignments.append({
                    "agent": agent_name,
                    "at": rule.get("at"),
                    "every": rule.get("every"),
                    "condition": rule.get("condition"),
                })

    runs = collect_task_runs(g, slug, limit=20)
    triggered = request.query_params.get("triggered", "")

    return templates.TemplateResponse("schedule_detail.html", {
        "request": request,
        **group_context(g),
        "active": "schedule",
        "slug": slug,
        "name": humanize_prompt_name(prompt_file),
        "filename": prompt_file,
        "description": meta.get("description", ""),
        "expected_output": meta.get("expected_output", ""),
        "body_raw": body,
        "content_html": content_html,
        "filepath": str(prompt_path),
        "assignments": assignments,
        "runs": runs,
        "dispatch_enabled": dispatch_cfg.get("enabled", False),
        "triggered": triggered,
    })
```

- [ ] **Step 2: Create the schedule detail template**

Create `agency/templates/schedule_detail.html`:

```html
{% extends "base.html" %}
{% block title %}{{ name }} — Schedule — {{ group_name }}{% endblock %}
{% block content %}
<div class="max-w-4xl mx-auto">

  <!-- Triggered banner -->
  {% if triggered %}
  <div class="mb-4 px-4 py-3 rounded-lg bg-emerald-50 border border-emerald-200 text-emerald-800 text-sm">
    Run triggered for <strong>{{ triggered }}</strong>. Check <a href="/{{ group }}/logs" class="underline">Logs</a> for output.
  </div>
  {% endif %}

  <!-- Breadcrumb -->
  <div class="text-sm text-gray-400 mb-4">
    <a href="/{{ group }}/schedule" class="hover:text-gray-600">Schedule</a>
    <span class="mx-1">→</span>
    <span class="text-gray-600">{{ name }}</span>
  </div>

  <!-- Header -->
  <div class="mb-6">
    <h1 class="text-2xl font-bold text-gray-900">{{ name }}</h1>
    {% if description %}
    <p class="text-gray-500 mt-1">{{ description }}</p>
    {% endif %}
  </div>

  <!-- Schedule Table -->
  <section class="mb-8">
    <h2 class="text-sm font-semibold text-gray-700 uppercase tracking-wide mb-3">Schedule</h2>
    <div class="bg-white rounded-xl border border-gray-200 divide-y divide-gray-100">
      {% for a in assignments %}
      <div class="flex items-center justify-between px-4 py-3">
        <div class="flex items-center gap-3">
          <a href="/{{ group }}/agents/{{ a.agent }}" class="text-sm font-medium text-indigo-600 hover:underline">{{ a.agent }}</a>
          {% if a.condition %}
          <span class="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs bg-amber-50 text-amber-700 border border-amber-200">{{ a.condition }}</span>
          {% elif a.at %}
          <span class="text-sm text-gray-500 font-mono">@ {{ a.at }}</span>
          {% elif a.every %}
          <span class="text-sm text-gray-500 font-mono">every {{ a.every }}</span>
          {% endif %}
        </div>
        {% if not a.condition %}
        <form method="POST" action="/{{ group }}/schedule/{{ slug }}/run">
          <input type="hidden" name="agent" value="{{ a.agent }}">
          <button type="submit" class="px-3 py-1.5 text-xs font-medium rounded-lg bg-indigo-600 text-white hover:bg-indigo-700 transition-colors">
            Run now
          </button>
        </form>
        {% endif %}
      </div>
      {% endfor %}
      {% if not assignments %}
      <div class="px-4 py-6 text-center text-sm text-gray-400">No agents assigned to this task.</div>
      {% endif %}
    </div>
  </section>

  <!-- Expected Output -->
  {% if expected_output %}
  <section class="mb-8">
    <h2 class="text-sm font-semibold text-gray-700 uppercase tracking-wide mb-3">Expected Output</h2>
    <div class="bg-gray-50 rounded-xl border border-gray-200 px-4 py-3 text-sm text-gray-600">
      {{ expected_output }}
    </div>
  </section>
  {% endif %}

  <!-- Prompt Content -->
  <section class="mb-8">
    <div class="flex items-center justify-between mb-3">
      <h2 class="text-sm font-semibold text-gray-700 uppercase tracking-wide">Prompt</h2>
      <button onclick="document.getElementById('prompt-editor').classList.toggle('hidden'); document.getElementById('prompt-rendered').classList.toggle('hidden')"
              class="text-xs text-indigo-600 hover:underline">Edit</button>
    </div>
    <div id="prompt-rendered" class="bg-white rounded-xl border border-gray-200 px-5 py-4 prose prose-sm max-w-none">
      {{ content_html | safe }}
    </div>
    <div id="prompt-editor" class="hidden">
      <form method="POST" action="/{{ group }}/documents/save">
        <input type="hidden" name="path" value="{{ filepath }}">
        <textarea name="content" rows="20" class="w-full font-mono text-sm border border-gray-300 rounded-lg p-3 bg-gray-50">{{ body_raw }}</textarea>
        <div class="mt-3 flex justify-end gap-2">
          <button type="button" onclick="document.getElementById('prompt-editor').classList.add('hidden'); document.getElementById('prompt-rendered').classList.remove('hidden')"
                  class="px-4 py-2 text-sm text-gray-600 rounded-lg border border-gray-300 hover:bg-gray-50">Cancel</button>
          <button type="submit" class="px-4 py-2 bg-indigo-600 text-white text-sm rounded-lg hover:bg-indigo-700">Save</button>
        </div>
      </form>
    </div>
  </section>

  <!-- Run History -->
  <section class="mb-8">
    <h2 class="text-sm font-semibold text-gray-700 uppercase tracking-wide mb-3">Run History</h2>
    {% if runs %}
    <div class="bg-white rounded-xl border border-gray-200 overflow-hidden">
      <table class="w-full text-sm">
        <thead class="bg-gray-50 border-b border-gray-200">
          <tr>
            <th class="px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase">Date</th>
            <th class="px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase">Time</th>
            <th class="px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase">Agent</th>
            <th class="px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase">Status</th>
            <th class="px-4 py-2 text-right text-xs font-medium text-gray-500 uppercase">Size</th>
          </tr>
        </thead>
        <tbody class="divide-y divide-gray-100">
          {% for run in runs %}
          <tr class="hover:bg-gray-50">
            <td class="px-4 py-2 text-gray-600">{{ run.date }}</td>
            <td class="px-4 py-2 text-gray-600 font-mono">{{ run.time }}</td>
            <td class="px-4 py-2">
              <a href="/{{ group }}/agents/{{ run.agent }}" class="text-indigo-600 hover:underline">{{ run.agent }}</a>
            </td>
            <td class="px-4 py-2">
              <span class="inline-flex items-center gap-1.5">
                <span class="w-1.5 h-1.5 rounded-full {% if run.status == 'error' %}bg-red-500{% else %}bg-emerald-400{% endif %}"></span>
                {% if run.status == 'error' %}Error{% else %}Success{% endif %}
              </span>
            </td>
            <td class="px-4 py-2 text-right">
              <a href="/{{ group }}/logs/view?path={{ run.out_path | urlencode }}" class="text-indigo-600 hover:underline">
                {{ "%.1f" | format(run.out_size / 1024) }}KB
              </a>
            </td>
          </tr>
          {% endfor %}
        </tbody>
      </table>
    </div>
    {% else %}
    <div class="bg-white rounded-xl border border-gray-200 px-4 py-8 text-center text-sm text-gray-400">
      No runs yet.
    </div>
    {% endif %}
  </section>

</div>
{% endblock %}
```

- [ ] **Step 3: Verify the route works**

Run: `.venv/bin/python3 -m agency.app &`
Then: `curl -s http://127.0.0.1:8500/{your-default-group}/schedule/{a-known-prompt-slug} | head -20`
Expected: HTML response with task detail view. Kill the server after.

- [ ] **Step 4: Commit**

```bash
git add agency/app.py agency/templates/schedule_detail.html
git commit -m "feat(schedule): add task detail page with prompt content and run history"
```

---

### Task 5: Run Now endpoint

**Files:**
- Modify: `agency/app.py` (add route after schedule_detail)
- Modify: `tests/test_schedule.py` (add test)

- [ ] **Step 1: Write failing test**

Add to `tests/test_schedule.py`:

```python
from unittest.mock import patch, MagicMock
from agency.app import app
from fastapi.testclient import TestClient


class TestRunNowEndpoint:
    def test_rejects_unassigned_agent(self):
        """Run now should 400 if agent isn't assigned to this prompt."""
        client = TestClient(app)
        with patch("agency.app.get_group") as mock_gg, \
             patch("agency.app.GROUPS", {"test": {"dispatch": {"agents": {}}}}):
            mock_gg.return_value = {
                "key": "test",
                "path": Path("/tmp/test"),
                "shared": Path("/tmp/test/shared"),
                "agents": ["product"],
                "agents_full": [{"name": "product", "integration": "claude-code"}],
            }
            resp = client.post("/test/schedule/routine/run", data={"agent": "product"}, follow_redirects=False)
            assert resp.status_code == 400
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_schedule.py::TestRunNowEndpoint -v`
Expected: Fails — route doesn't exist.

- [ ] **Step 3: Implement the run-now endpoint**

Add after `schedule_detail` in `agency/app.py`:

```python
@app.post("/{group}/schedule/{slug}/run", response_class=HTMLResponse)
async def schedule_run_now(request: Request, group: str, slug: str, background_tasks: BackgroundTasks):
    """Manually trigger an agent run for a scheduled task."""
    g = get_group(group)
    form = await request.form()
    agent_name = form.get("agent", "")
    if not agent_name:
        raise HTTPException(400, "Agent name required")

    group_cfg = GROUPS.get(g["key"], {})
    dispatch_cfg = group_cfg.get("dispatch", {})
    prompt_file = f"{slug}.md"

    # Validate agent is assigned to this prompt
    assigned = False
    for ag, rules in dispatch_cfg.get("agents", {}).items():
        if ag != agent_name or not isinstance(rules, list):
            continue
        for rule in rules:
            if rule.get("prompt") == prompt_file:
                assigned = True
                break

    if not assigned:
        raise HTTPException(400, f"Agent '{agent_name}' is not assigned to '{prompt_file}'")

    prompt_path = g["shared"] / "prompts" / prompt_file
    if not prompt_path.exists():
        raise HTTPException(404, "Prompt file not found")

    # Resolve agent config
    agents_full = g.get("agents_full", g.get("_agents_normalized", []))
    agent_config = {"name": agent_name, "integration": "claude-code"}
    for a in agents_full:
        if a["name"] == agent_name:
            agent_config = a
            break

    # Resolve timeout
    agent_dispatch = dispatch_cfg.get("agents", {}).get(agent_name, {})
    if isinstance(agent_dispatch, dict):
        timeout = agent_dispatch.get("timeout", dispatch_cfg.get("timeout", 1800))
    else:
        timeout = dispatch_cfg.get("timeout", 1800)

    log_dir = g["shared"] / "logs" / datetime.now().strftime("%Y-%m-%d")
    log_dir.mkdir(parents=True, exist_ok=True)

    agent_dir = get_agent_dir(g, agent_name)

    from agency.dispatch.run import _run_agent
    background_tasks.add_task(
        _run_agent,
        Path(g["path"]), agent_name, prompt_file,
        timeout, log_dir, agent_config,
        agent_dir=agent_dir,
    )

    return RedirectResponse(
        f"/{group}/schedule/{slug}?triggered={agent_name}",
        status_code=303,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_schedule.py::TestRunNowEndpoint -v`
Expected: Pass.

- [ ] **Step 5: Commit**

```bash
git add agency/app.py tests/test_schedule.py
git commit -m "feat(schedule): add run-now endpoint for manual task triggers"
```

---

### Task 6: Sidebar nav + document browser banner

**Files:**
- Modify: `agency/templates/base.html` (add nav item between Prompts and Memory)
- Modify: `agency/templates/document_view.html` (add banner for scheduled prompts)
- Modify: `agency/app.py` (pass schedule info to document_view context)

- [ ] **Step 1: Add Schedule nav item to sidebar**

In `agency/templates/base.html`, after the Prompts nav item (line 377) and before the Memory nav item (line 379), add:

```html
        <a href="/{{ group }}/schedule" class="nav-item {% if active == 'schedule' %}active{% endif %}">
          <span class="flex items-center gap-2">
            <svg class="nav-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 7V3m8 4V3m-9 8h10M5 21h14a2 2 0 002-2V7a2 2 0 00-2-2H5a2 2 0 00-2 2v12a2 2 0 002 2z"/></svg>
            Schedule
          </span>
        </a>
```

- [ ] **Step 2: Add schedule context to document_view route**

In `agency/app.py`, in the `document_view()` handler (around line 2840), before the `return templates.TemplateResponse(...)`, add logic to detect if the file is a scheduled prompt:

```python
    # Check if this file is a scheduled prompt
    schedule_slug = ""
    prompts_dir = g["shared"] / "prompts"
    if fpath.parent == prompts_dir and fpath.suffix == ".md" and not fpath.name.startswith("_"):
        group_cfg = GROUPS.get(g["key"], {})
        dispatch_cfg = group_cfg.get("dispatch", {})
        stem = fpath.name.removesuffix(".md")
        for agent_rules in dispatch_cfg.get("agents", {}).values():
            if isinstance(agent_rules, list):
                for rule in agent_rules:
                    if rule.get("prompt") == fpath.name:
                        schedule_slug = stem
                        break
            if schedule_slug:
                break
```

Add `"schedule_slug": schedule_slug,` to the template context dict.

- [ ] **Step 3: Add banner to document_view.html**

In `agency/templates/document_view.html`, after the breadcrumb/header area and before the content section, add:

```html
  {% if schedule_slug %}
  <div class="mb-4 px-4 py-3 rounded-lg bg-indigo-50 border border-indigo-200 text-sm text-indigo-800 flex items-center justify-between">
    <span>This file is a scheduled task.</span>
    <a href="/{{ group }}/schedule/{{ schedule_slug }}" class="font-medium text-indigo-600 hover:underline">View as task →</a>
  </div>
  {% endif %}
```

- [ ] **Step 4: Verify both changes work**

Run: `.venv/bin/python3 -m agency.app &`
Check sidebar has "Schedule" item. Browse to a prompt file in documents and see the banner. Kill server after.

- [ ] **Step 5: Commit**

```bash
git add agency/templates/base.html agency/templates/document_view.html agency/app.py
git commit -m "feat(schedule): add sidebar nav and document browser integration"
```

---

### Task 7: Enhanced agent profile — replace schedule pills with task cards

**Files:**
- Modify: `agency/app.py` (enhance agent_profile route context)
- Modify: `agency/templates/agent_profile.html` (replace pills section)

- [ ] **Step 1: Add task card data to agent_profile route**

In `agency/app.py`, in the `agent_profile()` handler (around line 2270), after the existing `agent_schedule` and `dispatch_enabled` lines, add:

```python
    # Build enriched task cards for this agent's schedule
    agent_tasks = []
    for rule in agent_schedule:
        prompt_file = rule.get("prompt", "")
        if not prompt_file:
            continue
        slug = prompt_file.removesuffix(".md")
        prompt_path = g["shared"] / "prompts" / prompt_file
        meta, _ = parse_prompt_frontmatter(prompt_path)
        last_runs = collect_task_runs(g, slug, limit=1)
        last_run = last_runs[0] if last_runs else None
        # Only include runs by this specific agent
        agent_last_runs = [r for r in collect_task_runs(g, slug, limit=5) if r["agent"] == agent]
        agent_last_run = agent_last_runs[0] if agent_last_runs else None
        agent_tasks.append({
            "slug": slug,
            "name": humanize_prompt_name(prompt_file),
            "at": rule.get("at"),
            "every": rule.get("every"),
            "condition": rule.get("condition"),
            "last_run": agent_last_run,
        })
```

Add `"agent_tasks": agent_tasks,` to the template context dict.

- [ ] **Step 2: Replace schedule pills in agent_profile.html**

In `agency/templates/agent_profile.html`, replace lines 37-51 (the schedule pills block) with:

```html
      {% if agent_tasks %}
      <div class="mt-3 space-y-2">
        {% for task in agent_tasks %}
        <a href="/{{ group }}/schedule/{{ task.slug }}" class="flex items-center justify-between px-3 py-2 rounded-lg border border-gray-200 hover:border-indigo-300 hover:bg-indigo-50/30 transition-all text-sm group">
          <div class="flex items-center gap-2">
            <span class="font-medium text-gray-800 group-hover:text-indigo-700">{{ task.name }}</span>
            {% if task.condition %}
            <span class="px-1.5 py-0.5 rounded text-xs bg-amber-50 text-amber-700 border border-amber-200">{{ task.condition }}</span>
            {% elif task.at %}
            <span class="text-xs text-gray-400 font-mono">@ {{ task.at }}</span>
            {% elif task.every %}
            <span class="text-xs text-gray-400 font-mono">every {{ task.every }}</span>
            {% endif %}
          </div>
          {% if task.last_run %}
          <span class="inline-flex items-center gap-1.5 text-xs {% if task.last_run.status == 'error' %}text-red-500{% else %}text-gray-400{% endif %}">
            <span class="w-1.5 h-1.5 rounded-full {% if task.last_run.status == 'error' %}bg-red-500{% else %}bg-emerald-400{% endif %}"></span>
            {{ task.last_run.date }} {{ task.last_run.time }}
          </span>
          {% endif %}
        </a>
        {% endfor %}
      </div>
      {% else %}
      <div class="mt-2">
        <span class="text-xs text-gray-400">No scheduled tasks</span>
      </div>
      {% endif %}
```

- [ ] **Step 3: Verify the agent profile renders correctly**

Run: `.venv/bin/python3 -m agency.app &`
Browse to an agent profile that has dispatch rules. Verify task cards appear with links to schedule detail. Kill server after.

- [ ] **Step 4: Commit**

```bash
git add agency/app.py agency/templates/agent_profile.html
git commit -m "feat(schedule): replace agent profile schedule pills with task cards"
```

---

### Task 8: Handle prompt frontmatter in dispatch runner

**Files:**
- Modify: `agency/dispatch/run.py` (strip frontmatter before passing to agent)
- Modify: `tests/test_schedule.py` (add test)

- [ ] **Step 1: Write failing test**

Add to `tests/test_schedule.py`:

```python
from agency.dispatch.run import strip_prompt_frontmatter


class TestStripPromptFrontmatter:
    def test_strips_frontmatter(self):
        text = "---\ndescription: test\n---\n# The prompt\nDo stuff."
        assert strip_prompt_frontmatter(text) == "# The prompt\nDo stuff."

    def test_no_frontmatter_unchanged(self):
        text = "# The prompt\nDo stuff."
        assert strip_prompt_frontmatter(text) == "# The prompt\nDo stuff."

    def test_empty_string(self):
        assert strip_prompt_frontmatter("") == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_schedule.py::TestStripPromptFrontmatter -v`
Expected: ImportError.

- [ ] **Step 3: Implement frontmatter stripping in dispatch runner**

In `agency/dispatch/run.py`, add near the top (after imports):

```python
def strip_prompt_frontmatter(text: str) -> str:
    """Remove YAML frontmatter from prompt text, returning body only."""
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            return parts[2].strip()
    return text
```

Then in `_run_agent()`, change line 194 from:

```python
    result = integration.run(agent_dir, prompt_path, timeout)
```

to:

```python
    # Strip frontmatter from prompt before passing to agent
    raw_prompt = prompt_path.read_text()
    clean_prompt = strip_prompt_frontmatter(raw_prompt)
    if clean_prompt != raw_prompt:
        # Write stripped content to a temp file so integration gets clean prompt
        import tempfile
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False, dir=log_dir) as tmp:
            tmp.write(clean_prompt)
            clean_path = Path(tmp.name)
        try:
            result = integration.run(agent_dir, clean_path, timeout)
        finally:
            clean_path.unlink(missing_ok=True)
    else:
        result = integration.run(agent_dir, prompt_path, timeout)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_schedule.py::TestStripPromptFrontmatter -v`
Expected: All pass.

Run the full test suite to check nothing broke:
`.venv/bin/python -m pytest tests/ -v`

- [ ] **Step 5: Commit**

```bash
git add agency/dispatch/run.py tests/test_schedule.py
git commit -m "feat(schedule): strip frontmatter from prompts before dispatch"
```

---

### Task 9: Save description and expected_output from task detail

**Files:**
- Modify: `agency/app.py` (add save endpoint for task metadata)
- Modify: `agency/templates/schedule_detail.html` (make description and expected_output editable forms)

- [ ] **Step 1: Add the save route**

Add after `schedule_run_now` in `agency/app.py`:

```python
@app.post("/{group}/schedule/{slug}/save", response_class=HTMLResponse)
async def schedule_save_meta(request: Request, group: str, slug: str):
    """Save task description and expected_output to prompt frontmatter."""
    g = get_group(group)
    prompt_file = f"{slug}.md"
    prompt_path = g["shared"] / "prompts" / prompt_file
    if not prompt_path.exists():
        raise HTTPException(404, "Prompt not found")

    validate_file_access(prompt_path, g["path"], allowed_roots=get_allowed_roots(g))

    form = await request.form()
    new_description = form.get("description", "").strip()
    new_expected = form.get("expected_output", "").strip()

    meta, body = parse_prompt_frontmatter(prompt_path)
    if new_description:
        meta["description"] = new_description
    elif "description" in meta:
        del meta["description"]
    if new_expected:
        meta["expected_output"] = new_expected
    elif "expected_output" in meta:
        del meta["expected_output"]

    # Rebuild file
    if meta:
        front = yaml.dump(meta, default_flow_style=False).strip()
        content = f"---\n{front}\n---\n\n{body}\n"
    else:
        content = f"{body}\n"

    prompt_path.write_text(content)
    return RedirectResponse(f"/{group}/schedule/{slug}", status_code=303)
```

- [ ] **Step 2: Update schedule_detail.html to make fields editable**

Replace the header description line and expected output section. In the Header section, replace:

```html
    {% if description %}
    <p class="text-gray-500 mt-1">{{ description }}</p>
    {% endif %}
```

with:

```html
    <form method="POST" action="/{{ group }}/schedule/{{ slug }}/save" class="mt-2">
      <div class="space-y-3">
        <div>
          <label class="block text-xs font-medium text-gray-500 mb-1">Description</label>
          <input type="text" name="description" value="{{ description }}"
                 placeholder="Why this task exists..."
                 class="w-full text-sm border border-gray-200 rounded-lg px-3 py-2 focus:border-indigo-300 focus:ring-1 focus:ring-indigo-300">
        </div>
        <div>
          <label class="block text-xs font-medium text-gray-500 mb-1">Expected Output</label>
          <input type="text" name="expected_output" value="{{ expected_output }}"
                 placeholder="What a successful run produces..."
                 class="w-full text-sm border border-gray-200 rounded-lg px-3 py-2 focus:border-indigo-300 focus:ring-1 focus:ring-indigo-300">
        </div>
        <div class="flex justify-end">
          <button type="submit" class="px-3 py-1.5 text-xs font-medium rounded-lg bg-gray-100 text-gray-700 hover:bg-gray-200 transition-colors">Save</button>
        </div>
      </div>
    </form>
```

Then remove the separate "Expected Output" section since it's now in the header form.

- [ ] **Step 3: Verify save works**

Run: `.venv/bin/python3 -m agency.app &`
Browse to a task detail, edit description, save. Check the prompt file has frontmatter. Kill server after.

- [ ] **Step 4: Commit**

```bash
git add agency/app.py agency/templates/schedule_detail.html
git commit -m "feat(schedule): editable description and expected_output via frontmatter"
```

---

### Task 10: Full integration test + CLAUDE.md update

**Files:**
- Modify: `tests/test_schedule.py` (add integration-style test)
- Modify: `CLAUDE.md` (update route table, template list, project structure)

- [ ] **Step 1: Run full test suite**

Run: `.venv/bin/python -m pytest tests/ -v`
Expected: All tests pass (existing + new).

- [ ] **Step 2: Update CLAUDE.md**

In the root `CLAUDE.md`:

Add to the **Route Structure > Org-Scoped Routes** table:

```
| GET | `/{group}/schedule` | Task-oriented view of dispatch schedule |
| GET | `/{group}/schedule/{slug}` | Task detail — prompt content, schedule, history, run button |
| POST | `/{group}/schedule/{slug}/run` | Manually trigger agent run for a task |
| POST | `/{group}/schedule/{slug}/save` | Save task description/expected_output to frontmatter |
```

Add to the **Project Structure** template list:

```
│       ├── schedule.html          # Scheduled task list (card grid)
│       ├── schedule_detail.html   # Task detail: schedule, prompt, history, run now
```

Update template count from 27 to 29.

- [ ] **Step 3: Run full test suite again**

Run: `.venv/bin/python -m pytest tests/ -v`
Expected: All tests pass.

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md tests/test_schedule.py
git commit -m "docs: update CLAUDE.md with schedule routes and templates"
```

- [ ] **Step 5: Restart service and verify end-to-end**

```bash
systemctl --user restart agency.service
```

Browse to:
1. `/{group}/schedule` — task cards visible
2. Click a card → task detail with schedule, prompt, history
3. Agent profile → task cards replace old pills
4. Document browser → prompt file shows "View as task" banner
5. "Run now" → triggers and redirects with banner
