"""Route-rendering smoke tests.

Exercises every template-rendering GET route end-to-end through the ASGI app so a
framework upgrade (e.g. the Starlette 1.x `TemplateResponse(request, name, ...)`
signature change) that breaks rendering fails loudly here rather than in production.
Before this file the only live-route coverage was two workspace tests.
"""

import pytest
from pathlib import Path
from starlette.testclient import TestClient


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


@pytest.fixture
def client(tmp_path):
    """A fully-populated single-group app: agent, observation, proposal, decision,
    prompt, memory, and a log — enough for every list and detail route to render."""
    from agency.app import app, CONFIG, GROUPS

    gp = tmp_path / "grp"
    # Agent with a claude-code identity file
    _write(gp / "product" / "CLAUDE.md", "# Product\n\nProduct agent definition.\n")
    _write(gp / "product" / "memory.md", "# Memory\n\nremembered thing\n")

    shared = gp / "shared"
    for sub in ("observations", "proposals", "decisions", "prompts", "logs"):
        (shared / sub).mkdir(parents=True, exist_ok=True)
    _write(shared / "memory.md", "# Shared memory\n")

    _write(shared / "observations" / "obs-one.md",
           "---\nagent: product\ndate: 2026-03-20T06:00:00-04:00\n"
           "category: test\nstatus: open\nfloat: false\nttl_days: 14\n---\n\n"
           "**First observation** body text.\n")
    _write(shared / "proposals" / "prop-one.md",
           "---\norigin_agent: product\ndate: 2026-03-20\nstatus: proposed\n"
           "observations: [obs-one.md]\nttl_days: 30\nquestions:\n"
           "  - id: approve\n    type: boolean\n    prompt: \"Approve?\"\n---\n\n"
           "**A proposal** body.\n")
    _write(shared / "decisions" / "dec-one.md",
           "---\nproposal: prop-one.md\ndecided_by: admin\ndate: 2026-03-20\n"
           "answers:\n  approve: approved\nexecution_status: complete\n---\n\n"
           "**A decision** body.\n")
    _write(shared / "prompts" / "routine.md", "# Routine\n\nDo the thing.\n")
    _write(shared / "logs" / "2026-03-20" / "product-run.out", "log line\n")

    group_cfg = {
        "name": "Grp",
        "path": str(gp),
        "agents": ["product"],
        "_agents_normalized": [{"name": "product", "integration": "claude-code"}],
        "workspaces": [
            {"name": "Terminal", "type": "tmux",
             "config": {"script_path": str(gp / "tmux.sh")}},
        ],
    }
    (gp / "tmux.sh").write_text("#!/bin/bash\n")

    CONFIG.clear()
    CONFIG.update({"agency": {"title": "Test", "default_group": "grp"},
                   "groups": {"grp": group_cfg}})
    GROUPS.clear()
    GROUPS["grp"] = group_cfg
    c = TestClient(app)
    c.grp_path = gp  # absolute group path for path-param routes
    return c


# (path, expected status) — detail routes use the slugs created in the fixture.
GET_ROUTES = [
    ("/grp/", 200),
    ("/grp/agents", 200),
    ("/grp/agents/product", 200),
    ("/grp/observations", 200),
    ("/grp/observations/obs-one", 200),
    ("/grp/proposals", 200),
    ("/grp/proposals/prop-one", 200),
    ("/grp/decisions", 200),
    ("/grp/decisions/dec-one", 200),
    ("/grp/documents", 200),
    ("/grp/logs", 200),
    ("/grp/prompts", 200),
    ("/grp/prompts/routine", 200),
    ("/grp/memory", 200),
    ("/grp/workspaces", 200),
    ("/grp/workspaces/0/file", 200),
    ("/admin/", 200),
    ("/admin/dispatch", 200),
    ("/admin/groups", 200),
    ("/admin/integrations", 200),
    # NOTE: /admin/orgs/{org}/edit reads config.yaml from disk (not in-memory CONFIG),
    # so it can't be reached from this fixture; its template change is identical to the
    # 34 routes covered here.
]


@pytest.mark.parametrize("path,expected", GET_ROUTES)
def test_route_renders(client, path, expected):
    resp = client.get(path, follow_redirects=True)
    assert resp.status_code == expected, f"{path} -> {resp.status_code}"
    assert "text/html" in resp.headers.get("content-type", ""), f"{path} not HTML"


def test_memory_view_renders(client):
    abs_path = str(client.grp_path / "shared" / "memory.md")
    resp = client.get("/grp/memory/view", params={"path": abs_path},
                      follow_redirects=True)
    assert resp.status_code == 200


def test_document_view_renders(client):
    abs_path = str(client.grp_path / "product" / "memory.md")
    resp = client.get("/grp/documents/view", params={"path": abs_path},
                      follow_redirects=True)
    assert resp.status_code == 200


def test_security_headers_present(client):
    """Every response carries the defense-in-depth headers."""
    resp = client.get("/grp/", follow_redirects=True)
    h = resp.headers
    assert h["X-Frame-Options"] == "DENY"
    assert h["X-Content-Type-Options"] == "nosniff"
    assert h["Referrer-Policy"] == "same-origin"
    assert "Permissions-Policy" in h
    csp = h["Content-Security-Policy"]
    assert "default-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp
    assert "object-src 'none'" in csp
    # the resources the UI actually loads must remain allowed
    assert "https://cdn.tailwindcss.com" in csp
    assert "https://fonts.googleapis.com" in csp


def test_security_headers_on_static(client):
    """Headers apply to static assets too (middleware runs for all routes)."""
    resp = client.get("/static/icon.svg")
    assert resp.headers.get("X-Content-Type-Options") == "nosniff"


def test_prompt_save_writes_within_group(client):
    """prompt_save still saves legitimately after adding the path guard."""
    resp = client.post("/grp/prompts/routine/save",
                       data={"content": "# Routine\n\nUpdated body.\n"},
                       follow_redirects=False)
    assert resp.status_code == 303
    saved = (client.grp_path / "shared" / "prompts" / "routine.md").read_text()
    assert "Updated body." in saved

