"""Tests for schedule visualizer helper functions."""

from pathlib import Path

import pytest
from starlette.testclient import TestClient

from agency.app import (
    build_schedule_cards,
    collect_task_runs,
    humanize_prompt_name,
    parse_prompt_frontmatter,
)


class TestParsePromptFrontmatter:
    """Tests for parse_prompt_frontmatter()."""

    def test_with_frontmatter(self, tmp_path):
        p = tmp_path / "prompt.md"
        p.write_text("---\ndescription: Morning routine\nexpected_output: status report\n---\nDo the thing.\n")
        meta, body = parse_prompt_frontmatter(p)
        assert meta["description"] == "Morning routine"
        assert meta["expected_output"] == "status report"
        assert body == "Do the thing."

    def test_without_frontmatter(self, tmp_path):
        p = tmp_path / "prompt.md"
        p.write_text("Just a plain prompt.\n")
        meta, body = parse_prompt_frontmatter(p)
        assert meta == {}
        assert "Just a plain prompt." in body

    def test_missing_file(self, tmp_path):
        p = tmp_path / "nonexistent.md"
        meta, body = parse_prompt_frontmatter(p)
        assert meta == {}
        assert body == ""


class TestHumanizePromptName:
    """Tests for humanize_prompt_name()."""

    def test_basic(self):
        assert humanize_prompt_name("product-routine.md") == "Product Routine"

    def test_with_md_suffix(self):
        assert humanize_prompt_name("morning-report.md") == "Morning Report"

    def test_without_suffix(self):
        assert humanize_prompt_name("morning-report") == "Morning Report"

    def test_underscores(self):
        assert humanize_prompt_name("daily_standup.md") == "Daily Standup"

    def test_mixed_separators(self):
        assert humanize_prompt_name("my_cool-prompt.md") == "My Cool Prompt"


class TestCollectTaskRuns:
    """Tests for collect_task_runs()."""

    def _make_group(self, tmp_path):
        shared = tmp_path / "shared"
        logs = shared / "logs"
        logs.mkdir(parents=True)
        return {"path": tmp_path, "shared": shared}

    def test_finds_matching_logs(self, tmp_path):
        g = self._make_group(tmp_path)
        date_dir = g["shared"] / "logs" / "2026-03-29"
        date_dir.mkdir()
        (date_dir / "product-routine-070001.out").write_text("output here")
        (date_dir / "product-routine-070001.err").write_text("")

        runs = collect_task_runs(g, "routine")
        assert len(runs) == 1
        assert runs[0]["agent"] == "product"
        assert runs[0]["date"] == "2026-03-29"
        assert runs[0]["time"] == "07:00:01"
        assert runs[0]["status"] == "success"
        assert runs[0]["out_size"] == len("output here")

    def test_detects_error_status(self, tmp_path):
        g = self._make_group(tmp_path)
        date_dir = g["shared"] / "logs" / "2026-03-29"
        date_dir.mkdir()
        (date_dir / "qa-routine-080000.out").write_text("some output")
        (date_dir / "qa-routine-080000.err").write_text("error: something broke")

        runs = collect_task_runs(g, "routine")
        assert len(runs) == 1
        assert runs[0]["status"] == "error"
        assert runs[0]["agent"] == "qa"

    def test_sorted_newest_first(self, tmp_path):
        g = self._make_group(tmp_path)
        for date in ["2026-03-27", "2026-03-29", "2026-03-28"]:
            d = g["shared"] / "logs" / date
            d.mkdir()
            (d / "product-routine-070000.out").write_text("x")
            (d / "product-routine-070000.err").write_text("")

        runs = collect_task_runs(g, "routine")
        dates = [r["date"] for r in runs]
        assert dates == ["2026-03-29", "2026-03-28", "2026-03-27"]

    def test_caps_at_limit(self, tmp_path):
        g = self._make_group(tmp_path)
        for i in range(25):
            d = g["shared"] / "logs" / f"2026-03-{i+1:02d}"
            d.mkdir()
            (d / "product-routine-070000.out").write_text("x")
            (d / "product-routine-070000.err").write_text("")

        runs = collect_task_runs(g, "routine", limit=20)
        assert len(runs) == 20

    def test_empty_logs_dir(self, tmp_path):
        g = self._make_group(tmp_path)
        runs = collect_task_runs(g, "routine")
        assert runs == []

    def test_no_logs_dir(self, tmp_path):
        g = {"path": tmp_path, "shared": tmp_path / "shared"}
        runs = collect_task_runs(g, "routine")
        assert runs == []

    def test_ignores_non_matching_stems(self, tmp_path):
        g = self._make_group(tmp_path)
        date_dir = g["shared"] / "logs" / "2026-03-29"
        date_dir.mkdir()
        (date_dir / "product-routine-070000.out").write_text("match")
        (date_dir / "product-routine-070000.err").write_text("")
        (date_dir / "product-daily-080000.out").write_text("no match")
        (date_dir / "product-daily-080000.err").write_text("")

        runs = collect_task_runs(g, "routine")
        assert len(runs) == 1
        assert runs[0]["agent"] == "product"

    def test_hyphenated_agent_name(self, tmp_path):
        """Agent names with hyphens: business-ops-routine-070000.out"""
        g = self._make_group(tmp_path)
        date_dir = g["shared"] / "logs" / "2026-03-29"
        date_dir.mkdir()
        (date_dir / "business-ops-routine-070000.out").write_text("x")
        (date_dir / "business-ops-routine-070000.err").write_text("")

        runs = collect_task_runs(g, "routine")
        assert len(runs) == 1
        assert runs[0]["agent"] == "business-ops"


