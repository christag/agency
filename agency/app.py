"""Agency Dashboard — multi-group agent management interface."""

import csv
import io
import os
import re
import shutil
import subprocess
import tempfile
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

import markdown
import yaml
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape

try:
    import nh3
except ImportError:  # pragma: no cover
    nh3 = None
    import sys as _sys
    print(
        "WARNING: nh3 is not installed — rendered markdown will be HTML-escaped as a "
        "fail-safe against XSS. Run 'pip install -e .' to restore rich formatting.",
        file=_sys.stderr,
    )

import uvicorn

from agency.config import normalize_agents, agent_names, get_agent_dir, get_allowed_roots, find_agent_in_config, is_shared_agent
from agency.integrations import get_integration, detect_integration, REGISTRY
from agency.dispatch.install import install_timer, get_timer_status as _get_timer_status
import json as json_module
from agency.workspaces import migrate_tmux_config, REGISTRY as WORKSPACE_REGISTRY

# ── Config ────────────────────────────────────────────────────────────────────

CONFIG_PATH = Path.cwd() / "config.yaml"


def load_config() -> dict:
    """Read config.yaml and return dict. Returns defaults if file doesn't exist."""
    if not CONFIG_PATH.exists():
        return {"agency": {"title": "Agency", "default_group": ""}, "groups": {}}
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f) or {}


def save_config(config: dict) -> None:
    """Atomically write config.yaml (temp file + rename)."""
    fd, tmp = tempfile.mkstemp(dir=CONFIG_PATH.parent, suffix=".yaml")
    try:
        with os.fdopen(fd, "w") as f:
            yaml.dump(config, f, default_flow_style=False, sort_keys=False)
        os.replace(tmp, CONFIG_PATH)
    except Exception:
        os.unlink(tmp)
        raise


def reload_groups() -> None:
    """Reload the global GROUPS dict from config."""
    global GROUPS, CONFIG
    CONFIG = load_config()
    GROUPS = CONFIG.get("groups", {})
    # Normalize agent lists so all code paths see consistent data
    for key, g in GROUPS.items():
        default_int = g.get("default_integration", "claude-code")
        raw_agents = g.get("agents", [])
        g["_agents_normalized"] = normalize_agents(raw_agents, default_int)
        # Keep agents as a name list for backward compat
        g["agents"] = agent_names(g["_agents_normalized"])
        GROUPS[key] = migrate_tmux_config(GROUPS[key])


CONFIG = load_config()
GROUPS = CONFIG.get("groups", {})
for key, g in GROUPS.items():
    default_int = g.get("default_integration", "claude-code")
    raw_agents = g.get("agents", [])
    g["_agents_normalized"] = normalize_agents(raw_agents, default_int)
    g["agents"] = agent_names(g["_agents_normalized"])
    g.update(migrate_tmux_config(g))


def get_agency_config() -> dict:
    """Return agency-level config with defaults."""
    agency = CONFIG.get("agency", {})
    return {
        "title": agency.get("title", "Agency"),
        "default_group": agency.get("default_group", "") or (list(GROUPS.keys())[0] if GROUPS else ""),
        "decided_by": agency.get("decided_by", "admin"),
        "ai_backend": agency.get("ai_backend", "claude-code"),
        "theme": agency.get("theme", ""),
    }


# ── Themes ─────────────────────────────────────────────────────────────────

THEMES_DIR = Path(__file__).parent / "themes"


def load_themes() -> dict[str, dict]:
    """Read all theme YAML files from the themes directory."""
    themes = {}
    if not THEMES_DIR.is_dir():
        return themes
    for f in sorted(THEMES_DIR.glob("*.yaml")):
        try:
            data = yaml.safe_load(f.read_text()) or {}
            themes[f.stem] = data
        except (yaml.YAMLError, OSError):
            continue
    return themes


def generate_theme_css(theme: dict) -> str:
    """Generate CSS custom properties and structural overrides from a theme dict."""
    lines = ["<style>/* Theme: {} */".format(theme.get("name", "Custom"))]

    light = theme.get("light", {})
    dark = theme.get("dark", {})
    logo = theme.get("logo", {})
    scale = theme.get("scale", {})

    # CSS custom properties
    props_light = []
    props_dark = []
    for key, val in light.items():
        props_light.append(f"  --t-{key.replace('_', '-')}: {val};")
    for key, val in dark.items():
        props_dark.append(f"  --t-{key.replace('_', '-')}: {val};")

    # Logo properties
    logo_light = logo.get("light", {})
    logo_dark = logo.get("dark", {})
    for key, val in logo_light.items():
        props_light.append(f"  --t-logo-{key.replace('_', '-')}: {val};")
    for key, val in logo_dark.items():
        props_dark.append(f"  --t-logo-{key.replace('_', '-')}: {val};")

    # Scale properties
    for key, val in scale.items():
        props_light.append(f"  --t-scale-{key}: {val};")
        props_dark.append(f"  --t-scale-{key}: {val};")

    lines.append(":root {")
    lines.extend(props_light)
    lines.append("}")
    lines.append(".dark {")
    lines.extend(props_dark)
    lines.append("}")

    # Structural overrides using the custom properties
    lines.append("""
/* Body */
body { background-color: var(--t-bg) !important; color: var(--t-text) !important; }

/* Sidebar */
nav#sidebar { background-color: var(--t-sidebar-bg) !important; }
.nav-item { color: var(--t-sidebar-text) !important; }
.nav-item:hover { color: var(--t-sidebar-active-text) !important; }
.nav-item.active { color: #fff !important; background: var(--t-sidebar-active-bg) !important; }
.nav-section { color: var(--t-sidebar-section) !important; }
.theme-toggle { color: var(--t-sidebar-text) !important; }
.theme-toggle:hover { color: var(--t-sidebar-active-text) !important; }

/* Logo */
nav#sidebar .logo-square { background-color: var(--t-logo-bg, var(--t-sidebar-bg)) !important; }
nav#sidebar .logo-square svg line { stroke: var(--t-logo-line, white) !important; }
nav#sidebar .logo-square svg circle { fill: var(--t-logo-node, white) !important; }

/* Mobile top bar */
.mobile-topbar { background-color: var(--t-sidebar-bg) !important; border-color: var(--t-border-subtle) !important; }

/* Cards and surfaces */
.bg-white, .dark .bg-white { background-color: var(--t-bg-card) !important; }
.bg-gray-50, .dark .bg-gray-50 { background-color: var(--t-bg) !important; }
.dark .bg-gray-100 { background-color: var(--t-bg-surface, var(--t-bg-card)) !important; }

/* Borders */
.border-gray-200, .dark .border-gray-200 { border-color: var(--t-border) !important; }
.border-gray-100, .dark .border-gray-100 { border-color: var(--t-border-subtle) !important; }

/* Text */
.text-gray-900, .dark .text-gray-900 { color: var(--t-text-heading) !important; }
.text-gray-800, .dark .text-gray-800 { color: var(--t-text-heading) !important; }
.text-gray-700, .dark .text-gray-700 { color: var(--t-text) !important; }
.text-gray-600, .dark .text-gray-600 { color: var(--t-text-muted) !important; }
.text-gray-500, .dark .text-gray-500 { color: var(--t-text-faint) !important; }

/* Primary action buttons */
.bg-indigo-600, .bg-purple-600 { background-color: var(--t-primary) !important; color: var(--t-primary-text) !important; }
.hover\\:bg-indigo-700:hover, .hover\\:bg-purple-700:hover { background-color: var(--t-primary-hover) !important; }
.text-indigo-600 { color: var(--t-primary) !important; }
.hover\\:text-indigo-800:hover { color: var(--t-primary-hover) !important; }
.focus\\:ring-indigo-500:focus { --tw-ring-color: var(--t-primary) !important; }
.focus\\:border-indigo-500:focus { border-color: var(--t-primary) !important; }

/* Form inputs */
.dark input, .dark textarea, .dark select {
  background-color: var(--t-code-bg) !important;
  border-color: var(--t-border) !important;
  color: var(--t-text) !important;
}
.dark input:focus, .dark textarea:focus, .dark select:focus {
  border-color: var(--t-primary) !important;
}

/* Code */
.prose code { background: var(--t-code-bg) !important; }
.prose pre { background: var(--t-code-bg) !important; }

/* Prose links */
.prose a { color: var(--t-link) !important; }
""")

    lines.append("</style>")
    return "\n".join(lines)


_THEME_CSS_CACHE: dict[str, str] = {}


def get_theme_css() -> str:
    """Return theme CSS for the currently selected theme, or empty string."""
    theme_key = CONFIG.get("agency", {}).get("theme", "")
    if not theme_key:
        return ""
    if theme_key in _THEME_CSS_CACHE:
        return _THEME_CSS_CACHE[theme_key]
    themes = load_themes()
    if theme_key not in themes:
        return ""
    css = generate_theme_css(themes[theme_key])
    _THEME_CSS_CACHE[theme_key] = css
    return css


# ── Dispatch Helpers ──────────────────────────────────────────────────────────

def _workspace_types_json() -> str:
    """Serialize workspace registry for admin templates."""
    return json_module.dumps([
        {"name": ws.name, "display_name": ws.display_name, "description": ws.description}
        for ws in WORKSPACE_REGISTRY.values()
    ])


def get_dispatch_status() -> dict:
    """Return dispatch installation status."""
    status = _get_timer_status()
    config_dispatch = CONFIG.get("agency", {}).get("dispatch", {})
    status["interval"] = config_dispatch.get("interval", 15)
    status["installed"] = status["installed"] or config_dispatch.get("installed", False)
    return status


def install_dispatch(interval: int = 15) -> str | None:
    """Install dispatch timer using platform-native scheduler."""
    error = install_timer(str(CONFIG_PATH), interval)
    if error:
        return error
    # Update config
    config = load_config()
    if "agency" not in config:
        config["agency"] = {}
    if "dispatch" not in config["agency"]:
        config["agency"]["dispatch"] = {}
    config["agency"]["dispatch"]["installed"] = True
    config["agency"]["dispatch"]["interval"] = interval
    save_config(config)
    reload_groups()
    return None


app = FastAPI(title="Agency Dashboard")


# ── Security headers ─────────────────────────────────────────────────────────
# Defense-in-depth headers on every response. The CSP explicitly allow-lists the
# external resources the UI already loads (Tailwind Play CDN — which needs
# 'unsafe-eval' for its in-browser JIT — and Google Fonts) and otherwise confines
# everything to same-origin, blocks framing/objects, and pins base-uri/form-action.
# 'unsafe-inline' remains for the app's inline <script>/<style> blocks; tightening
# that to nonces is a template-wide change. XSS payloads in rendered markdown are
# already stripped by render_md.
# HSTS is intentionally omitted: Agency serves plain HTTP on loopback by default;
# HSTS belongs on the terminating HTTPS reverse proxy, not here.
_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline' 'unsafe-eval' https://cdn.tailwindcss.com; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src 'self' https://fonts.gstatic.com; "
    "img-src 'self' data:; "
    "connect-src 'self' https://cdn.tailwindcss.com; "
    "manifest-src 'self'; "
    "object-src 'none'; "
    "base-uri 'self'; "
    "form-action 'self'; "
    "frame-ancestors 'none'"
)
_SECURITY_HEADERS = {
    "Content-Security-Policy": _CSP,
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "Permissions-Policy": "geolocation=(), camera=(), microphone=()",
}


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    for header, value in _SECURITY_HEADERS.items():
        response.headers.setdefault(header, value)
    return response


app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

md = markdown.Markdown(extensions=["tables", "fenced_code", "meta", "nl2br"])

STATIC_DIR = Path(__file__).parent / "static"


# ── PWA ──────────────────────────────────────────────────────────────────────


