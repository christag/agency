"""Tests for schedule visualizer helper functions."""

from pathlib import Path

import pytest

from agency.app import (
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
