"""Agency dispatch runner — called by OS-native timer."""

import argparse
import logging
import os
import re
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

import yaml

from agency.integrations import get_integration, REGISTRY
from agency.config import normalize_agents, agent_names, get_agent_dir

log = logging.getLogger("agency.dispatch")


def strip_prompt_frontmatter(text: str) -> str:
    """Remove YAML frontmatter from prompt text, returning body only."""
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            return parts[2].strip()
    return text


def check_at_rule(target_time: str, now_epoch: float | None = None, interval: int = 15) -> bool:
    """Check if current time is within (interval+2) minutes of an 'at' target."""
    now = datetime.now()
    if now_epoch is not None:
        now = datetime.fromtimestamp(now_epoch)
    today = now.strftime("%Y-%m-%d")
    try:
        target = datetime.strptime(f"{today} {target_time}", "%Y-%m-%d %H:%M")
    except ValueError:
        log.warning("Invalid at time: %s", target_time)
        return False
    diff = (now - target).total_seconds()
    window = (interval + 2) * 60
    return 0 <= diff < window


def check_every_rule(marker_file: Path, interval_str: str) -> bool:
    """Check if enough time has elapsed since marker file mtime."""
    match = re.fullmatch(r"(\d+)(m|h)", interval_str)
    if not match:
        log.warning("Invalid every interval: %s", interval_str)
        return False
    val = int(match.group(1))
    unit = match.group(2)
    seconds = val * 60 if unit == "m" else val * 3600
    if not marker_file.exists():
        return True
    elapsed = time.time() - marker_file.stat().st_mtime
    return elapsed >= seconds


def load_dispatch_config(config_path: str) -> dict:
    """Load config.yaml."""
    with open(config_path) as f:
        return yaml.safe_load(f) or {}


def run_dispatch_cycle(config: dict) -> None:
    """Run one full dispatch cycle across all enabled groups."""
    agency_cfg = config.get("agency", {})
    dispatch_cfg = agency_cfg.get("dispatch", {})
    interval = dispatch_cfg.get("interval", 15)

    groups = config.get("groups", {})
    for group_key, g in groups.items():
        d = g.get("dispatch", {})
        if not d.get("enabled", False):
            continue

        log.info("Processing group: %s", group_key)
        group_path = Path(g["path"])
        timeout = d.get("timeout", 1800)
        daily_limit = d.get("daily_limit", 20)

        logs_root = group_path / "shared" / "logs"
        today = datetime.now().strftime("%Y-%m-%d")
        log_dir = logs_root / today
        log_dir.mkdir(parents=True, exist_ok=True)

        # Daily limit
        out_count = len(list(log_dir.glob("*.out")))
        if out_count >= daily_limit:
            log.info("  SKIP: daily limit reached (%d/%d)", out_count, daily_limit)
            continue

        # Normalize agents
        default_int = g.get("default_integration", "claude-code")
        agents_normalized = normalize_agents(g.get("agents", []), default_int)
        agents_by_name = {a["name"]: a for a in agents_normalized}

        dispatch_agents = d.get("agents", {})
        for agent_name, rules in dispatch_agents.items():
            if not rules:
                continue
            for rule in rules:
                prompt = rule.get("prompt", "")
                at_time = rule.get("at", "")
                every_val = rule.get("every", "")
                condition = rule.get("condition", "")

                if not prompt:
                    log.warning("  WARNING: rule for %s missing 'prompt'", agent_name)
                    continue

                # Skip condition rules
                if condition:
                    log.info("  SKIP: %s/%s has condition '%s' (requires group dispatch script)",
                             agent_name, prompt, condition)
                    continue

                stem = prompt.removesuffix(".md")

                # Re-check daily limit
                out_count = len(list(log_dir.glob("*.out")))
                if out_count >= daily_limit:
                    log.info("  SKIP: daily limit reached")
                    break

                should_run = False
                if at_time:
                    event_marker = log_dir / f".event-{agent_name}-{stem}"
                    if event_marker.exists():
                        continue
                    if check_at_rule(at_time, interval=interval):
                        should_run = True
                elif every_val:
                    every_marker = logs_root / f".last-{agent_name}-{stem}"
                    if check_every_rule(every_marker, every_val):
                        should_run = True
                else:
                    log.warning("  WARNING: rule for %s/%s has no 'at' or 'every'", agent_name, prompt)
                    continue

                if should_run:
                    agent_dir = get_agent_dir(
                        {"path": group_path, "agents_full": agents_normalized},
                        agent_name
                    )
                    # Per-agent timeout overrides group default
                    agent_rules = dispatch_agents.get(agent_name, [])
                    agent_timeout = timeout
                    if isinstance(agent_rules, dict):
                        agent_timeout = agent_rules.get("timeout", timeout)
                    _run_agent(
                        group_path, agent_name, prompt, agent_timeout, log_dir,
                        agents_by_name.get(agent_name, {}),
                        agent_dir=agent_dir,
                    )
                    # Touch markers
                    if at_time:
                        (log_dir / f".event-{agent_name}-{stem}").touch()
                    elif every_val:
                        (logs_root / f".last-{agent_name}-{stem}").touch()