@app.get("/sw.js")
async def service_worker():
    """Serve service worker from root for full scope."""
    return FileResponse(STATIC_DIR / "sw.js", media_type="application/javascript",
                        headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"})


@app.get("/manifest.json")
async def manifest():
    """Serve PWA manifest with dynamic app title."""
    cfg = get_agency_config()
    data = json_module.loads((STATIC_DIR / "manifest.json").read_text())
    data["name"] = cfg.get("title", "Agency")
    data["short_name"] = cfg.get("title", "Agency")
    return data


# ── Group Resolution ──────────────────────────────────────────────────────────


def get_group(group: str) -> dict:
    """Resolve a group key to its full config dict."""
    if group not in GROUPS:
        raise HTTPException(404, f"Unknown group: {group}")
    g = GROUPS[group]
    return {
        "key": group,
        "name": g["name"],
        "path": Path(g["path"]),
        "agents": g["agents"],
        "agents_full": g.get("_agents_normalized", []),
        "shared": Path(g["path"]) / "shared",
    }


def get_agent_integration(g: dict, agent_name: str):
    """Resolve the integration for an agent in a group.

    Priority: filesystem detection first (for existing agents with identity files),
    then config, then group default. This ensures that an agent with CLAUDE.md is
    always handled by the claude-code integration, even if the group default is different.
    """
    agent_dir = get_agent_dir(g, agent_name)
    # 1. Auto-detect from existing files on disk
    if agent_dir.is_dir():
        detected = detect_integration(agent_dir)
        if detected:
            return detected
    # 2. Fall back to config (for new agents or dirs with no recognized files)
    for agent_info in g.get("agents_full", []):
        if agent_info["name"] == agent_name:
            return get_integration(agent_info.get("integration", "claude-code"))
    # 3. Group default
    return get_integration("claude-code")


def safe_redirect(url: str, fallback: str = "/") -> str:
    """Validate a redirect URL is a safe relative path."""
    if url and url.startswith("/") and not url.startswith("//"):
        return url
    return fallback


def group_context(g: dict, observations: list[dict] | None = None, proposals: list[dict] | None = None) -> dict:
    """Return standard template context for a group. Accepts precomputed lists to avoid double-reads."""
    agency = get_agency_config()
    group_cfg = GROUPS.get(g["key"], {})
    if observations is None:
        observations = list_observations(g)
    if proposals is None:
        proposals = list_proposals(g)
    open_observation_count = sum(1 for c in observations if c.get("status") == "open")
    actionable_proposal_count = sum(1 for c in proposals if c.get("status") in ("proposed", "investigating"))
    floated_observation_count = sum(1 for c in observations if c.get("float") and c.get("status") == "open")
    needs_action_count = actionable_proposal_count + floated_observation_count
    decisions = list_decisions(g)
    running_decisions = sum(1 for d in decisions if d.get("execution_status") == "running")
    return {
        "group": g["key"],
        "group_name": g["name"],
        "groups": {k: v["name"] for k, v in GROUPS.items()},
        "agency_title": agency.get("title", "Agency"),
        "admin_active": False,
        "workspaces": group_cfg.get("workspaces", []),
        "workspaces_available": bool(group_cfg.get("workspaces")),
        "nav_open_observations": open_observation_count,
        "nav_actionable": needs_action_count,
        "nav_actionable_proposals": actionable_proposal_count,
        "nav_agent_count": len(g["agents"]),
        "nav_running_decisions": running_decisions,
        "show_tips": CONFIG.get("agency", {}).get("show_tips", True) is not False,
        "tips_dismissed": CONFIG.get("agency", {}).get("tips_dismissed", []),
        "theme_css": get_theme_css(),
    }


# ── Helpers ──────────────────────────────────────────────────────────────────


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Parse YAML frontmatter from markdown text. Returns (meta, body)."""
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            try:
                meta = yaml.safe_load(parts[1]) or {}
                return meta, parts[2].strip()
            except yaml.YAMLError:
                pass
    return {}, text


def render_md(text: str) -> Markup:
    """Render markdown to sanitized HTML (guards against stored XSS, CWE-79).

    Agent- and user-authored markdown may contain raw HTML (python-markdown does
    not strip it), so the rendered output is passed through nh3's allowlist
    sanitizer before being marked safe. If nh3 is unavailable, fail closed by
    escaping the output entirely rather than emitting unsanitized HTML.
    """
    md.reset()
    html = md.convert(text)
    if nh3 is not None:
        return Markup(nh3.clean(html))
    return Markup(str(escape(html)))


def validate_file_access(fpath: Path, base_path: Path, allowed_roots: list[Path] | None = None) -> None:
    """Validate file is within base_path or any allowed root. Raises HTTPException(403) if not."""
    resolved = fpath.resolve()
    # Check base path first
    try:
        resolved.relative_to(base_path.resolve())
        return
    except ValueError:
        pass
    # Check additional allowed roots
    if allowed_roots:
        for root in allowed_roots:
            try:
                resolved.relative_to(root.resolve())
                return
            except ValueError:
                continue
    raise HTTPException(403, "Access denied")


def update_frontmatter_field(filepath: Path, field: str, value: str) -> None:
    """Update a single YAML frontmatter field in a markdown file."""
    raw = filepath.read_text()
    raw = re.sub(rf'^({field}:\s*).*$', f'\\1{value}', raw, count=1, flags=re.MULTILINE)
    filepath.write_text(raw)


def update_decision_execution(decision_path: Path, field: str, value) -> None:
    """Update execution_status (or other top-level field) in a decision file."""
    raw = decision_path.read_text()
    meta, body = parse_frontmatter(raw)
    meta[field] = value
    frontmatter = yaml.dump(meta, default_flow_style=False, sort_keys=False).strip()
    decision_path.write_text(f"---\n{frontmatter}\n---\n\n{body}\n")


def execute_decision(decision_path: Path, group_path: Path, agent: str,
                     proposal_slug: str, group_key: str = "") -> None:
    """Background task: dispatch agent to act on a decision's answers."""
    now = datetime.now(timezone.utc).isoformat()
    update_decision_execution(decision_path, "execution_status", "running")

    prompt = (
        f"A decision has been made on your proposal.\n\n"
        f"Read the decision file at agents/shared/decisions/{decision_path.name}\n"
        f"Read the linked proposal at agents/shared/proposals/{proposal_slug}.md\n\n"
        f"The decision file contains an 'answers' section in the frontmatter with "
        f"the human's responses to each question you asked in the proposal.\n\n"
        f"Act on these answers:\n"
        f"- If approved/accepted: execute the proposed action\n"
        f"- If deferred: acknowledge and schedule for later\n"
        f"- If rejected: close the loop gracefully, do not proceed\n"
        f"- For choice/free-response answers: use the human's input to guide your work\n\n"
        f"Do NOT modify the decision file — Agency handles status updates automatically.\n"
        f"Just do the work described in the proposal and answers."
    )

    g = GROUPS.get(group_key, {})
    grp = {"path": group_path, "agents_full": g.get("_agents_normalized", [])}

    agent_dir = get_agent_dir(grp, agent)
    if not agent_dir.exists():
        agent_dir = group_path

    log_dir = group_path / "shared" / "logs" / datetime.now().strftime("%Y-%m-%d")
    log_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%H%M%S")
    out_path = log_dir / f"{agent}-exec-{ts}.out"
    err_path = log_dir / f"{agent}-exec-{ts}.err"

    prompt_file = log_dir / f"{agent}-exec-{ts}.prompt"
    prompt_file.write_text(prompt)

    try:
        agent_integration = get_agent_integration(grp, agent)

        if not agent_integration.supports_execution:
            update_decision_execution(decision_path, "execution_status", "failed")
            update_decision_execution(decision_path, "execution_summary",
                                      f"Integration '{agent_integration.name}' does not support execution.")
            return

        if hasattr(agent_integration, 'with_config'):
            for a in g.get("_agents_normalized", []):
                if a["name"] == agent and "integration_config" in a:
                    agent_integration = agent_integration.with_config(a["integration_config"])
                    break

        # Resolve timeout: per-agent → group dispatch → default 30 min
        dispatch_cfg = g.get("dispatch", {})
        agent_timeout = dispatch_cfg.get("agents", {}).get(agent, {})
        if isinstance(agent_timeout, dict):
            timeout = agent_timeout.get("timeout", dispatch_cfg.get("timeout", 1800))
        else:
            timeout = dispatch_cfg.get("timeout", 1800)

        result = agent_integration.run(agent_dir, prompt_file, timeout=timeout)
        out_path.write_text(result.stdout)
        err_path.write_text(result.stderr)

        updated_meta, _ = parse_frontmatter(decision_path.read_text())
        exec_status = updated_meta.get("execution_status", "")
        if exec_status not in ("complete", "failed"):
            if result.exit_code == 0:
                update_decision_execution(decision_path, "execution_status", "complete")
                update_decision_execution(decision_path, "execution_summary",
                                          "Agent completed execution (inferred from exit code).")
            else:
                update_decision_execution(decision_path, "execution_status", "failed")
                update_decision_execution(decision_path, "execution_summary",
                                          f"Agent exited with code {result.exit_code}.")
    except Exception as e:
        update_decision_execution(decision_path, "execution_status", "failed")
        update_decision_execution(decision_path, "execution_summary", f"Execution error: {e}")
    finally:
        if prompt_file.exists():
            prompt_file.unlink()


def parse_csv_to_rows(text: str) -> tuple[list[str], list[list[str]]]:
    """Parse CSV text to header + rows, skipping comment lines."""
    lines = [l for l in text.splitlines() if l.strip() and not l.strip().startswith("#")]
    if not lines:
        return [], []
    reader = csv.reader(io.StringIO("\n".join(lines)))
    rows = list(reader)
    return rows[0], rows[1:]


def check_ttl_expired(meta: dict) -> bool:
    """Check if an item has exceeded its TTL based on date + ttl_days."""
    ttl = meta.get("ttl_days")
    if not ttl:
        return False
    item_date = meta.get("date")
    if not item_date:
        return False
    if isinstance(item_date, str):
        try:
            item_date = datetime.fromisoformat(item_date)
        except (ValueError, TypeError):
            return False
    elif not isinstance(item_date, datetime):
        try:
            # Handle date objects (not datetime)
            item_date = datetime.combine(item_date, datetime.min.time())
        except (TypeError, AttributeError):
            return False
    try:
        ttl = int(ttl)
    except (ValueError, TypeError):
        return False
    return datetime.now(tz=item_date.tzinfo) > item_date + timedelta(days=ttl)


def enforce_ttl(filepath: Path, meta: dict) -> bool:
    """Auto-archive an item if its TTL has expired. Returns True if archived."""
    status = meta.get("status", "")
    if status in ("archived", "dismissed", "decided"):
        return False
    if check_ttl_expired(meta):
        update_frontmatter_field(filepath, "status", "archived")
        meta["status"] = "archived"
        return True
    return False


def extract_display_title(body: str | None, slug: str) -> str:
    """Extract a human-readable title from markdown body text.

    Looks for the first **bold text** in the body (not inside headings).
    Falls back to slug with hyphens replaced by spaces.
    Truncates to 120 chars if needed.
    """
    if not body:
        return slug.replace("-", " ")

    for line in body.split("\n"):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        m = re.search(r"\*\*(.+?)\*\*", stripped)
        if m:
            title = m.group(1).rstrip(".,;:!?")
            if len(title) > 120:
                return title[:117] + "..."
            return title

    return slug.replace("-", " ")


def list_markdown_items(g: dict, subdir: str, apply_ttl: bool = False) -> list[dict]:
    """List markdown files from a shared subdirectory with parsed frontmatter."""
    item_dir = g["shared"] / subdir
    if not item_dir.exists():
        return []
    items = []
    for f in sorted(item_dir.glob("*.md"), reverse=True):
        raw = f.read_text()
        meta, body = parse_frontmatter(raw)
        meta.update({
            "_filename": f.name,
            "_body": body,
            "_slug": f.stem,
            "_title": extract_display_title(body, f.stem),
        })
        if apply_ttl:
            enforce_ttl(f, meta)
        items.append(meta)
    return items


def list_observations(g: dict) -> list[dict]:
    return list_markdown_items(g, "observations", apply_ttl=True)


def list_proposals(g: dict) -> list[dict]:
    return list_markdown_items(g, "proposals", apply_ttl=True)


def list_decisions(g: dict) -> list[dict]:
    return list_markdown_items(g, "decisions")


def build_pipeline_stats(observations: list[dict], proposals: list[dict],
                         decisions: list[dict]) -> dict:
    """Compute pipeline stage counts and 7-day sparkline data for dashboard."""
    today = datetime.now().date()

    def sparkline_buckets(items: list[dict]) -> list[int]:
        buckets = [0] * 7
        for item in items:
            date_val = item.get("date", "")
            if isinstance(date_val, str):
                try:
                    date_val = datetime.fromisoformat(date_val).date()
                except (ValueError, TypeError):
                    continue
            elif isinstance(date_val, datetime):
                date_val = date_val.date()
            elif hasattr(date_val, "year"):
                pass
            else:
                continue
            days_ago = (today - date_val).days
            if 0 <= days_ago < 7:
                buckets[6 - days_ago] += 1
        return buckets

    obs_total = len(observations)
    prop_total = len(proposals)
    dec_total = len(decisions)

    if obs_total > 8 and prop_total <= 1:
        flow = "bottleneck"
    elif obs_total > 5 * max(prop_total, 1) and obs_total > 5:
        flow = "bottleneck"
    else:
        flow = "healthy"

    return {
        "observations": {"total": obs_total, "sparkline": sparkline_buckets(observations)},
        "proposals": {"total": prop_total, "sparkline": sparkline_buckets(proposals)},
        "decisions": {"total": dec_total, "sparkline": sparkline_buckets(decisions)},
        "flow_status": flow,
    }


def build_activity_feed(observations: list[dict], proposals: list[dict],
                        limit: int = 15) -> list[dict]:
    """Build a cross-agent chronological feed for the dashboard activity zone."""
    events = []

    def _parse_dt(date_val) -> datetime:
        """Parse a date value into a naive datetime for safe comparison."""
        if isinstance(date_val, str):
            try:
                dt = datetime.fromisoformat(date_val)
            except (ValueError, TypeError):
                return datetime.min
        elif isinstance(date_val, datetime):
            dt = date_val
        else:
            return datetime.min
        # Strip tzinfo so naive and aware datetimes can be compared
        return dt.replace(tzinfo=None) if dt.tzinfo is not None else dt

    for o in observations:
        events.append({
            "type": "observation",
            "slug": o.get("_slug", ""),
            "agent": o.get("agent", ""),
            "timestamp": _parse_dt(o.get("date", "")),
            "status": o.get("status", ""),
        })

    for p in proposals:
        events.append({
            "type": "proposal",
            "slug": p.get("_slug", ""),
            "agent": p.get("origin_agent", ""),
            "timestamp": _parse_dt(p.get("date", "")),
            "status": p.get("status", ""),
        })

    events.sort(key=lambda e: e["timestamp"], reverse=True)
    return events[:limit]


def collect_documents(g: dict) -> list[dict]:
    """Collect standalone documents from agent directories."""
    docs = []
    skip_dirs = {"observations", "proposals", "decisions", "prompts", "logs", "archive",
                 "ad-skills", "social-posts", "templates", "dashboard"}

    identity_files = {i.identity_filename() for i in REGISTRY.values()}

    for agent in g["agents"]:
        agent_dir = get_agent_dir(g, agent)
        if not agent_dir.exists():
            continue
        for f in sorted(agent_dir.rglob("*")):
            if f.is_dir():
                continue
            if f.name.startswith(".") or f.name in identity_files:
                continue
            rel = f.relative_to(agent_dir)
            if any(part in skip_dirs for part in rel.parts[:-1]):
                continue
            suffix = f.suffix.lower()
            if suffix in (".md", ".csv", ".html", ".txt", ".py"):
                docs.append({
                    "agent": agent,
                    "path": str(f),
                    "rel_path": str(rel),
                    "name": f.name,
                    "suffix": suffix,
                })

    # Also add shared standalone files
    shared = g["shared"]
    if shared.exists():
        for f in sorted(shared.iterdir()):
            if f.is_file() and f.suffix in (".md", ".html", ".csv") and f.name != "memory.md":
                docs.append({
                    "agent": "shared",
                    "path": str(f),
                    "rel_path": f.name,
                    "name": f.name,
                    "suffix": f.suffix.lower(),
                })

    return docs


def collect_logs(g: dict) -> dict[str, list[dict]]:
    """Collect log files grouped by date."""
    logs_dir = g["shared"] / "logs"
    if not logs_dir.exists():
        return {}
    result = {}
    for date_dir in sorted(logs_dir.iterdir(), reverse=True):
        if not date_dir.is_dir():
            continue
        entries = []
        for f in sorted(date_dir.iterdir(), reverse=True):
            if f.name.startswith("."):
                continue
            entries.append({
                "name": f.name,
                "path": str(f),
                "suffix": f.suffix,
                "size": f.stat().st_size,
            })
        if entries:
            result[date_dir.name] = entries
    return result


def infer_agent_from_prompt(filename: str, agents: list[str]) -> str | None:
    """Infer agent name from prompt filename by matching agent name prefix.

    E.g., 'product-routine.md' matches agent 'product',
    'business-ops-daily-close.md' matches 'business-ops'.
    Tries longest agent name first to handle hyphenated names correctly.
    """
    stem = filename.replace(".md", "")
    # Sort agents by length descending so 'business-ops' matches before 'business'
    for agent in sorted(agents, key=len, reverse=True):
        if stem == agent or stem.startswith(agent + "-"):
            return agent
    return None


def collect_prompts(g: dict) -> list[dict]:
    """List prompt files with dispatch assignment info."""
    prompts_dir = g["shared"] / "prompts"
    if not prompts_dir.exists():
        return []

    agents = g["agents"]

    # Build prompt-centric dispatch map from config
    group_cfg = GROUPS.get(g["key"], {})
    dispatch_agents = group_cfg.get("dispatch", {}).get("agents", {})
    # Invert: {prompt_filename: [{agent, type, value}, ...]}
    prompt_assignments: dict[str, list[dict]] = {}
    for agent_name, rules in dispatch_agents.items():
        for rule in rules:
            prompt_file = rule.get("prompt", "")
            if not prompt_file:
                continue
            if prompt_file not in prompt_assignments:
                prompt_assignments[prompt_file] = []
            entry = {"agent": agent_name, "condition": rule.get("condition", "")}
            if rule.get("at"):
                entry["type"] = "at"
                entry["value"] = rule["at"]
            elif rule.get("every"):
                entry["type"] = "every"
                entry["value"] = rule["every"]
            else:
                continue
            prompt_assignments[prompt_file].append(entry)

    items = []
    for f in sorted(prompts_dir.glob("*.md")):
        assignments = prompt_assignments.get(f.name, [])
        inferred_agent = infer_agent_from_prompt(f.name, agents)

        # If no explicit assignments but we can infer the agent, pre-populate
        # with an unscheduled placeholder so the UI shows the agent association
        if not assignments and inferred_agent:
            assignments = [{"agent": inferred_agent, "type": "", "value": ""}]

        items.append({
            "name": f.name,
            "path": str(f),
            "slug": f.stem,
            "assignments": assignments,
            "inferred_agent": inferred_agent,
        })
    return items


def collect_memory_files(g: dict) -> list[dict]:
    """Collect all memory.md files."""
    items = []
    # Shared memory
    sm = g["shared"] / "memory.md"
    if sm.exists():
        items.append({"agent": "shared", "path": str(sm), "name": "memory.md"})
    # Per-agent
    for agent in g["agents"]:
        mf = get_agent_dir(g, agent) / "memory.md"
        if mf.exists():
            items.append({"agent": agent, "path": str(mf), "name": "memory.md"})
    return items


def status_badge(status: str) -> Markup:
    """Return a colored badge for observation/proposal status."""
    colors = {
        "open": "bg-amber-100 text-amber-800",
        "connected": "bg-blue-100 text-blue-800",
        "investigating": "bg-purple-100 text-purple-800",
        "proposed": "bg-green-100 text-green-800",
        "decided": "bg-emerald-100 text-emerald-800",
        "dismissed": "bg-gray-100 text-gray-500",
        "archived": "bg-gray-100 text-gray-400",
    }
    cls = colors.get(status or "", "bg-gray-100 text-gray-600")
    return Markup(f'<span class="inline-block px-2 py-0.5 rounded-full text-xs font-medium {cls}">{status or "unknown"}</span>')


def agent_badge(agent: str) -> Markup:
    """Return a colored badge for agent name."""
    palette = [
        "bg-rose-100 text-rose-700",
        "bg-indigo-100 text-indigo-700",
        "bg-sky-100 text-sky-700",
        "bg-teal-100 text-teal-700",
        "bg-lime-100 text-lime-700",
        "bg-orange-100 text-orange-700",
        "bg-fuchsia-100 text-fuchsia-700",
        "bg-yellow-100 text-yellow-700",
        "bg-violet-100 text-violet-700",
        "bg-cyan-100 text-cyan-700",
        "bg-amber-100 text-amber-700",
        "bg-pink-100 text-pink-700",
        "bg-emerald-100 text-emerald-700",
        "bg-slate-100 text-slate-700",
    ]
    idx = hash(agent or "") % len(palette)
    cls = palette[idx]
    return Markup(f'<span class="inline-block px-2 py-0.5 rounded-full text-xs font-medium {cls}">{agent}</span>')


# Register template filters
templates.env.filters["status_badge"] = status_badge
templates.env.filters["agent_badge"] = agent_badge
templates.env.filters["render_md"] = render_md


# ── Agent Helpers ─────────────────────────────────────────────────────────────


def resolve_agent_dir(g: dict, agent_name: str) -> Path:
    """Find an agent's directory, checking root and _subagents/. Raises 404 if not found."""
    if "/" in agent_name or ".." in agent_name:
        raise HTTPException(400, "Invalid agent name")
    agent_dir = get_agent_dir(g, agent_name)
    if agent_dir.is_dir():
        return agent_dir
    sub_dir = g["path"] / "_subagents" / agent_name
    if sub_dir.is_dir():
        return sub_dir
    raise HTTPException(404, f"Agent not found: {agent_name}")


def parse_agent_identity(agent_dir: Path, integration=None) -> dict:
    """Read agent identity via integration. Falls back to claude-code."""
    if integration is None:
        integration = get_integration("claude-code")
    identity = integration.parse_identity(agent_dir)
    if identity is None:
        return {"display_name": agent_dir.name, "title": "", "emoji": "", "body": "", "frontmatter": {}}
    return {
        "display_name": identity.display_name or agent_dir.name,
        "title": identity.title or "",
        "emoji": identity.emoji or "",
        "body": identity.body,
        "frontmatter": {},
    }


def save_agent_identity(agent_dir: Path, fields: dict, integration=None) -> None:
    """Save identity fields via integration."""
    if integration is None:
        integration = get_integration("claude-code")
    from agency.integrations import AgentIdentity
    existing = integration.parse_identity(agent_dir)
    identity = AgentIdentity(
        display_name=fields.get("display_name") or (existing.display_name if existing else None),
        title=fields.get("title") or (existing.title if existing else None),
        emoji=fields.get("emoji") or (existing.emoji if existing else None),
        body=existing.body if existing else "",
    )
    integration.write_identity(agent_dir, identity)


def save_agent_definition(agent_dir: Path, new_body: str, integration=None) -> None:
    """Save agent definition body via integration."""
    if integration is None:
        integration = get_integration("claude-code")
    from agency.integrations import AgentIdentity
    existing = integration.parse_identity(agent_dir)
    identity = AgentIdentity(
        display_name=existing.display_name if existing else None,
        title=existing.title if existing else None,
        emoji=existing.emoji if existing else None,
        body=new_body,
    )
    integration.write_identity(agent_dir, identity)


def find_headshot(agent_dir: Path) -> Path | None:
    """Find headshot file by checking extensions in order."""
    for ext in ("png", "jpg", "jpeg", "webp"):
        p = agent_dir / f"headshot.{ext}"
        if p.exists():
            return p
    return None


def get_agent_last_seen(g: dict, agent_name: str) -> datetime | None:
    """Scan log date directories newest-first, return mtime of first matching file."""
    logs_dir = g["shared"] / "logs"
    if not logs_dir.exists():
        return None
    for date_dir in sorted(logs_dir.iterdir(), reverse=True):
        if not date_dir.is_dir():
            continue
        for f in sorted(date_dir.iterdir(), reverse=True):
            if f.name.startswith(f"{agent_name}-") and f.suffix in (".out", ".err"):
                return datetime.fromtimestamp(f.stat().st_mtime)
    return None


def relative_time(dt: datetime | None) -> str:
    """Format datetime as relative string."""
    if dt is None:
        return "No activity recorded"
    now = datetime.now()
    diff = now - dt
    seconds = int(diff.total_seconds())
    if seconds < 60:
        return "Just now"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    if days <= 30:
        return f"{days}d ago"
    return dt.strftime("%Y-%m-%d")


templates.env.filters["relative_time"] = relative_time


def integration_badge_filter(name: str) -> Markup:
    """Render a colored badge for an integration name."""
    colors = {
        "claude-code": "bg-orange-100 text-orange-800",
        "codex": "bg-green-100 text-green-800",
        "gemini": "bg-blue-100 text-blue-800",
        "aider": "bg-purple-100 text-purple-800",
        "goose": "bg-yellow-100 text-yellow-800",
        "script": "bg-gray-100 text-gray-800",
        "sdk": "bg-indigo-100 text-indigo-800",
    }
    color = colors.get(name, "bg-gray-100 text-gray-800")
    try:
        display = get_integration(name).display_name
    except KeyError:
        display = name
    return Markup(f'<span class="px-2 py-0.5 rounded-full text-xs font-medium {color}">{display}</span>')


templates.env.filters["integration_badge"] = integration_badge_filter


def agent_health_status(last_seen: datetime | None) -> str:
    """Return health status based on last seen time. green/amber/red."""
    if last_seen is None:
        return "red"
    hours = (datetime.now() - last_seen).total_seconds() / 3600
    if hours < 24:
        return "green"
    elif hours < 48:
        return "amber"
    return "red"


def collect_agents_with_identity(g: dict) -> tuple[list[dict], list[dict]]:
    """Build full agent info lists. Returns (agents, subagents)."""
    observations = list_observations(g)
    agents = []
    subagents = []

    for agent_name in g["agents"]:
        agent_dir = get_agent_dir(g, agent_name)
        if not agent_dir.is_dir():
            continue
        agent_int = get_agent_integration(g, agent_name)
        identity = parse_agent_identity(agent_dir, agent_int)
        open_count = sum(1 for c in observations if c.get("agent") == agent_name and c.get("status") == "open")
        last_seen = get_agent_last_seen(g, agent_name)
        info = {
            "name": agent_name, "dir": agent_dir, **identity,
            "last_seen": last_seen,
            "health": agent_health_status(last_seen),
            "open_observations": open_count,
            "is_subagent": identity["frontmatter"].get("subagent", False),
            "has_headshot": find_headshot(agent_dir) is not None,
            "integration": agent_int.name,
        }
        if info["is_subagent"]:
            subagents.append(info)
        else:
            agents.append(info)

    subagents_dir = g["path"] / "_subagents"
    if subagents_dir.is_dir():
        for d in sorted(subagents_dir.iterdir()):
            if not d.is_dir() or d.name.startswith("."):
                continue
            if any(s["name"] == d.name for s in subagents):
                continue
            sub_int = get_agent_integration(g, d.name)
            identity = parse_agent_identity(d, sub_int)
            open_count = sum(1 for c in observations if c.get("agent") == d.name and c.get("status") == "open")
            last_seen = get_agent_last_seen(g, d.name)
            subagents.append({
                "name": d.name, "dir": d, **identity,
                "last_seen": last_seen,
                "health": agent_health_status(last_seen),
                "open_observations": open_count, "is_subagent": True,
                "has_headshot": find_headshot(d) is not None,
                "integration": sub_int.name,
            })

    return agents, subagents


def get_agent_logs(g: dict, agent_name: str, limit: int = 20) -> list[dict]:
    """Get recent log files for an agent, newest first."""
    logs_dir = g["shared"] / "logs"
    if not logs_dir.exists():
        return []
    results = []
    for date_dir in sorted(logs_dir.iterdir(), reverse=True):
        if not date_dir.is_dir():
            continue
        for f in sorted(date_dir.iterdir(), reverse=True):
            if f.name.startswith(f"{agent_name}-") and f.suffix in (".out", ".err"):
                results.append({"name": f.name, "path": str(f), "date": date_dir.name, "size": f.stat().st_size, "suffix": f.suffix})
                if len(results) >= limit:
                    return results
    return results


def build_agent_timeline(g: dict, agent_name: str, agent_observations: list[dict] | None = None, limit: int = 30) -> list[dict]:
    """Build an interleaved timeline of logs and observations for an agent.
    Accepts precomputed agent_observations to avoid re-reading files."""
    events = []

    # Add logs
    logs_dir = g["shared"] / "logs"
    if logs_dir.exists():
        for date_dir in sorted(logs_dir.iterdir(), reverse=True):
            if not date_dir.is_dir():
                continue
            for f in sorted(date_dir.iterdir(), reverse=True):
                if f.name.startswith(f"{agent_name}-") and f.suffix in (".out", ".err"):
                    mtime = datetime.fromtimestamp(f.stat().st_mtime)
                    events.append({
                        "type": "log",
                        "timestamp": mtime,
                        "name": f.name,
                        "path": str(f),
                        "date": date_dir.name,
                        "size": f.stat().st_size,
                        "suffix": f.suffix,
                    })

    # Add observations from precomputed list
    for c in (agent_observations or []):
        obs_date = c.get("date")
        if isinstance(obs_date, str):
            try:
                obs_date = datetime.fromisoformat(obs_date).replace(tzinfo=None)
            except (ValueError, TypeError):
                obs_date = datetime.now()
        elif isinstance(obs_date, datetime):
            obs_date = obs_date.replace(tzinfo=None)
        else:
            obs_date = datetime.now()
        events.append({
            "type": "observation",
            "timestamp": obs_date,
            "slug": c.get("_slug", ""),
            "status": c.get("status", "open"),
            "body_preview": c.get("_body", "")[:120],
            "float": c.get("float", False),
        })

    events.sort(key=lambda e: e["timestamp"], reverse=True)
    return events[:limit]


# ── Routes ───────────────────────────────────────────────────────────────────


@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    """Redirect to the default group."""
    agency = get_agency_config()
    default = agency.get("default_group", list(GROUPS.keys())[0] if GROUPS else "")
    if default and default in GROUPS:
        return RedirectResponse(f"/{default}/", status_code=303)
    # Fallback to first group
    first = list(GROUPS.keys())[0] if GROUPS else ""
    if first:
        return RedirectResponse(f"/{first}/", status_code=303)
    return RedirectResponse("/setup", status_code=303)


# ── Setup Routes ─────────────────────────────────────────────────────────────


@app.get("/setup", response_class=HTMLResponse)
async def setup_page(request: Request):
    """First-run wizard page."""
    if GROUPS:
        return RedirectResponse("/", status_code=303)
    suggestion = str(Path.home() / "agents")
    return templates.TemplateResponse(request, "setup.html", {
        "request": request,
        "agency_title": get_agency_config().get("title", "Agency"),
        "suggestion": suggestion,
        "error": "",
        "path_value": "",
    })


@app.post("/setup", response_class=HTMLResponse)
async def setup_process(request: Request):
    """Process first-run setup: scan path, create group, initialize, redirect."""
    if GROUPS:
        return RedirectResponse("/", status_code=303)

    form = await request.form()
    path_str = form.get("path", "").strip()
    suggestion = str(Path.home() / "agents")
    agency_title = get_agency_config().get("title", "Agency")

    # Expand ~ and validate
    path = Path(path_str).expanduser()
    if not path.is_dir():
        return templates.TemplateResponse(request, "setup.html", {
            "request": request,
            "agency_title": agency_title,
            "suggestion": suggestion,
            "error": "That path doesn't exist or isn't a directory. Check the path and try again.",
            "path_value": path_str,
        })

    # Scan for agents
    detected = []
    for d in sorted(path.iterdir()):
        if d.is_dir() and d.name not in ("shared", "_subagents") and not d.name.startswith("."):
            if detect_integration(d):
                detected.append(d.name)

    if not detected:
        return templates.TemplateResponse(request, "setup.html", {
            "request": request,
            "agency_title": agency_title,
            "suggestion": suggestion,
            "error": 'No agents found at this path. Agency looks for subdirectories containing an agent definition file (CLAUDE.md, AGENTS.md, GEMINI.md, etc.). <a href="/admin/" class="underline">Set up manually in Settings</a>.',
            "path_value": path_str,
        })

    # Derive group key (deduplicate)
    base_key = path.name.lower().replace(" ", "-")
    key = base_key
    config = load_config()
    counter = 2
    while key in config.get("groups", {}):
        key = f"{base_key}-{counter}"
        counter += 1

    name = path.name.replace("-", " ").title()

    if "groups" not in config:
        config["groups"] = {}
    config["groups"][key] = {
        "name": name,
        "path": str(path),
        "agents": detected,
    }
    if "agency" not in config:
        config["agency"] = {}
    config["agency"]["default_group"] = key

    save_config(config)
    reload_groups()

    # Initialize shared folder structure
    shared = path / "shared"
    for subdir in ["observations", "proposals", "decisions", "prompts", "logs"]:
        (shared / subdir).mkdir(parents=True, exist_ok=True)
    memory_path = shared / "memory.md"
    if not memory_path.exists():
        memory_path.write_text(f"# {name} — Shared Memory\n\nCollective knowledge and decisions.\n")

    # Copy _observation-system-steps.md from an existing group if available
    observation_steps_target = shared / "prompts" / "_observation-system-steps.md"
    if not observation_steps_target.exists():
        for other_key, other_g in config.get("groups", {}).items():
            if other_key == key:
                continue
            source = Path(other_g["path"]) / "shared" / "prompts" / "_observation-system-steps.md"
            if source.exists():
                shutil.copy2(source, observation_steps_target)
                break

    for agent in detected:
        (path / agent).mkdir(exist_ok=True)

    return RedirectResponse(f"/setup/complete/{key}", status_code=303)


@app.get("/setup/complete/{group}", response_class=HTMLResponse)
async def setup_complete(request: Request, group: str):
    """Post-setup page — tells user to come back later."""
    agency_title = get_agency_config().get("title", "Agency")
    g = GROUPS.get(group)
    group_name = g["name"] if g else group
    return templates.TemplateResponse(request, "setup_complete.html", {
        "request": request,
        "agency_title": agency_title,
        "group": group,
        "group_name": group_name,
    })


# ── Tip Routes ────────────────────────────────────────────────────────────────


@app.post("/tips/dismiss", response_class=HTMLResponse)
async def tip_dismiss(request: Request):
    """Dismiss a specific tip card."""
    form = await request.form()
    tip_id = form.get("tip_id", "").strip()
    redirect = safe_redirect(form.get("redirect", "/"))

    if tip_id:
        config = load_config()
        if "agency" not in config:
            config["agency"] = {}
        dismissed = config["agency"].get("tips_dismissed", [])
        if tip_id not in dismissed:
            dismissed.append(tip_id)
            config["agency"]["tips_dismissed"] = dismissed
            save_config(config)
            reload_groups()

    return RedirectResponse(redirect, status_code=303)


@app.post("/tips/hide-all", response_class=HTMLResponse)
async def tip_hide_all(request: Request):
    """Hide all tip cards globally."""
    form = await request.form()
    redirect = safe_redirect(form.get("redirect", "/"))

    config = load_config()
    if "agency" not in config:
        config["agency"] = {}
    config["agency"]["show_tips"] = False
    save_config(config)
    reload_groups()

    return RedirectResponse(redirect, status_code=303)


# ── Admin Routes ──────────────────────────────────────────────────────────────


def admin_context(admin_page: str = "settings", dispatch_error: str = "") -> dict:
    """Build common context for admin pages."""
    config = load_config()
    agency = config.get("agency", {"title": "Agency", "default_group": ""})
    groups = config.get("groups", {})
    orgs = []
    for key, g in groups.items():
        org_path = Path(g["path"])
        shared_exists = (org_path / "shared").exists()
        path_exists = org_path.exists()
        dispatch_cfg = g.get("dispatch", {})
        orgs.append({
            "key": key,
            "name": g["name"],
            "path": g["path"],
            "agents": g.get("agents", []),
            "agent_count": len(g.get("agents", [])),
            "initialized": shared_exists,
            "path_exists": path_exists,
            "dispatch_enabled": dispatch_cfg.get("enabled", False),
        })
    return {
        "agency_title": agency.get("title", "Agency"),
        "default_group": agency.get("default_group", ""),
        "orgs": orgs,
        "groups": {k: v["name"] for k, v in groups.items()},
        "admin_active": True,
        "active": "admin",
        "admin_page": admin_page,
        "dispatch": get_dispatch_status(),
        "dispatch_error": dispatch_error,
        "theme_css": get_theme_css(),
    }


@app.get("/admin/", response_class=HTMLResponse)
async def admin_settings_page(request: Request):
    """Admin app settings page."""
    return templates.TemplateResponse(request, "admin_settings.html", {
        "request": request,
        **admin_context("settings"),
        "integrations": {name: i.display_name for name, i in REGISTRY.items() if i.supports_ai_backend},
        "ai_backend": get_agency_config()["ai_backend"],
        "installed_count": len(REGISTRY),
        "themes": load_themes(),
        "current_theme": get_agency_config()["theme"],
    })


def _read_integration_config():
    """Read integration module list from config."""
    from agency.integrations import _read_config
    return _read_config()


@app.get("/admin/integrations", response_class=HTMLResponse)
async def admin_integrations_page(request: Request):
    """Admin integrations management page."""
    from agency.integrations import scan_available

    config_modules = _read_integration_config()
    module_to_author = {}
    for mod in config_modules:
        parts = mod.split(".")
        if len(parts) == 2:
            module_to_author[parts[1]] = parts[0]

    installed = []
    for name, i in REGISTRY.items():
        module_name = name.replace("-", "_")
        author = module_to_author.get(module_name, "unknown")
        installed.append({
            "name": name,
            "display_name": i.display_name,
            "module_path": f"{author}.{module_name}",
            "supports_execution": i.supports_execution,
            "supports_ai_backend": i.supports_ai_backend,
            "identity_file": i.identity_filename() if hasattr(i, 'identity_filename') and callable(i.identity_filename) else "—",
            "author": author,
        })

    available = scan_available()

    return templates.TemplateResponse(request, "admin_integrations.html", {
        "request": request,
        **admin_context("integrations"),
        "installed": installed,
        "available": available,
        "restart_needed": request.query_params.get("restart") == "1",
    })


@app.post("/admin/integrations/register", response_class=HTMLResponse)
async def admin_integrations_register(request: Request):
    """Register an available integration."""
    from agency.integrations import register_integration
    form = await request.form()
    module_path = form.get("module_path", "")
    if module_path:
        register_integration(module_path)
    return RedirectResponse("/admin/integrations?restart=1", status_code=303)


@app.post("/admin/integrations/unregister", response_class=HTMLResponse)
async def admin_integrations_unregister(request: Request):
    """Unregister an installed integration."""
    from agency.integrations import unregister_integration
    form = await request.form()
    module_path = form.get("module_path", "")
    if module_path:
        unregister_integration(module_path)
    return RedirectResponse("/admin/integrations?restart=1", status_code=303)


@app.post("/admin/integrations/restart", response_class=HTMLResponse)
async def admin_integrations_restart(request: Request):
    """Restart the agency service to apply integration changes."""
    try:
        subprocess.Popen(["systemctl", "--user", "restart", "agency.service"])
    except Exception:
        pass
    return RedirectResponse("/admin/integrations", status_code=303)


@app.get("/admin/dispatch", response_class=HTMLResponse)
async def admin_dispatch_page(request: Request):
    """Admin dispatch configuration page."""
    return templates.TemplateResponse(request, "admin_dispatch.html", {
        "request": request,
        **admin_context("dispatch"),
    })


@app.get("/admin/groups", response_class=HTMLResponse)
async def admin_groups_page(request: Request):
    """Admin agent groups page."""
    return templates.TemplateResponse(request, "admin_groups.html", {
        "request": request,
        **admin_context("groups"),
    })


@app.post("/admin/settings", response_class=HTMLResponse)
async def admin_save_settings(request: Request):
    """Save agency-level settings."""
    form = await request.form()
    title = form.get("title", "Agency").strip()
    default_group = form.get("default_group", "").strip()

    config = load_config()
    if "agency" not in config:
        config["agency"] = {}
    config["agency"]["title"] = title or "Agency"
    if default_group:
        config["agency"]["default_group"] = default_group

    ai_backend = form.get("ai_backend", "claude-code")
    config["agency"]["ai_backend"] = ai_backend

    theme = form.get("theme", "").strip()
    config["agency"]["theme"] = theme
    _THEME_CSS_CACHE.clear()  # Invalidate cached CSS

    # Handle dispatch interval update
    dispatch_interval_raw = form.get("dispatch_interval", "")
    if dispatch_interval_raw:
        try:
            new_interval = int(dispatch_interval_raw)
            if 5 <= new_interval <= 120:
                if "dispatch" not in config["agency"]:
                    config["agency"]["dispatch"] = {}
                old_interval = config["agency"]["dispatch"].get("interval", 15)
                config["agency"]["dispatch"]["interval"] = new_interval
                # If interval changed and dispatch is installed, reload timer
                if new_interval != old_interval and config["agency"]["dispatch"].get("installed", False):
                    subprocess.run(
                        ["systemctl", "--user", "daemon-reload"],
                        capture_output=True, text=True, timeout=10,
                    )
                    subprocess.run(
                        ["systemctl", "--user", "restart", "agency-dispatch.timer"],
                        capture_output=True, text=True, timeout=10,
                    )
        except (ValueError, TypeError):
            pass

    save_config(config)
    reload_groups()
    # Redirect back to dispatch page if interval was changed, otherwise settings
    redirect = "/admin/dispatch" if dispatch_interval_raw else "/admin/"
    return RedirectResponse(redirect, status_code=303)


@app.post("/admin/dispatch/install", response_class=HTMLResponse)
async def admin_dispatch_install(request: Request):
    """Install the dispatch systemd timer."""
    error = install_dispatch()
    if error:
        return templates.TemplateResponse(request, "admin_dispatch.html", {
            "request": request,
            **admin_context("dispatch", dispatch_error=error),
        })
    return RedirectResponse("/admin/dispatch", status_code=303)


@app.get("/admin/orgs/new", response_class=HTMLResponse)
async def admin_org_new(request: Request):
    """Create new org form."""
    agency = get_agency_config()
    config = load_config()
    return templates.TemplateResponse(request, "admin_org_edit.html", {
        "request": request,
        "agency_title": agency.get("title", "Agency"),
        "admin_active": True,
        "active": "admin",
        "admin_page": "groups",
        "groups": {k: v["name"] for k, v in config.get("groups", {}).items()},
        "mode": "create",
        "org_key": "",
        "org_name": "",
        "org_path": "",
        "org_agents": "",
        "org_workspaces_json": json_module.dumps([]),
        "workspace_types_json": _workspace_types_json(),
        "agent_infos": [],
        "warning": "",
    })


@app.post("/admin/orgs/create", response_class=HTMLResponse)
async def admin_org_create(request: Request):
    """Create a new org."""
    form = await request.form()
    key = form.get("key", "").strip().lower().replace(" ", "-")
    name = form.get("name", "").strip()
    path = form.get("path", "").strip()
    agents_raw = form.get("agents", "").strip()
    agents = [a.strip() for a in agents_raw.splitlines() if a.strip()]
    workspaces_json = form.get("workspaces_json", "[]")
    try:
        ws_list = json_module.loads(workspaces_json)
    except (json_module.JSONDecodeError, TypeError):
        ws_list = []

    if not key or not name or not path:
        agency = get_agency_config()
        config = load_config()
        return templates.TemplateResponse(request, "admin_org_edit.html", {
            "request": request,
            "agency_title": agency.get("title", "Agency"),
            "admin_active": True,
            "active": "admin",
            "admin_page": "groups",
            "groups": {k: v["name"] for k, v in config.get("groups", {}).items()},
            "mode": "create",
            "org_key": key,
            "org_name": name,
            "org_path": path,
            "org_agents": agents_raw,
            "org_workspaces_json": json_module.dumps(ws_list),
            "workspace_types_json": _workspace_types_json(),
            "agent_infos": [],
            "warning": "Key, name, and path are required.",
        })

    config = load_config()
    if "groups" not in config:
        config["groups"] = {}

    warning = ""
    if not Path(path).exists():
        warning = f"Warning: Path {path} does not exist on disk. You can create it later via Initialize."

    group_cfg = {
        "name": name,
        "path": path,
        "agents": agents,
    }
    if ws_list:
        group_cfg["workspaces"] = ws_list
    config["groups"][key] = group_cfg

    save_config(config)
    reload_groups()

    if warning:
        agency = get_agency_config()
        return templates.TemplateResponse(request, "admin_org_edit.html", {
            "request": request,
            "agency_title": agency.get("title", "Agency"),
            "admin_active": True,
            "active": "admin",
            "groups": {k: v["name"] for k, v in config.get("groups", {}).items()},
            "mode": "edit",
            "org_key": key,
            "org_name": name,
            "org_path": path,
            "org_agents": "\n".join(agents),
            "org_workspaces_json": json_module.dumps(ws_list),
            "workspace_types_json": _workspace_types_json(),
            "agent_infos": [get_agent_info(Path(path), a) for a in agents] if Path(path).exists() else [],
            "warning": warning + " Org saved successfully.",
        })

    return RedirectResponse("/admin/groups", status_code=303)


@app.get("/admin/orgs/{org}/edit", response_class=HTMLResponse)
async def admin_org_edit(request: Request, org: str):
    """Edit org form."""
    config = load_config()
    groups = config.get("groups", {})
    if org not in groups:
        raise HTTPException(404, f"Unknown org: {org}")

    g = groups[org]
    agency = get_agency_config()
    base = Path(g["path"])

    # Build rich agent info
    grp_full = {"path": base, "agents_full": GROUPS.get(org, {}).get("_agents_normalized", [])}
    _agent_names = [a if isinstance(a, str) else a.get("name", "") for a in g.get("agents", []) if a]
    agent_infos = [get_agent_info(base, a, agent_dir=get_agent_dir(grp_full, a)) for a in _agent_names]

    # Dispatch config for this group
    dispatch_cfg = g.get("dispatch", {})
    prompts = []
    prompts_dir = Path(g["path"]) / "shared" / "prompts"
    if prompts_dir.exists():
        prompts = sorted(f.name for f in prompts_dir.glob("*.md"))

    return templates.TemplateResponse(request, "admin_org_edit.html", {
        "request": request,
        "agency_title": agency.get("title", "Agency"),
        "admin_active": True,
        "active": "admin",
        "admin_page": "groups",
        "groups": {k: v["name"] for k, v in groups.items()},
        "mode": "edit",
        "org_key": org,
        "org_name": g["name"],
        "org_path": g["path"],
        "org_agents": "\n".join(_agent_names),
        "org_workspaces_json": json_module.dumps(g.get("workspaces", [])),
        "workspace_types_json": _workspace_types_json(),
        "agent_infos": agent_infos,
        "dispatch_enabled": dispatch_cfg.get("enabled", False),
        "dispatch_timeout": dispatch_cfg.get("timeout", 1800),
        "dispatch_daily_limit": dispatch_cfg.get("daily_limit", 20),
        "dispatch_agents": dispatch_cfg.get("agents", {}),
        "dispatch_installed": CONFIG.get("agency", {}).get("dispatch", {}).get("installed", False),
        "available_prompts": prompts,
        "all_integrations": {name: i.display_name for name, i in REGISTRY.items()},
        "default_integration": g.get("default_integration", "claude-code"),
        "warning": "",
    })


@app.post("/admin/orgs/{org}/save", response_class=HTMLResponse)
async def admin_org_save(request: Request, org: str):
    """Save org changes."""
    form = await request.form()
    name = form.get("name", "").strip()
    path = form.get("path", "").strip()
    agents_raw = form.get("agents", "").strip()
    agents = [a.strip() for a in agents_raw.splitlines() if a.strip()]
    workspaces_json = form.get("workspaces_json", "[]")
    try:
        ws_list = json_module.loads(workspaces_json)
    except (json_module.JSONDecodeError, TypeError):
        ws_list = []

    config = load_config()
    if org not in config.get("groups", {}):
        raise HTTPException(404, f"Unknown org: {org}")

    warning = ""
    if path and not Path(path).exists():
        warning = f"Warning: Path {path} does not exist on disk."

    config["groups"][org]["name"] = name or config["groups"][org]["name"]
    if path:
        config["groups"][org]["path"] = path
    config["groups"][org]["agents"] = agents
    if ws_list:
        config["groups"][org]["workspaces"] = ws_list
    elif "workspaces" in config["groups"].get(org, {}):
        del config["groups"][org]["workspaces"]
    config["groups"][org].pop("tmux_config", None)

    default_integration = form.get("default_integration", "claude-code")
    config["groups"][org]["default_integration"] = default_integration

    save_config(config)
    reload_groups()

    if warning:
        agency = get_agency_config()
        return templates.TemplateResponse(request, "admin_org_edit.html", {
            "request": request,
            "agency_title": agency.get("title", "Agency"),
            "admin_active": True,
            "active": "admin",
            "groups": {k: v["name"] for k, v in config.get("groups", {}).items()},
            "mode": "edit",
            "org_key": org,
            "org_name": config["groups"][org]["name"],
            "org_path": config["groups"][org]["path"],
            "org_agents": "\n".join(agents),
            "org_workspaces_json": json_module.dumps(ws_list),
            "workspace_types_json": _workspace_types_json(),
            "agent_infos": [get_agent_info(Path(config["groups"][org]["path"]), a) for a in agents],
            "dispatch_enabled": config["groups"][org].get("dispatch", {}).get("enabled", False),
            "dispatch_timeout": config["groups"][org].get("dispatch", {}).get("timeout", 1800),
            "dispatch_daily_limit": config["groups"][org].get("dispatch", {}).get("daily_limit", 20),
            "dispatch_agents": config["groups"][org].get("dispatch", {}).get("agents", {}),
            "dispatch_installed": CONFIG.get("agency", {}).get("dispatch", {}).get("installed", False),
            "available_prompts": sorted(f.name for f in (Path(path) / "shared" / "prompts").glob("*.md")) if (Path(path) / "shared" / "prompts").exists() else [],
            "warning": warning + " Changes saved.",
        })

    return RedirectResponse("/admin/", status_code=303)


@app.post("/admin/orgs/{org}/dispatch", response_class=HTMLResponse)
async def admin_org_dispatch_save(request: Request, org: str):
    """Save dispatch config for an org."""
    config = load_config()
    if org not in config.get("groups", {}):
        raise HTTPException(404, f"Unknown org: {org}")

    form = await request.form()
    enabled = form.get("enabled") == "on"
    timeout = int(form.get("timeout", 1800))
    daily_limit = int(form.get("daily_limit", 20))

    g = config["groups"][org]
    agents_list = g.get("agents", [])

    agents_dispatch = {}
    for agent in agents_list:
        rules = []
        idx = 0
        while True:
            rule_type = form.get(f"rule_type_{agent}_{idx}")
            if rule_type is None:
                break
            rule_value = form.get(f"rule_value_{agent}_{idx}", "").strip()
            rule_prompt = form.get(f"rule_prompt_{agent}_{idx}", "").strip()
            if rule_value and rule_prompt:
                rule = {"prompt": rule_prompt}
                if rule_type == "at":
                    rule["at"] = rule_value
                else:
                    rule["every"] = rule_value
                rules.append(rule)
            idx += 1
        if rules:
            agents_dispatch[agent] = rules

    config["groups"][org]["dispatch"] = {
        "enabled": enabled,
        "timeout": timeout,
        "daily_limit": daily_limit,
        "agents": agents_dispatch,
    }

    save_config(config)
    reload_groups()
    return RedirectResponse(f"/admin/orgs/{org}/edit", status_code=303)


@app.post("/admin/orgs/{org}/delete", response_class=HTMLResponse)
async def admin_org_delete(request: Request, org: str):
    """Remove org from config (does not delete files on disk)."""
    config = load_config()
    if org not in config.get("groups", {}):
        raise HTTPException(404, f"Unknown org: {org}")

    del config["groups"][org]

    # If the deleted group was the default, update default
    agency = config.get("agency", {})
    if agency.get("default_group") == org:
        remaining = list(config.get("groups", {}).keys())
        agency["default_group"] = remaining[0] if remaining else ""
        config["agency"] = agency

    save_config(config)
    reload_groups()
    return RedirectResponse("/admin/groups", status_code=303)


@app.post("/admin/orgs/{org}/initialize", response_class=HTMLResponse)
async def admin_org_initialize(request: Request, org: str):
    """Create the folder structure for an org. Idempotent."""
    config = load_config()
    if org not in config.get("groups", {}):
        raise HTTPException(404, f"Unknown org: {org}")

    g = config["groups"][org]
    base = Path(g["path"])

    # Create base dir if needed
    base.mkdir(parents=True, exist_ok=True)

    # Create shared structure
    shared = base / "shared"
    for subdir in ["observations", "proposals", "decisions", "prompts", "logs"]:
        (shared / subdir).mkdir(parents=True, exist_ok=True)

    # Create shared memory.md if it doesn't exist
    memory_path = shared / "memory.md"
    if not memory_path.exists():
        memory_path.write_text(f"# {g['name']} — Shared Memory\n\nCollective knowledge and decisions.\n")

    # Copy _observation-system-steps.md from an existing group if available
    observation_steps_target = shared / "prompts" / "_observation-system-steps.md"
    if not observation_steps_target.exists():
        # Try to find an existing one to copy
        for other_key, other_g in config.get("groups", {}).items():
            if other_key == org:
                continue
            source = Path(other_g["path"]) / "shared" / "prompts" / "_observation-system-steps.md"
            if source.exists():
                shutil.copy2(source, observation_steps_target)
                break

    # Create agent directories (skip shared agents with external paths)
    raw_agents = g.get("agents", [])
    for agent in raw_agents:
        name = agent if isinstance(agent, str) else agent.get("name", "")
        if name and not is_shared_agent(raw_agents, name):
            (base / name).mkdir(exist_ok=True)

    return RedirectResponse("/admin/groups", status_code=303)


@app.post("/admin/orgs/{org}/autodetect", response_class=HTMLResponse)
async def admin_org_autodetect(request: Request, org: str):
    """Auto-detect agents by scanning for directories with recognized definition files."""
    config = load_config()
    if org not in config.get("groups", {}):
        raise HTTPException(404, f"Unknown org: {org}")

    g = config["groups"][org]
    base = Path(g["path"])
    agency = get_agency_config()

    detected = []
    if base.exists():
        for d in sorted(base.iterdir()):
            if d.is_dir() and d.name != "shared" and not d.name.startswith("."):
                if detect_integration(d):
                    detected.append(d.name)

    # Update config with detected agents
    raw_agents = detected if detected else g.get("agents", [])
    if detected:
        config["groups"][org]["agents"] = detected
        save_config(config)
        reload_groups()

    grp_full = {"path": base, "agents_full": GROUPS.get(org, {}).get("_agents_normalized", [])}
    _agent_names = [a if isinstance(a, str) else a.get("name", "") for a in raw_agents if a]
    agent_infos = [get_agent_info(base, a, agent_dir=get_agent_dir(grp_full, a)) for a in _agent_names]

    # Gather dispatch + prompt context (same as admin_org_edit)
    dispatch_cfg = g.get("dispatch", {})
    prompts = []
    prompts_dir = Path(g["path"]) / "shared" / "prompts"
    if prompts_dir.exists():
        prompts = sorted(f.name for f in prompts_dir.glob("*.md"))

    return templates.TemplateResponse(request, "admin_org_edit.html", {
        "request": request,
        "agency_title": agency.get("title", "Agency"),
        "admin_active": True,
        "active": "admin",
        "admin_page": "groups",
        "groups": {k: v["name"] for k, v in config.get("groups", {}).items()},
        "mode": "edit",
        "org_key": org,
        "org_name": g["name"],
        "org_path": g["path"],
        "org_workspaces_json": json_module.dumps(g.get("workspaces", [])),
        "workspace_types_json": _workspace_types_json(),
        "org_agents": "\n".join(_agent_names),
        "agent_infos": agent_infos,
        "dispatch_enabled": dispatch_cfg.get("enabled", False),
        "dispatch_timeout": dispatch_cfg.get("timeout", 1800),
        "dispatch_daily_limit": dispatch_cfg.get("daily_limit", 20),
        "dispatch_agents": dispatch_cfg.get("agents", {}),
        "dispatch_installed": CONFIG.get("agency", {}).get("dispatch", {}).get("installed", False),
        "available_prompts": prompts,
        "all_integrations": {name: i.display_name for name, i in REGISTRY.items()},
        "default_integration": g.get("default_integration", "claude-code"),
        "warning": f"Auto-detected {len(detected)} agents." if detected else "No agents with recognized definition files found in path.",
    })


# ── Agent CRUD Routes ────────────────────────────────────────────────────────


def get_agent_info(base: Path, agent_name: str, agent_dir: Path | None = None) -> dict:
    """Gather filesystem info about an individual agent."""
    if agent_dir is None:
        agent_dir = base / agent_name
    info = {
        "name": agent_name,
        "dir_exists": agent_dir.is_dir(),
        "has_definition": any(
            (agent_dir / i.identity_filename()).exists()
            for i in REGISTRY.values()
        ) if agent_dir.is_dir() else False,
        "has_memory": (agent_dir / "memory.md").exists(),
        "has_mcp": (agent_dir / ".mcp.json").exists(),
        "files": [],
    }
    if agent_dir.is_dir():
        info["files"] = sorted(f.name for f in agent_dir.iterdir() if f.is_file())
    return info


@app.get("/admin/orgs/{org}/agents/{agent}", response_class=HTMLResponse)
async def admin_agent_detail(request: Request, org: str, agent: str):
    """View/edit an individual agent."""
    config = load_config()
    groups = config.get("groups", {})
    if org not in groups:
        raise HTTPException(404, f"Unknown org: {org}")

    g = groups[org]
    base = Path(g["path"])
    agency = get_agency_config()

    idx, _ = find_agent_in_config(g.get("agents", []), agent)
    if idx < 0:
        raise HTTPException(404, f"Agent '{agent}' not in group '{org}'")

    grp_full = {"path": base, "agents_full": GROUPS.get(org, {}).get("_agents_normalized", [])}
    agent_dir = get_agent_dir(grp_full, agent)
    agent_info = get_agent_info(base, agent, agent_dir=agent_dir)

    # Read editable files
    definition_content = ""
    memory_md = ""
    if agent_dir.is_dir():
        agent_integration = get_agent_integration(grp_full, agent)
        identity_path = agent_dir / agent_integration.identity_filename()
        if identity_path.exists():
            definition_content = identity_path.read_text()
        memory_path = agent_dir / "memory.md"
        if memory_path.exists():
            memory_md = memory_path.read_text()

    return templates.TemplateResponse(request, "admin_agent_detail.html", {
        "request": request,
        "agency_title": agency.get("title", "Agency"),
        "admin_active": True,
        "active": "admin",
        "admin_page": "groups",
        "groups": {k: v["name"] for k, v in groups.items()},
        "org_key": org,
        "org_name": g["name"],
        "agent": agent_info,
        "claude_md": definition_content,
        "definition_content": definition_content,
        "memory_md": memory_md,
        "warning": "",
    })


@app.post("/admin/orgs/{org}/agents/{agent}/save", response_class=HTMLResponse)
async def admin_agent_save(request: Request, org: str, agent: str):
    """Save agent definition file and/or memory.md."""
    config = load_config()
    groups = config.get("groups", {})
    if org not in groups:
        raise HTTPException(404, f"Unknown org: {org}")

    g = groups[org]
    base = Path(g["path"])
    grp_full = {"path": base, "agents_full": GROUPS.get(org, {}).get("_agents_normalized", [])}
    agent_dir = get_agent_dir(grp_full, agent)

    # Security: validate path is within allowed roots
    validate_file_access(agent_dir, base, allowed_roots=get_allowed_roots(grp_full))

    form = await request.form()
    file_type = form.get("file_type", "claude_md")
    content = form.get("content", "")

    # Create agent dir if it doesn't exist
    agent_dir.mkdir(parents=True, exist_ok=True)

    if file_type == "claude_md" or file_type == "definition":
        # Detect integration from existing files, fall back to config
        agent_int = get_agent_integration(grp_full, agent)
        save_agent_definition(agent_dir, content, agent_int)
    elif file_type == "memory_md":
        (agent_dir / "memory.md").write_text(content)

    # Persist per-agent integration if submitted
    integration = form.get("integration", "")
    if integration:
        config = load_config()
        agents = config["groups"][org].get("agents", [])
        for i, a in enumerate(agents):
            name = a if isinstance(a, str) else a.get("name", "")
            if name == agent:
                default_int = config["groups"][org].get("default_integration", "claude-code")
                if integration != default_int:
                    agents[i] = {"name": agent, "integration": integration}
                else:
                    agents[i] = agent  # Use shorthand if matches default
                break
        config["groups"][org]["agents"] = agents
        save_config(config)
        reload_groups()

    return RedirectResponse(f"/admin/orgs/{org}/agents/{agent}", status_code=303)


@app.post("/admin/orgs/{org}/agents/create", response_class=HTMLResponse)
async def admin_agent_create(request: Request, org: str):
    """Add a new agent to a group."""
    config = load_config()
    if org not in config.get("groups", {}):
        raise HTTPException(404, f"Unknown org: {org}")

    form = await request.form()
    agent_name = form.get("name", "").strip().lower().replace(" ", "-")
    agent_name = re.sub(r"[^a-z0-9\-]", "", agent_name)

    if not agent_name:
        return RedirectResponse(f"/admin/orgs/{org}/edit", status_code=303)

    g = config["groups"][org]
    agents = g.get("agents", [])

    idx, _ = find_agent_in_config(agents, agent_name)
    if idx < 0:
        agents.append(agent_name)
        config["groups"][org]["agents"] = agents
        save_config(config)
        reload_groups()

    # Create directory + scaffold
    base = Path(g["path"])
    agent_dir = base / agent_name
    agent_dir.mkdir(parents=True, exist_ok=True)
    # Use group's default integration to determine identity file
    default_int = g.get("default_integration", "claude-code")
    integration = get_integration(default_int)
    identity_file = agent_dir / integration.identity_filename()
    if not identity_file.exists():
        identity_file.write_text(f"# {agent_name.replace('-', ' ').title()} Agent\n\nRole definition goes here.\n")
    memory_path = agent_dir / "memory.md"
    if not memory_path.exists():
        memory_path.write_text(f"# {agent_name.replace('-', ' ').title()} Memory\n\n")

    return RedirectResponse(f"/admin/orgs/{org}/edit", status_code=303)


@app.post("/admin/orgs/{org}/agents/{agent}/rename", response_class=HTMLResponse)
async def admin_agent_rename(request: Request, org: str, agent: str):
    """Rename an agent (config + directory)."""
    config = load_config()
    if org not in config.get("groups", {}):
        raise HTTPException(404, f"Unknown org: {org}")

    form = await request.form()
    new_name = form.get("new_name", "").strip().lower().replace(" ", "-")
    new_name = re.sub(r"[^a-z0-9\-]", "", new_name)

    if not new_name or new_name == agent:
        return RedirectResponse(f"/admin/orgs/{org}/agents/{agent}", status_code=303)

    g = config["groups"][org]
    agents = g.get("agents", [])
    base = Path(g["path"])

    # Update config
    idx, entry = find_agent_in_config(agents, agent)
    if idx >= 0:
        if isinstance(entry, dict):
            agents[idx] = {**entry, "name": new_name}
        else:
            agents[idx] = new_name
        config["groups"][org]["agents"] = agents
        save_config(config)
        reload_groups()

    # Skip directory rename for shared agents (external path)
    if not is_shared_agent(agents, new_name):
        old_dir = base / agent
        new_dir = base / new_name
        # Confine both paths to the group root (agent comes from the URL, unsanitized) —
        # mirrors the guard in admin_agent_delete.
        validate_file_access(old_dir, base)
        validate_file_access(new_dir, base)
        if old_dir.is_dir() and not new_dir.exists():
            old_dir.rename(new_dir)

    return RedirectResponse(f"/admin/orgs/{org}/agents/{new_name}", status_code=303)


@app.post("/admin/orgs/{org}/agents/{agent}/delete", response_class=HTMLResponse)
async def admin_agent_delete(request: Request, org: str, agent: str):
    """Remove an agent from config. Optionally delete directory."""
    config = load_config()
    if org not in config.get("groups", {}):
        raise HTTPException(404, f"Unknown org: {org}")

    form = await request.form()
    delete_files = form.get("delete_files", "") == "true"

    g = config["groups"][org]
    agents = g.get("agents", [])

    # Remove from config
    shared = is_shared_agent(agents, agent)
    idx, _ = find_agent_in_config(agents, agent)
    if idx >= 0:
        agents.pop(idx)
        config["groups"][org]["agents"] = agents
        save_config(config)
        reload_groups()

    # Optionally delete directory (skip shared agents — external path)
    if delete_files and not shared:
        agent_dir = Path(g["path"]) / agent
        validate_file_access(agent_dir, Path(g["path"]))
        if agent_dir.is_dir():
            shutil.rmtree(agent_dir)

    return RedirectResponse(f"/admin/orgs/{org}/edit", status_code=303)


# ── Group Routes ──────────────────────────────────────────────────────────────


@app.get("/{group}/agents", response_class=HTMLResponse)
async def agents_list(request: Request, group: str):
    """List all agents with identity and health info."""
    g = get_group(group)
    agents, subagents = collect_agents_with_identity(g)
    return templates.TemplateResponse(request, "agents.html", {
        "request": request,
        **group_context(g),
        "agents": agents,
        "subagents": subagents,
    })


@app.get("/{group}/agents/{agent}", response_class=HTMLResponse)
async def agent_profile(request: Request, group: str, agent: str):
    """View an agent's profile with identity, logs, observations, and memory."""
    g = get_group(group)
    agent_dir = resolve_agent_dir(g, agent)
    agent_int = get_agent_integration(g, agent)
    identity = parse_agent_identity(agent_dir, agent_int)
    is_subagent = (g["path"] / "_subagents" / agent).is_dir() or identity["frontmatter"].get("subagent", False)
    last_seen = get_agent_last_seen(g, agent)
    all_observations = list_observations(g)
    agent_observations = [c for c in all_observations if c.get("agent") == agent]
    timeline = build_agent_timeline(g, agent, agent_observations=agent_observations)
    has_headshot = find_headshot(agent_dir) is not None
    has_memory = (agent_dir / "memory.md").exists()
    memory_path = str(agent_dir / "memory.md") if has_memory else ""

    # Get dispatch schedule for this agent
    group_cfg = GROUPS.get(g["key"], {})
    dispatch_cfg = group_cfg.get("dispatch", {})
    agent_schedule = dispatch_cfg.get("agents", {}).get(agent, [])
    dispatch_enabled = dispatch_cfg.get("enabled", False)

    return templates.TemplateResponse(request, "agent_profile.html", {
        "request": request,
        **group_context(g),
        "agent": agent,
        "identity": identity,
        "is_subagent": is_subagent,
        "last_seen": last_seen,
        "timeline": timeline,
        "has_headshot": has_headshot,
        "has_memory": has_memory,
        "memory_path": memory_path,
        "agent_schedule": agent_schedule,
        "dispatch_enabled": dispatch_enabled,
        "agent_integration": agent_int.name,
    })


@app.post("/{group}/agents/{agent}/identity", response_class=HTMLResponse)
async def agent_save_identity(request: Request, group: str, agent: str):
    """Save identity fields via detected integration."""
    g = get_group(group)
    agent_dir = resolve_agent_dir(g, agent)
    agent_int = get_agent_integration(g, agent)
    form = await request.form()
    fields = {
        "display_name": form.get("display_name", "").strip(),
        "title": form.get("title", "").strip(),
        "emoji": form.get("emoji", "").strip(),
    }
    save_agent_identity(agent_dir, fields, integration=agent_int)
    return RedirectResponse(f"/{group}/agents/{agent}", status_code=303)


@app.post("/{group}/agents/{agent}/definition", response_class=HTMLResponse)
async def agent_save_definition(request: Request, group: str, agent: str):
    """Save agent definition body preserving frontmatter."""
    g = get_group(group)
    agent_dir = resolve_agent_dir(g, agent)
    agent_int = get_agent_integration(g, agent)
    form = await request.form()
    body = form.get("body", "")
    save_agent_definition(agent_dir, body, integration=agent_int)
    return RedirectResponse(f"/{group}/agents/{agent}", status_code=303)


@app.post("/{group}/agents/{agent}/upload-headshot", response_class=HTMLResponse)
async def agent_upload_headshot(request: Request, group: str, agent: str):
    """Upload a headshot image for an agent."""
    g = get_group(group)
    agent_dir = resolve_agent_dir(g, agent)
    form = await request.form()
    upload = form.get("headshot")
    if not upload or not hasattr(upload, 'filename') or not upload.filename:
        return RedirectResponse(f"/{group}/agents/{agent}", status_code=303)
    ext = Path(upload.filename).suffix.lower().lstrip(".")
    if ext not in ("png", "jpg", "jpeg", "webp"):
        raise HTTPException(400, "Invalid image format. Use PNG, JPG, or WebP.")
    content = await upload.read()
    if len(content) > 2 * 1024 * 1024:
        raise HTTPException(400, "Image too large. Maximum 2MB.")
    # Remove any existing headshots
    for old_ext in ("png", "jpg", "jpeg", "webp"):
        old = agent_dir / f"headshot.{old_ext}"
        if old.exists():
            old.unlink()
    (agent_dir / f"headshot.{ext}").write_bytes(content)
    return RedirectResponse(f"/{group}/agents/{agent}", status_code=303)


@app.get("/{group}/agents/{agent}/headshot")
async def agent_headshot(group: str, agent: str):
    """Serve an agent's headshot image."""
    g = get_group(group)
    agent_dir = resolve_agent_dir(g, agent)
    headshot = find_headshot(agent_dir)
    if not headshot:
        raise HTTPException(404, "No headshot")
    return FileResponse(headshot)


@app.post("/{group}/agents/{agent}/toggle-subagent", response_class=HTMLResponse)
async def agent_toggle_subagent(request: Request, group: str, agent: str):
    """Toggle an agent between regular and subagent status."""
    g = get_group(group)
    if "/" in agent or ".." in agent:
        raise HTTPException(400, "Invalid agent name")
    root_dir = get_agent_dir(g, agent)
    sub_dir = g["path"] / "_subagents" / agent
    is_currently_subagent = sub_dir.is_dir()

    config = load_config()
    group_config = config["groups"][g["key"]]

    if is_currently_subagent:
        if root_dir.exists():
            raise HTTPException(409, f"Cannot move: {root_dir} already exists")
        shutil.move(str(sub_dir), str(root_dir))
        agents = group_config.get("agents", [])
        idx, _ = find_agent_in_config(agents, agent)
        if idx < 0:
            group_config.setdefault("agents", []).append(agent)
    else:
        if not root_dir.is_dir():
            raise HTTPException(404, f"Agent directory not found: {agent}")
        if sub_dir.exists():
            raise HTTPException(409, f"Cannot move: {sub_dir} already exists")
        (g["path"] / "_subagents").mkdir(exist_ok=True)
        shutil.move(str(root_dir), str(sub_dir))
        agents = group_config.get("agents", [])
        idx, _ = find_agent_in_config(agents, agent)
        if idx >= 0:
            agents.pop(idx)
            group_config["agents"] = agents

    save_config(config)
    reload_groups()
    return RedirectResponse(f"/{group}/agents/{agent}", status_code=303)


@app.get("/{group}/", response_class=HTMLResponse)
async def home(request: Request, group: str):
    """Dashboard home — mission control."""
    g = get_group(group)
    observations = list_observations(g)
    proposals = list_proposals(g)
    decisions = list_decisions(g)

    open_observations = [c for c in observations if c.get("status") in ("open",)]
    floated_observations = [c for c in observations if c.get("float")]
    actionable_proposals = [c for c in proposals if c.get("status") in ("proposed", "investigating")]

    floated_open_observations = [c for c in observations if c.get("float") and c.get("status") == "open"]
    needs_action_count = len(actionable_proposals) + len(floated_open_observations)

    # Zone 1: Fleet status
    agents, subagents = collect_agents_with_identity(g)

    # Zone 2: Pipeline pulse
    pipeline = build_pipeline_stats(observations, proposals, decisions)

    # Zone 4: Activity feed
    activity = build_activity_feed(observations, proposals, limit=15)

    return templates.TemplateResponse(request, "home.html", {
        "request": request,
        **group_context(g, observations=observations, proposals=proposals),
        # Zone 1: Fleet
        "fleet_agents": agents,
        "fleet_healthy": sum(1 for a in agents if a["health"] == "green"),
        # Zone 2: Pipeline
        "pipeline": pipeline,
        # Zone 3: Attention queue
        "actionable_proposals": actionable_proposals,
        "open_observations": open_observations[:7],
        "floated_observations": floated_observations,
        "needs_action_count": needs_action_count,
        # Zone 4: Activity
        "activity_feed": activity,
        # Executing decisions
        "running_decisions": [d for d in decisions if d.get("execution_status") == "running"],
    })


@app.get("/{group}/observations", response_class=HTMLResponse)
async def observations_list(request: Request, group: str, agent: str = "", status: str = ""):
    """List all observations with optional filtering."""
    g = get_group(group)
    observations = list_observations(g)
    filtered = observations
    if agent:
        filtered = [c for c in filtered if c.get("agent") == agent]
    if status:
        filtered = [c for c in filtered if c.get("status") == status]
    return templates.TemplateResponse(request, "observations.html", {
        "request": request,
        **group_context(g, observations=observations),
        "observations": filtered,
        "filter_agent": agent,
        "filter_status": status,
        "agents": g["agents"],
    })


@app.get("/{group}/observations/{slug}", response_class=HTMLResponse)
async def observation_detail(request: Request, group: str, slug: str):
    """View a single observation."""
    g = get_group(group)
    path = g["shared"] / "observations" / f"{slug}.md"
    if not path.exists():
        raise HTTPException(404, "Observation not found")
    raw = path.read_text()
    meta, body = parse_frontmatter(raw)

    # Resolve pipeline chain: observation → proposal → decision
    pipeline = None
    linked_proposal_slug = meta.get("linked_proposal", "")
    if linked_proposal_slug:
        proposal_slug = linked_proposal_slug.replace(".md", "")
        proposal_path = g["shared"] / "proposals" / f"{proposal_slug}.md"
        pipeline = {"proposal_slug": proposal_slug, "proposal_exists": proposal_path.exists()}
        # Check for a decision on that proposal
        decision_path = g["shared"] / "decisions" / f"{proposal_slug}.md"
        if decision_path.exists():
            dmeta, _ = parse_frontmatter(decision_path.read_text())
            pipeline["decision_slug"] = proposal_slug
            pipeline["decision_status"] = dmeta.get("execution_status", "decided")
        else:
            pipeline["decision_slug"] = None

    return templates.TemplateResponse(request, "observation_detail.html", {
        "request": request,
        **group_context(g),
        "meta": meta,
        "body_html": render_md(body),
        "body_raw": body,
        "slug": slug,
        "title": extract_display_title(body, slug),
        "filename": path.name,
        "pipeline": pipeline,
    })


@app.post("/{group}/observations/{slug}/status", response_class=HTMLResponse)
async def observation_update_status(request: Request, group: str, slug: str):
    """Update an observation's status via form submission."""
    g = get_group(group)
    path = g["shared"] / "observations" / f"{slug}.md"
    if not path.exists():
        raise HTTPException(404, "Observation not found")

    form = await request.form()
    new_status = form.get("status", "")
    if new_status not in ("open", "connected", "dismissed", "archived"):
        raise HTTPException(400, "Invalid status")

    update_frontmatter_field(path, "status", new_status)

    return RedirectResponse(f"/{group}/observations/{slug}", status_code=303)


@app.get("/{group}/proposals", response_class=HTMLResponse)
async def proposals_list(request: Request, group: str):
    """List all proposals."""
    g = get_group(group)
    items = list_proposals(g)
    return templates.TemplateResponse(request, "proposals.html", {
        "request": request,
        **group_context(g),
        "proposals": items,
    })


@app.get("/{group}/proposals/{slug}", response_class=HTMLResponse)
async def proposal_detail(request: Request, group: str, slug: str):
    """View a single proposal."""
    g = get_group(group)
    proposals_dir = g["shared"] / "proposals"
    observations_dir = g["shared"] / "observations"
    decisions_dir = g["shared"] / "decisions"

    path = proposals_dir / f"{slug}.md"
    if not path.exists():
        raise HTTPException(404, "Proposal not found")
    raw = path.read_text()
    meta, body = parse_frontmatter(raw)

    # Find linked observations
    linked = []
    for c in meta.get("observations", []):
        cpath = observations_dir / c
        if cpath.exists():
            linked.append({"filename": c, "slug": cpath.stem})

    # Find related decision
    decision = None
    if decisions_dir.exists():
        for d in decisions_dir.glob("*.md"):
            dtext = d.read_text()
            if slug in dtext:
                dmeta, dbody = parse_frontmatter(dtext)
                decision = {"filename": d.name, "slug": d.stem, "meta": dmeta, "body": dbody}
                break

    # Sync proposal status if a decision exists but status is stale
    if decision and meta.get("status") != "decided":
        update_frontmatter_field(path, "status", "decided")
        meta["status"] = "decided"

    # Parse questions for template
    questions = meta.get("questions", [])
    decision_answers = decision["meta"].get("answers", {}) if decision else {}

    return templates.TemplateResponse(request, "proposal_detail.html", {
        "request": request,
        **group_context(g),
        "meta": meta,
        "body_html": render_md(body),
        "body_raw": body,
        "slug": slug,
        "title": extract_display_title(body, slug),
        "linked_observations": linked,
        "decision": decision,
        "questions": questions,
        "answers": decision_answers,
    })


@app.post("/{group}/proposals/{slug}/decide", response_class=HTMLResponse)
async def proposal_decide(request: Request, group: str, slug: str,
                           background_tasks: BackgroundTasks):
    """Create a decision by answering a proposal's questions."""
    g = get_group(group)
    decisions_dir = g["shared"] / "decisions"
    proposals_dir = g["shared"] / "proposals"

    # Read proposal to get questions and origin_agent
    cpath = proposals_dir / f"{slug}.md"
    if not cpath.exists():
        raise HTTPException(404, "Proposal not found")
    cmeta, _ = parse_frontmatter(cpath.read_text())
    origin_agent = cmeta.get("origin_agent", "")
    questions = cmeta.get("questions", [])

    form = await request.form()

    # Build answers from form data
    answers = {}
    for q in questions:
        key = f"answer_{q['id']}"
        if q.get("type") == "choice" and q.get("multi"):
            answers[q["id"]] = form.getlist(key)
        else:
            answers[q["id"]] = form.get(key, "")

    agency_cfg = get_agency_config()
    decided_by = agency_cfg.get("decided_by", "admin")
    today = datetime.now().strftime("%Y-%m-%d")

    # Build decision frontmatter
    meta = {
        "proposal": f"{slug}.md",
        "decided_by": decided_by,
        "date": today,
        "answers": answers,
        "execution_status": "pending",
    }

    frontmatter = yaml.dump(meta, default_flow_style=False, sort_keys=False).strip()
    decision_content = f"---\n{frontmatter}\n---\n"

    decisions_dir.mkdir(exist_ok=True)
    decision_path = decisions_dir / f"{slug}.md"
    decision_path.write_text(decision_content)

    # Update proposal status to decided
    update_frontmatter_field(cpath, "status", "decided")

    # Always dispatch agent to act on the decision
    if origin_agent:
        background_tasks.add_task(
            execute_decision,
            decision_path, Path(g["path"]), origin_agent, slug,
            group_key=group,
        )

    return RedirectResponse(f"/{group}/decisions/{slug}", status_code=303)


@app.get("/{group}/decisions", response_class=HTMLResponse)
async def decisions_list(request: Request, group: str):
    """List all decisions."""
    g = get_group(group)
    items = list_decisions(g)
    return templates.TemplateResponse(request, "decisions.html", {
        "request": request,
        **group_context(g),
        "decisions": items,
    })


@app.get("/{group}/decisions/{slug}", response_class=HTMLResponse)
async def decision_detail(request: Request, group: str, slug: str):
    """View a single decision."""
    g = get_group(group)
    path = g["shared"] / "decisions" / f"{slug}.md"
    if not path.exists():
        raise HTTPException(404, "Decision not found")
    raw = path.read_text()
    meta, body = parse_frontmatter(raw)

    # Resolve pipeline chain: observations → proposal → this decision
    pipeline_observations = []
    proposal_slug = (meta.get("proposal", "") or "").replace(".md", "")
    if proposal_slug:
        proposal_path = g["shared"] / "proposals" / f"{proposal_slug}.md"
        if proposal_path.exists():
            cmeta, _ = parse_frontmatter(proposal_path.read_text())
            for obs_file in cmeta.get("observations", []):
                obs_slug = obs_file.replace(".md", "")
                obs_path = g["shared"] / "observations" / obs_file
                if obs_path.exists():
                    pipeline_observations.append({"slug": obs_slug, "filename": obs_file})

    execution_status = meta.get("execution_status", "")
    execution_summary = meta.get("execution_summary", "")

    # Load proposal questions for rendering answers
    questions = []
    if proposal_slug:
        proposal_path = g["shared"] / "proposals" / f"{proposal_slug}.md"
        if proposal_path.exists():
            pmeta, _ = parse_frontmatter(proposal_path.read_text())
            questions = pmeta.get("questions", [])

    return templates.TemplateResponse(request, "decision_detail.html", {
        "request": request,
        **group_context(g),
        "meta": meta,
        "body_html": render_md(body),
        "slug": slug,
        "title": extract_display_title(body, slug),
        "pipeline_observations": pipeline_observations,
        "proposal_slug": proposal_slug,
        "execution_status": execution_status,
        "execution_summary": execution_summary,
        "questions": questions,
        "answers": meta.get("answers", {}),
    })


@app.post("/{group}/decisions/{slug}/retry", response_class=HTMLResponse)
async def decision_retry(request: Request, group: str, slug: str,
                         background_tasks: BackgroundTasks):
    """Retry execution of a failed approved decision."""
    g = get_group(group)
    decision_path = g["shared"] / "decisions" / f"{slug}.md"
    if not decision_path.exists():
        raise HTTPException(404, "Decision not found")

    meta, _ = parse_frontmatter(decision_path.read_text())
    proposal_slug = (meta.get("proposal", "") or "").replace(".md", "")

    # Look up origin_agent from the linked proposal
    agent = ""
    if proposal_slug:
        proposal_path = g["shared"] / "proposals" / f"{proposal_slug}.md"
        if proposal_path.exists():
            pmeta, _ = parse_frontmatter(proposal_path.read_text())
            agent = pmeta.get("origin_agent", "")

    if not agent or not proposal_slug:
        raise HTTPException(400, "Decision has no execution agent or linked proposal")

    # Reset execution status
    update_decision_execution(decision_path, "execution_status", "pending")
    update_decision_execution(decision_path, "execution_summary", None)

    background_tasks.add_task(
        execute_decision,
        decision_path, Path(g["path"]), agent, proposal_slug,
        group_key=group,
    )

    return RedirectResponse(f"/{group}/decisions/{slug}", status_code=303)


@app.get("/{group}/documents", response_class=HTMLResponse)
async def documents_list(request: Request, group: str, agent: str = ""):
    """Browse documents by agent."""
    g = get_group(group)
    docs = collect_documents(g)
    if agent:
        docs = [d for d in docs if d["agent"] == agent]
    by_agent = {}
    for d in docs:
        by_agent.setdefault(d["agent"], []).append(d)
    return templates.TemplateResponse(request, "documents.html", {
        "request": request,
        **group_context(g),
        "by_agent": by_agent,
        "filter_agent": agent,
        "agents": g["agents"] + ["shared"],
    })


@app.get("/{group}/documents/view", response_class=HTMLResponse)
async def document_view(request: Request, group: str, path: str):
    """View a document file."""
    g = get_group(group)
    fpath = Path(path)
    # Security: must be under this group's agents dir
    validate_file_access(fpath, g["path"], allowed_roots=get_allowed_roots(g))
    if not fpath.exists():
        raise HTTPException(404, "File not found")

    raw = fpath.read_text()
    suffix = fpath.suffix.lower()

    content_html = ""
    is_csv = False
    csv_headers = []
    csv_rows = []

    if suffix == ".csv":
        is_csv = True
        csv_headers, csv_rows = parse_csv_to_rows(raw)
    elif suffix == ".html":
        content_html = raw
    elif suffix == ".md":
        _, body = parse_frontmatter(raw)
        content_html = render_md(body)
    else:
        content_html = f"<pre class='whitespace-pre-wrap text-sm'>{raw}</pre>"

    return templates.TemplateResponse(request, "document_view.html", {
        "request": request,
        **group_context(g),
        "filename": fpath.name,
        "filepath": str(fpath),
        "raw": raw,
        "content_html": content_html,
        "is_csv": is_csv,
        "csv_headers": csv_headers,
        "csv_rows": csv_rows,
        "suffix": suffix,
        "is_editable": suffix in (".md", ".csv"),
    })


@app.post("/{group}/documents/save", response_class=HTMLResponse)
async def document_save(request: Request, group: str):
    """Save edits to a document."""
    g = get_group(group)
    form = await request.form()
    path = form.get("path", "")
    content = form.get("content", "")
    fpath = Path(path)

    validate_file_access(fpath, g["path"], allowed_roots=get_allowed_roots(g))

    fpath.write_text(content)
    return RedirectResponse(f"/{group}/documents/view?path={urllib.parse.quote(path, safe='')}", status_code=303)


@app.get("/{group}/logs", response_class=HTMLResponse)
async def logs_list(request: Request, group: str):
    """Browse execution logs by date."""
    g = get_group(group)
    logs = collect_logs(g)
    return templates.TemplateResponse(request, "logs.html", {
        "request": request,
        **group_context(g),
        "logs": logs,
    })


@app.get("/{group}/logs/view", response_class=HTMLResponse)
async def log_view(request: Request, group: str, path: str):
    """View a log file."""
    g = get_group(group)
    fpath = Path(path)
    logs_dir = g["shared"] / "logs"
    validate_file_access(fpath, logs_dir)
    if not fpath.exists():
        raise HTTPException(404, "Log not found")

    raw = fpath.read_text()
    content_html = render_md(raw) if fpath.suffix == ".out" else Markup(f"<pre class='whitespace-pre-wrap text-sm text-red-700'>{escape(raw)}</pre>")

    return templates.TemplateResponse(request, "log_view.html", {
        "request": request,
        **group_context(g),
        "filename": fpath.name,
        "content_html": content_html,
        "raw": raw,
    })


@app.get("/{group}/prompts", response_class=HTMLResponse)
async def prompts_list(request: Request, group: str):
    """Browse and edit agent prompts."""
    g = get_group(group)
    items = collect_prompts(g)
    group_cfg = GROUPS.get(g["key"], {})
    dispatch_cfg = group_cfg.get("dispatch", {})
    return templates.TemplateResponse(request, "prompts.html", {
        "request": request,
        **group_context(g),
        "prompts": items,
        "agents": g["agents"],
        "dispatch_enabled": dispatch_cfg.get("enabled", False),
    })


@app.get("/{group}/prompts/{slug}", response_class=HTMLResponse)
async def prompt_detail(request: Request, group: str, slug: str):
    """View/edit a prompt."""
    g = get_group(group)
    path = g["shared"] / "prompts" / f"{slug}.md"
    if not path.exists():
        raise HTTPException(404, "Prompt not found")
    raw = path.read_text()
    content_html = render_md(raw)
    return templates.TemplateResponse(request, "prompt_detail.html", {
        "request": request,
        **group_context(g),
        "slug": slug,
        "raw": raw,
        "content_html": content_html,
    })


@app.post("/{group}/prompts/{slug}/save", response_class=HTMLResponse)
async def prompt_save(request: Request, group: str, slug: str):
    """Save edits to a prompt."""
    g = get_group(group)
    path = g["shared"] / "prompts" / f"{slug}.md"
    # Confine the write to the group root (slug comes from the URL) — matches
    # documents/save and memory/save.
    validate_file_access(path, g["path"], allowed_roots=get_allowed_roots(g))
    form = await request.form()
    content = form.get("content", "")
    path.write_text(content)
    return RedirectResponse(f"/{group}/prompts/{slug}", status_code=303)


@app.post("/{group}/prompts/dispatch", response_class=HTMLResponse)
async def prompts_dispatch_save(request: Request, group: str):
    """Save dispatch assignments edited from the prompts page."""
    g = get_group(group)
    config = load_config()
    if group not in config.get("groups", {}):
        raise HTTPException(404, f"Unknown group: {group}")

    form = await request.form()

    # Rebuild agent-centric dispatch rules from prompt-centric form data.
    # Form fields: assign_agent_{prompt}_{idx}, assign_type_{prompt}_{idx}, assign_value_{prompt}_{idx}
    # Collect all prompt filenames from the form
    prompt_rules: dict[str, list[dict]] = {}  # prompt_name -> [{agent, type, value}]
    prompts_dir = g["shared"] / "prompts"
    prompt_files = sorted(f.name for f in prompts_dir.glob("*.md")) if prompts_dir.exists() else []

    for prompt_file in prompt_files:
        prompt_key = prompt_file.replace(".md", "")
        idx = 0
        while True:
            agent = form.get(f"assign_agent_{prompt_key}_{idx}")
            if agent is None:
                break
            rule_type = form.get(f"assign_type_{prompt_key}_{idx}", "").strip()
            rule_value = form.get(f"assign_value_{prompt_key}_{idx}", "").strip()
            if agent and rule_type and rule_value:
                if prompt_file not in prompt_rules:
                    prompt_rules[prompt_file] = []
                prompt_rules[prompt_file].append({
                    "agent": agent,
                    "type": rule_type,
                    "value": rule_value,
                })
            idx += 1

    # Invert back to agent-centric for config storage
    agents_dispatch: dict[str, list[dict]] = {}
    for prompt_file, assignments in prompt_rules.items():
        for a in assignments:
            agent_name = a["agent"]
            if agent_name not in agents_dispatch:
                agents_dispatch[agent_name] = []
            rule = {"prompt": prompt_file}
            if a["type"] == "at":
                rule["at"] = a["value"]
            else:
                rule["every"] = a["value"]
            # Preserve condition from hidden form field if present
            condition = a.get("condition", "")
            if condition:
                rule["condition"] = condition
            agents_dispatch[agent_name].append(rule)

    # Re-add condition-tagged rules that weren't in the form (read-only rows)
    existing_agents = config["groups"][group].get("dispatch", {}).get("agents", {})
    for agent_name, rules in existing_agents.items():
        for rule in rules:
            if rule.get("condition"):
                if agent_name not in agents_dispatch:
                    agents_dispatch[agent_name] = []
                # Check if this exact condition rule already exists (from hidden field)
                existing = any(
                    r.get("prompt") == rule["prompt"] and r.get("condition") == rule["condition"]
                    for r in agents_dispatch[agent_name]
                )
                if not existing:
                    agents_dispatch[agent_name].append(rule)

    # Merge into existing dispatch config (preserve enabled/timeout/daily_limit)
    existing_dispatch = config["groups"][group].get("dispatch", {})
    existing_dispatch["agents"] = agents_dispatch
    config["groups"][group]["dispatch"] = existing_dispatch

    save_config(config)
    reload_groups()
    return RedirectResponse(f"/{group}/prompts", status_code=303)


@app.get("/{group}/memory", response_class=HTMLResponse)
async def memory_list(request: Request, group: str):
    """Browse and edit agent memory files."""
    g = get_group(group)
    items = collect_memory_files(g)
    return templates.TemplateResponse(request, "memory.html", {
        "request": request,
        **group_context(g),
        "memory_files": items,
    })


@app.get("/{group}/memory/view", response_class=HTMLResponse)
async def memory_view(request: Request, group: str, path: str):
    """View/edit a memory file."""
    g = get_group(group)
    fpath = Path(path)
    validate_file_access(fpath, g["path"], allowed_roots=get_allowed_roots(g))
    if not fpath.exists():
        raise HTTPException(404, "Memory file not found")

    raw = fpath.read_text()
    content_html = render_md(raw)
    agent = fpath.parent.name

    return templates.TemplateResponse(request, "memory_view.html", {
        "request": request,
        **group_context(g),
        "agent": agent,
        "path": str(fpath),
        "raw": raw,
        "content_html": content_html,
    })


@app.post("/{group}/memory/save", response_class=HTMLResponse)
async def memory_save(request: Request, group: str):
    """Save edits to a memory file."""
    g = get_group(group)
    form = await request.form()
    path = form.get("path", "")
    content = form.get("content", "")
    fpath = Path(path)

    validate_file_access(fpath, g["path"], allowed_roots=get_allowed_roots(g))

    fpath.write_text(content)
    return RedirectResponse(f"/{group}/memory/view?path={urllib.parse.quote(path, safe='')}", status_code=303)


@app.get("/{group}/workspaces", response_class=HTMLResponse)
async def workspaces_list(request: Request, group: str):
    """List all workspaces for a group."""
    g = get_group(group)
    group_cfg = GROUPS.get(group, {})
    workspace_list = group_cfg.get("workspaces", [])
    from agency.workspaces import REGISTRY
    enriched = []
    for ws in workspace_list:
        plugin = REGISTRY.get(ws.get("type", "custom"))
        enriched.append({
            **ws,
            "plugin": plugin,
            "summary": plugin.render_summary(ws.get("config", {})) if plugin else "",
            "config_files": plugin.get_config_files(ws.get("config", {})) if plugin else [],
            "can_launch": plugin.supports_launch() if plugin else False,
        })
    return templates.TemplateResponse(request, "workspaces.html", {
        "request": request,
        **group_context(g),
        "enriched_workspaces": enriched,
        "active": "workspaces",
    })


@app.get("/{group}/workspaces/{idx}/file", response_class=HTMLResponse)
async def workspace_file_view(request: Request, group: str, idx: int):
    """View/edit a config file within a workspace."""
    g = get_group(group)
    group_cfg = GROUPS.get(group, {})
    workspace_list = group_cfg.get("workspaces", [])
    if idx < 0 or idx >= len(workspace_list):
        raise HTTPException(404, "Workspace not found")
    ws = workspace_list[idx]
    from agency.workspaces import REGISTRY
    plugin = REGISTRY.get(ws.get("type", "custom"))
    config_files = plugin.get_config_files(ws.get("config", {})) if plugin else []
    file_path = request.query_params.get("path", "")
    if not file_path and config_files:
        file_path = config_files[0]["path"]
    # Validate file is in the plugin's allowlist
    allowed_paths = [cf["path"] for cf in config_files]
    if file_path and file_path not in allowed_paths:
        raise HTTPException(403, "File not in workspace config files")
    raw = ""
    language = "text"
    if file_path:
        fpath = Path(file_path)
        if fpath.exists():
            raw = fpath.read_text()
        for cf in config_files:
            if cf["path"] == file_path:
                language = cf.get("language", "text")
                break
    return templates.TemplateResponse(request, "workspace_detail.html", {
        "request": request,
        **group_context(g),
        "ws": ws,
        "ws_idx": idx,
        "plugin": plugin,
        "config_files": config_files,
        "current_file": file_path,
        "raw": raw,
        "language": language,
        "active": "workspaces",
    })


@app.post("/{group}/workspaces/{idx}/file/save", response_class=HTMLResponse)
async def workspace_file_save(request: Request, group: str, idx: int):
    """Save edits to a workspace config file."""
    g = get_group(group)
    group_cfg = GROUPS.get(group, {})
    workspace_list = group_cfg.get("workspaces", [])
    if idx < 0 or idx >= len(workspace_list):
        raise HTTPException(404, "Workspace not found")
    form = await request.form()
    file_path = form.get("file_path", "")
    content = form.get("content", "")
    if file_path:
        ws = workspace_list[idx]
        from agency.workspaces import REGISTRY
        plugin = REGISTRY.get(ws.get("type", "custom"))
        allowed = [cf["path"] for cf in plugin.get_config_files(ws.get("config", {}))] if plugin else []
        # Workspace config files are intentionally external (tmux script_path, Cursor
        # project_path, etc. live outside the group root), so root containment does not
        # apply here. The control is this deny-by-default allowlist — the path must exactly
        # match one the plugin itself derived from admin config — mirroring workspace_file_view.
        if file_path not in allowed:
            raise HTTPException(403, "File not in workspace config files")
        Path(file_path).write_text(content)
    return RedirectResponse(f"/{group}/workspaces/{idx}/file?path={urllib.parse.quote(file_path, safe='')}", status_code=303)


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Agency — Agent Management Dashboard")
    parser.add_argument("--port", type=int, default=8500, help="Port to serve on (default: 8500)")
    parser.add_argument("--host", default="127.0.0.1", help="Host to bind to (default: 127.0.0.1, loopback only). Agency has no authentication — only bind to a non-loopback address behind an authenticating reverse proxy.")
    args = parser.parse_args()

    # First-run: create default config
    if not CONFIG_PATH.exists():
        save_config({"agency": {"title": "Agency", "default_group": ""}, "groups": {}})
        print(f"First run — created config.yaml in {CONFIG_PATH.parent}")
        print(f"Visit http://localhost:{args.port}/admin/ to set up your first agent group.")

    reload_groups()
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