class TestBuildScheduleCards:
    """Tests for build_schedule_cards()."""

    def _make_group(self, tmp_path):
        shared = tmp_path / "shared"
        prompts = shared / "prompts"
        logs = shared / "logs"
        prompts.mkdir(parents=True)
        logs.mkdir(parents=True)
        return {"path": tmp_path, "shared": shared}

    def test_builds_cards_from_dispatch_config(self, tmp_path):
        """Two agents sharing a prompt → one card with two assignments."""
        g = self._make_group(tmp_path)
        (g["shared"] / "prompts" / "routine.md").write_text(
            "---\ndescription: Daily routine\nexpected_output: status report\n---\nDo stuff.\n"
        )
        dispatch_cfg = {
            "agents": {
                "product": [
                    {"prompt": "routine.md", "at": "07:30"},
                ],
                "qa": [
                    {"prompt": "routine.md", "every": "6h"},
                ],
            }
        }
        cards = build_schedule_cards(g, dispatch_cfg)
        assert len(cards) == 1
        card = cards[0]
        assert card["slug"] == "routine"
        assert card["name"] == "Routine"
        assert card["filename"] == "routine.md"
        assert card["description"] == "Daily routine"
        assert card["expected_output"] == "status report"
        assert len(card["assignments"]) == 2
        agents = {a["agent"] for a in card["assignments"]}
        assert agents == {"product", "qa"}
        # Check individual assignment fields
        product_assign = next(a for a in card["assignments"] if a["agent"] == "product")
        assert product_assign["at"] == "07:30"
        assert product_assign["every"] is None
        assert product_assign["condition"] is None
        qa_assign = next(a for a in card["assignments"] if a["agent"] == "qa")
        assert qa_assign["at"] is None
        assert qa_assign["every"] == "6h"

    def test_multiple_prompts_become_multiple_cards(self, tmp_path):
        """One agent with two prompts → two cards."""
        g = self._make_group(tmp_path)
        (g["shared"] / "prompts" / "routine.md").write_text("---\ndescription: Routine\n---\nBody.\n")
        (g["shared"] / "prompts" / "cleanup.md").write_text("---\ndescription: Cleanup\n---\nBody.\n")
        dispatch_cfg = {
            "agents": {
                "product": [
                    {"prompt": "routine.md", "at": "07:30"},
                    {"prompt": "cleanup.md", "at": "23:00"},
                ],
            }
        }
        cards = build_schedule_cards(g, dispatch_cfg)
        assert len(cards) == 2
        slugs = [c["slug"] for c in cards]
        assert "cleanup" in slugs
        assert "routine" in slugs

    def test_excludes_system_prompts(self, tmp_path):
        """Prompts starting with _ are excluded."""
        g = self._make_group(tmp_path)
        (g["shared"] / "prompts" / "_system.md").write_text("---\ndescription: System\n---\nBody.\n")
        (g["shared"] / "prompts" / "routine.md").write_text("---\ndescription: Routine\n---\nBody.\n")
        dispatch_cfg = {
            "agents": {
                "product": [
                    {"prompt": "_system.md", "at": "06:00"},
                    {"prompt": "routine.md", "at": "07:30"},
                ],
            }
        }
        cards = build_schedule_cards(g, dispatch_cfg)
        assert len(cards) == 1
        assert cards[0]["slug"] == "routine"

    def test_empty_dispatch_returns_empty(self, tmp_path):
        """Empty config → empty list."""
        g = self._make_group(tmp_path)
        cards = build_schedule_cards(g, {})
        assert cards == []
        cards = build_schedule_cards(g, {"agents": {}})
        assert cards == []


class TestRunNowValidation:
    """Validation tests for POST /{group}/schedule/{slug}/run."""

    def _make_app(self, tmp_path):
        from agency.app import app, CONFIG, GROUPS

        # Create shared structure
        (tmp_path / "shared" / "prompts").mkdir(parents=True, exist_ok=True)
        (tmp_path / "shared" / "observations").mkdir(parents=True, exist_ok=True)
        (tmp_path / "shared" / "proposals").mkdir(parents=True, exist_ok=True)
        (tmp_path / "shared" / "logs").mkdir(parents=True, exist_ok=True)
        (tmp_path / "alpha").mkdir()

        # Write a prompt file
        (tmp_path / "shared" / "prompts" / "morning.md").write_text(
            "---\ndescription: Morning routine\n---\n\nDo the morning thing.\n"
        )

        group_cfg = {
            "name": "Test Group",
            "path": str(tmp_path),
            "key": "test",
            "shared": tmp_path / "shared",
            "agents": ["alpha"],
            "agents_full": [{"name": "alpha", "integration": "claude-code"}],
            "_agents_normalized": [{"name": "alpha", "integration": "claude-code"}],
            "default_integration": "claude-code",
            "dispatch": {
                "enabled": True,
                "timeout": 1800,
                "agents": {
                    "alpha": [
                        {"prompt": "morning.md", "at": "09:00"},
                    ],
                },
            },
        }

        CONFIG.clear()
        CONFIG.update({"agency": {"title": "Test", "default_group": "test"}, "groups": {"test": group_cfg}})
        GROUPS.clear()
        GROUPS["test"] = group_cfg
        return TestClient(app)

    def test_rejects_empty_agent(self, tmp_path):
        """POST with no agent field should return 400."""
        client = self._make_app(tmp_path)
        resp = client.post("/test/schedule/morning/run", data={})
        assert resp.status_code == 400

    def test_rejects_unassigned_agent(self, tmp_path):
        """POST with agent not in dispatch config should return 400."""
        client = self._make_app(tmp_path)
        resp = client.post("/test/schedule/morning/run", data={"agent": "not-assigned"})
        assert resp.status_code == 400