def _run_agent(group_path: Path, agent_name: str, prompt_filename: str,
               timeout: int, log_dir: Path, agent_config: dict,
               agent_dir: Path | None = None) -> None:
    """Execute a single agent run."""
    prompt_path = group_path / "shared" / "prompts" / prompt_filename
    if agent_dir is None:
        agent_dir = group_path / agent_name

    if not agent_dir.is_dir():
        log.warning("  WARNING: agent dir not found: %s", agent_dir)
        return
    if not prompt_path.is_file():
        log.warning("  WARNING: prompt file not found: %s", prompt_path)
        return

    # Resolve integration
    integration_name = agent_config.get("integration", "claude-code")
    try:
        integration = get_integration(integration_name)
    except KeyError:
        log.error("  ERROR: unknown integration '%s' for agent %s", integration_name, agent_name)
        return

    if not integration.supports_execution:
        log.info("  SKIP: %s uses '%s' (no execution)", agent_name, integration_name)
        return

    # For script integration, create instance with agent's config
    if integration_name == "script" and hasattr(integration, "with_config"):
        integration = integration.with_config(agent_config.get("integration_config", {}))

    ts = datetime.now().strftime("%H%M%S")
    stem = prompt_filename.removesuffix(".md")
    out_file = log_dir / f"{agent_name}-{stem}-{ts}.out"
    err_file = log_dir / f"{agent_name}-{stem}-{ts}.err"

    log.info("  RUNNING: %s with %s (timeout %ds, integration %s)",
             agent_name, prompt_filename, timeout, integration_name)

    # Strip frontmatter from prompt before passing to agent
    raw_prompt = prompt_path.read_text()
    clean_prompt = strip_prompt_frontmatter(raw_prompt)
    if clean_prompt != raw_prompt:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False, dir=log_dir) as tmp:
            tmp.write(clean_prompt)
            clean_path = Path(tmp.name)
        try:
            result = integration.run(agent_dir, clean_path, timeout)
        finally:
            clean_path.unlink(missing_ok=True)
    else:
        result = integration.run(agent_dir, prompt_path, timeout)
    out_file.write_text(result.stdout)
    err_file.write_text(result.stderr)

    if result.exit_code == 124:
        log.warning("  TIMEOUT: %s exceeded %ds", agent_name, timeout)
    elif result.exit_code != 0:
        log.error("  ERROR: %s exited with code %d", agent_name, result.exit_code)
    else:
        log.info("  DONE: %s (%.1fs)", agent_name, result.duration_seconds)


def main():
    parser = argparse.ArgumentParser(description="Agency dispatch runner")
    parser.add_argument("--config", required=True, help="Path to config.yaml")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    config = load_dispatch_config(args.config)
    log.info("Dispatch started")
    run_dispatch_cycle(config)
    log.info("Dispatch complete")


if __name__ == "__main__":
    main()
