---
name: agency-setup
description: >
  Set up a fully functional agent team for any codebase with Agency-compatible
  structure. Use when 'agency setup', 'set up agents', 'create agent team',
  'bootstrap agents', 'add agents to this project', or setting up agent
  infrastructure for a repository. Creates agents/, shared/, dispatch, tmux
  config, and optionally registers with Agency dashboard.
user_invocable: true
---

# Agency Setup

Interactive, Claude-led skill that analyzes a codebase and sets up a fully functional
agent team. Claude does most of the suggesting — the user approves with "ok" or tweaks.

## Phase 1: Analyze the Codebase

Gather context automatically (no user input). Read whichever of these exist:

1. **Project identity**: CLAUDE.md, README.md, README, docs/
2. **Language/framework**: package.json, pyproject.toml, go.mod, Cargo.toml, Gemfile,
   requirements.txt, pom.xml, build.gradle, Makefile
3. **Structure**: `ls` the project root, glob for key patterns (`src/`, `lib/`, `app/`,
   `tests/`, `scripts/`, `templates/`, `config/`)
4. **Git context**: `git log --oneline -15` for recent activity, `git remote -v` for origin
5. **Existing agents**: Check if `agents/` already exists (abort if fully populated)
6. **Deployment**: Check for Dockerfile, Containerfile, docker-compose, systemd units,
   CI/CD configs (.github/workflows/, .gitlab-ci.yml)

From this, determine:
- **Language** and **framework** (e.g., Python/FastAPI, TypeScript/Next.js, Go/stdlib)
- **Project purpose** (web app, CLI tool, library, API, pipeline, etc.)
- **Complexity** (file count, directory depth, number of modules)
- **Deployment model** (container, systemd, serverless, library/package)
- **Testing setup** (test framework, test directory, CI)

Present a 3-4 sentence summary of what you found. Then proceed to Phase 2.

## Phase 2: Propose Agent Team

Based on the analysis, propose 3-5 agents. Present as a table:

```
| Agent | Role | Owns | Permissions | Dispatch |
|-------|------|------|-------------|----------|
| product | PM & Developer | codebase, features | edit code | evening |
| maintainer | Upkeep & Ops | service health, config | read-only | morning, cleanup |
| strategist | Vision & Direction | product roadmap | read-only | morning |
```

Below the table, give a 1-2 sentence rationale for each agent explaining WHY this
project needs this role specifically.

**Guidelines for agent design:**

- Every project needs a **builder** agent (can edit code)
- Most projects benefit from a **maintainer** agent (health checks, quality)
- Projects with a product direction benefit from a **strategist/advisor** agent
- Large projects may need domain specialists (e.g., frontend + backend, or data + API)
- Keep the team lean — 3 agents is often enough, 5 is the practical max
- Each agent must have a distinct, non-overlapping domain
- Only ONE agent should have code-edit permissions (the builder)

**End Phase 2 by asking:** "Does this team look right? You can add, remove, or rename
agents, or say 'ok' to proceed."

## Phase 3: Quick Customization

For each agent, ask ONE question:

> "{Agent name} will observe {default tasks}. Any specific observation tasks to add?
> (Enter to use defaults)"

Default observation tasks by archetype:
- **Builder**: code quality scan, documentation drift, template/route consistency
- **Maintainer**: service health, config validation, dependency audit, log errors
- **Strategist**: product review, feature gap analysis, landscape comparison
- **Domain specialist**: domain-specific metrics and patterns

If the user presses Enter or says "ok"/"defaults", use the defaults. This phase should
take seconds, not minutes.

## Phase 4: Generate Everything

Read the templates from `references/` before generating:
- `references/templates.md` — agent CLAUDE.md, memory.md, shared memory
- `references/dispatch-templates.md` — dispatch.sh, timer, service, tmux script
- `references/observation-system-steps.md` — universal observation pipeline steps

### 4.1 Directory Structure

```bash
mkdir -p agents/{agent1,agent2,...}
mkdir -p agents/shared/{observations,proposals,decisions,logs,prompts}
```

### 4.2 Agent Files

For each agent, generate from the templates in `references/templates.md`:
- `agents/{agent}/CLAUDE.md` — Fill template with project-specific context
- `agents/{agent}/memory.md` — Standard memory template

