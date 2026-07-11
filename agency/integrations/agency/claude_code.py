"""Claude Code CLI integration."""

import shutil
import subprocess
import time
from pathlib import Path

import yaml

from agency.integrations import (
    BaseIntegration, RunResult, AgentIdentity, IntegrationError, _register,
    parse_identity_frontmatter,
)

# Keep backward-compat alias for any external importers
_parse_frontmatter = parse_identity_frontmatter


class ClaudeCodeIntegration(BaseIntegration):
    name = "claude-code"
    display_name = "Claude Code"
    supports_execution = True
    supports_ai_backend = True
    detect_priority = 10

    def identity_filename(self) -> str:
        return "CLAUDE.md"

    def detect(self, agent_dir: Path) -> bool:
        return (agent_dir / "CLAUDE.md").is_file()

    def parse_identity(self, agent_dir: Path) -> AgentIdentity | None:
        path = agent_dir / "CLAUDE.md"
        if not path.is_file():
            return None
        meta, body = _parse_frontmatter(path.read_text())
        return AgentIdentity(
            display_name=meta.get("display_name"),
            title=meta.get("title"),
            emoji=meta.get("emoji"),
            body=body,
        )

    def write_identity(self, agent_dir: Path, identity: AgentIdentity) -> None:
        path = agent_dir / "CLAUDE.md"
        # Preserve existing frontmatter fields
        if path.is_file():
            existing_meta, _ = _parse_frontmatter(path.read_text())
        else:
            existing_meta = {}
        # Update identity fields
        for key, value in [
            ("display_name", identity.display_name),
            ("title", identity.title),
            ("emoji", identity.emoji),
        ]:
            if value:
                existing_meta[key] = value
            elif key in existing_meta and not value:
                del existing_meta[key]
        # Write
        if existing_meta:
            front = yaml.dump(existing_meta, default_flow_style=False, sort_keys=False).strip()
            path.write_text(f"---\n{front}\n---\n\n{identity.body}")
        else:
            path.write_text(identity.body)

    def run(self, agent_dir: Path, prompt_file: Path, timeout: int) -> RunResult:
        prompt_text = prompt_file.read_text()
        cmd = self._find_cmd()
        start = time.monotonic()
        try:
            result = subprocess.run(
                [cmd, "--dangerously-skip-permissions", "-p", prompt_text],
                capture_output=True, text=True, timeout=timeout,
                cwd=str(agent_dir),
                # The prompt is passed via -p, never stdin. Without this, a
                # background/service invocation inherits an open stdin pipe and the
                # claude CLI blocks ~3s waiting for input, then writes a spurious
                # "no stdin data received in 3s" warning to stderr — which surfaces
                # as a red ERR entry on the logs page for an otherwise-clean run.
                stdin=subprocess.DEVNULL,
            )
            duration = time.monotonic() - start
            return RunResult(
                exit_code=result.returncode,
                stdout=result.stdout,
                stderr=result.stderr,
                duration_seconds=duration,
            )
        except subprocess.TimeoutExpired:
            duration = time.monotonic() - start
            return RunResult(exit_code=124, stdout="", stderr="Timed out", duration_seconds=duration)
        except FileNotFoundError:
            raise IntegrationError(f"Claude Code CLI not found. Looked for: {cmd}")

    def prompt(self, text: str, timeout: int = 60) -> str:
        cmd = self._find_cmd()
        try:
            result = subprocess.run(
                [cmd, "-p", text],
                capture_output=True, text=True, timeout=timeout,
                stdin=subprocess.DEVNULL,  # prompt is via -p; avoid the CLI's 3s stdin wait + warning
            )
            if result.returncode != 0:
                raise IntegrationError(f"claude exited with code {result.returncode}: {result.stderr}")
            return result.stdout
        except FileNotFoundError:
            raise IntegrationError(f"Claude Code CLI not found. Looked for: {cmd}")
        except subprocess.TimeoutExpired:
            raise IntegrationError(f"claude timed out after {timeout}s")

    def _find_cmd(self) -> str:
        return self._resolve_cmd("claude")


_register(ClaudeCodeIntegration())