Plus shared files:
- `agents/shared/memory.md` — Project context, preferences, learned facts
- `agents/shared/prompts/_observation-system-steps.md` — Copy from `references/observation-system-steps.md`

### 4.3 Dispatch Prompts

For each agent, generate a dispatch prompt at `agents/shared/prompts/{agent}-routine.md`.
Follow the structure in `references/dispatch-templates.md`:
- Section 0: Load memory
- Numbered observation tasks (from Phase 3)
- Pre-approved actions
- Boundaries
- Update memory
- Observation system steps reference

If the maintainer/cleanup agent exists, also generate `{agent}-cleanup.md`.

### 4.4 Dispatch Infrastructure

Generate from templates in `references/dispatch-templates.md`:
- `agents/shared/dispatch.sh` — Event-based dispatcher with morning + evening windows
- `agents/shared/{project}-dispatch.timer` — Systemd timer (7am + 9pm ET default)
- `agents/shared/{project}-dispatch.service` — Systemd oneshot service

Use the project directory name as the `{project}` prefix for timer/service names.

Make dispatch.sh executable: `chmod +x agents/shared/dispatch.sh`

### 4.5 Tmux Launch Script

Generate `agents/shared/tmux-agents.sh` from the template in `references/dispatch-templates.md`.
The script creates a tmux session with:
- One pane per agent + one Terminal pane
- Color-coded borders (cycle through a palette)
- Unicode symbol labels
- Each agent pane launches `claude --dangerously-skip-permissions` from its agent directory
- Grid layout: calculate rows/cols based on agent count + 1 terminal pane

Make it executable: `chmod +x agents/shared/tmux-agents.sh`

### 4.6 Gitignore

Add `agents/` to `.gitignore` if not already present:
```bash
grep -qxF 'agents/' .gitignore 2>/dev/null || echo 'agents/' >> .gitignore
```

### 4.7 Agency Registration

Check for an Agency `config.yaml` in common locations, in order: the current
working directory, `~/dev/agency`, `~/agency`, and `~/.agency`. Use the first
one found.

If none of these exist, ask: "Where is your Agency install? (path to the
directory containing config.yaml, or leave blank to skip)". If the user
provides a path and it contains a valid `config.yaml`, use it; if left blank,
skip silently.

If found, ask: "Register this agent group with Agency dashboard? (Y/n)"

If yes:
- Derive a group key from the project directory name (lowercase, hyphens)
- Add to config.yaml under `groups:` with name, path, agents list, and tmux_config path
- The `tmux_config` field should point to the absolute path of `tmux-agents.sh`
- **Write dispatch config** derived from the generated `dispatch.sh` event handlers:
  - Set `dispatch.enabled: true`, `dispatch.timeout: 300`, `dispatch.daily_limit: 15`
  - For each agent→prompt mapping in `dispatch.sh`, add a rule under `dispatch.agents`:
    ```yaml
    dispatch:
      enabled: true
      timeout: 300
      daily_limit: 15
      agents:
        agent-name:
        - prompt: agent-name-routine.md
          at: "07:00"        # From the morning event handler time
        - prompt: agent-name-cleanup.md
          at: "21:00"        # From the evening event handler time
    ```
  - The `at` time should match the midpoint of the dispatch.sh time window for that event
  - If an assignment has a code condition (e.g., only runs when a DB check passes), add
    `condition: condition-name` to the rule — these display as read-only in the UI
  - This keeps config.yaml in sync with dispatch.sh so the Agency dashboard shows accurate schedules

### 4.8 Systemd Timer Setup

Ask: "Enable dispatch timer? This will run agents at 7am and 9pm ET daily. (Y/n)"

If yes:
- Symlink timer and service to `~/.config/systemd/user/`
- Run `systemctl --user daemon-reload`
- Run `systemctl --user enable --now {project}-dispatch.timer`
- Show the next fire time

## Phase 5: Summary

Print a summary of everything created:
- Number of agents and their names
- File count
- Dispatch schedule (if enabled)
- Agency registration status
- Tmux command to launch: `agents/shared/tmux-agents.sh`

## Important Notes

- If `agents/` already exists with content, warn the user and ask before overwriting
- All generated files use the project's actual context (language, framework, paths)
- Agent CLAUDE.md files should reference the project's root CLAUDE.md conventions
- Dispatch prompts should use project-appropriate commands (npm test vs pytest vs go test)
- The builder agent's boundaries should match the project's actual structure
