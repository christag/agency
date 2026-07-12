"""Agency CLI — terminal interface for agent management."""
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

# Import shared helpers from the web app module
from agency.app import (
    load_config, reload_groups, get_agency_config, get_group,
    list_observations, list_proposals, list_decisions,
    collect_agents_with_identity, extract_display_title,
    parse_frontmatter, update_frontmatter_field,
    GROUPS, CONFIG, CONFIG_PATH,
)


# ── ANSI Colors ──────────────────────────────────────────────────────────────

def _supports_color():
    return hasattr(sys.stdout, "isatty") and sys.stdout.isatty()

COLORS_ENABLED = _supports_color()

def _c(code: str, text: str) -> str:
    if not COLORS_ENABLED:
        return text
    return f"\033[{code}m{text}\033[0m"

def green(t): return _c("32", t)
def yellow(t): return _c("33", t)
def red(t): return _c("31", t)
def cyan(t): return _c("36", t)
def bold(t): return _c("1", t)
def dim(t): return _c("2", t)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _default_group() -> str:
    cfg = get_agency_config()
    return cfg.get("default_group", "")

def _resolve_group(args) -> dict:
    """Get group dict from --group flag or default."""
    group_key = getattr(args, "group", None) or _default_group()
    if not group_key:
        print("Error: No group specified and no default_group in config.", file=sys.stderr)
        sys.exit(1)
    try:
        return get_group(group_key)
    except Exception:
        print(f"Error: Group '{group_key}' not found.", file=sys.stderr)
        sys.exit(1)

def _health_dot(health: str) -> str:
    if health == "green": return green("●")
    if health == "amber": return yellow("●")
    return red("●")

def _relative_time(dt_val) -> str:
    if not dt_val:
        return "never"
    if isinstance(dt_val, str):
        try:
            dt_val = datetime.fromisoformat(dt_val)
        except (ValueError, TypeError):
            return str(dt_val)
    now = datetime.now(dt_val.tzinfo) if dt_val.tzinfo else datetime.now()
    diff = now - dt_val
    minutes = int(diff.total_seconds() / 60)
    if minutes < 60: return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24: return f"{hours}h ago"
    days = hours // 24
    return f"{days}d ago"


# ── Subcommands ──────────────────────────────────────────────────────────────

def cmd_serve(args):
    """Start the web server."""
    from agency.app import main as app_main
    # Override sys.argv so argparse in app.main doesn't see our args
    sys.argv = ["agency"]
    if args.port:
        sys.argv += ["--port", str(args.port)]
    if args.host:
        sys.argv += ["--host", args.host]
    app_main()

def cmd_inbox(args):
    """Show what needs attention."""
    g = _resolve_group(args)
    observations = list_observations(g)
    proposals = list_proposals(g)
    decisions = list_decisions(g)

    actionable = [p for p in proposals if p.get("status") in ("proposed", "investigating")]
    floated = [o for o in observations if o.get("float") and o.get("status") == "open"]
    open_obs = [o for o in observations if o.get("status") == "open"]

    if getattr(args, "json", False):
        print(json.dumps({
            "group": g["key"],
            "actionable_proposals": [{"slug": p["_slug"], "title": p.get("_title", p["_slug"]), "status": p["status"], "agent": p.get("origin_agent", "")} for p in actionable],
            "floated_observations": [{"slug": o["_slug"], "title": o.get("_title", o["_slug"]), "agent": o.get("agent", "")} for o in floated],
            "open_observations": len(open_obs),
            "total_decisions": len(decisions),
        }, indent=2))
        return

    title = get_agency_config().get("title", "Agency")
    print(f"\n{bold(title)} — {g['name']}\n")

    if actionable:
        print(f"  {bold('NEEDS DECISION')} ({len(actionable)})")
        for p in actionable:
            agent = p.get("origin_agent", "?")
            title_text = p.get("_title", p["_slug"])
            print(f"  ├─ {cyan(p['_slug'][:30])}  {p.get('status', '')}  {dim(agent)}")
        print()

    if floated:
        print(f"  {bold('FLOATED SIGNALS')} ({len(floated)})")
        for o in floated:
            agent = o.get("agent", "?")
            print(f"  ├─ {yellow(o['_slug'][:30])}  {dim(agent)}")
        print()

    if open_obs:
        print(f"  {bold('OPEN OBSERVATIONS')} ({len(open_obs)})")
        for o in open_obs[:5]:
            agent = o.get("agent", "?")
            print(f"  ├─ {o['_slug'][:30]}  {dim(agent)}")
        if len(open_obs) > 5:
            print(f"  └─ ... and {len(open_obs) - 5} more")
        print()

    agents, _ = collect_agents_with_identity(g)
    healthy = sum(1 for a in agents if a["health"] == "green")
    print(f"  FLEET: {len(agents)} agents · {healthy} green · {len(agents) - healthy} other\n")

def cmd_status(args):
    """Fleet overview across all groups."""
    if getattr(args, "json", False):
        result = {}
        for key in GROUPS:
            g = get_group(key)
            obs = list_observations(g)
            props = list_proposals(g)
            decs = list_decisions(g)
            agents, _ = collect_agents_with_identity(g)
            result[key] = {
                "name": g["name"],
                "observations": len(obs),
                "proposals": len(props),
                "decisions": len(decs),
                "agents": len(agents),
                "healthy": sum(1 for a in agents if a["health"] == "green"),
            }
        print(json.dumps(result, indent=2))
        return

    title = get_agency_config().get("title", "Agency")
    print(f"\n{bold(title)} — Fleet Status\n")

    for key in GROUPS:
        g = get_group(key)
        obs = list_observations(g)
        props = list_proposals(g)
        decs = list_decisions(g)
        agents, _ = collect_agents_with_identity(g)
        healthy = sum(1 for a in agents if a["health"] == "green")
        actionable = sum(1 for p in props if p.get("status") in ("proposed", "investigating"))

        status_dots = " ".join(f"{_health_dot(a['health'])} {a['name']}" for a in agents[:6])
        needs = f"{yellow(str(actionable))} needs decision" if actionable else green("clear")

        print(f"  {bold(g['name'])} ({key})")
        print(f"    {status_dots}")
        print(f"    {len(obs)} observations · {len(props)} proposals · {len(decs)} decisions · {needs}")
        print()

def cmd_observations(args):
    """List observations."""
    g = _resolve_group(args)
    items = list_observations(g)
    if args.agent:
        items = [i for i in items if i.get("agent") == args.agent]
    if args.status:
        items = [i for i in items if i.get("status") == args.status]

    if getattr(args, "json", False):
        print(json.dumps([{"slug": i["_slug"], "title": i.get("_title", i["_slug"]), "agent": i.get("agent", ""), "status": i.get("status", ""), "date": str(i.get("date", ""))} for i in items], indent=2))
        return

    print(f"\n{bold('Observations')} — {g['name']} ({len(items)} total)\n")
    for i in items:
        status = i.get("status", "")
        agent = i.get("agent", "")
        title_text = i.get("_title", i["_slug"])
        float_marker = f" {yellow('★')}" if i.get("float") else ""
        agent_str = agent[:12].rjust(12)
        print(f"  {dim(agent_str)}  {title_text[:60]}{float_marker}  {dim(status)}")
    print()

def cmd_proposals(args):
    """List proposals."""
    g = _resolve_group(args)
    items = list_proposals(g)
    if args.status:
        items = [i for i in items if i.get("status") == args.status]

    if getattr(args, "json", False):
        print(json.dumps([{"slug": i["_slug"], "title": i.get("_title", i["_slug"]), "status": i.get("status", ""), "agent": i.get("origin_agent", ""), "date": str(i.get("date", ""))} for i in items], indent=2))
        return

    print(f"\n{bold('Proposals')} — {g['name']} ({len(items)} total)\n")
    for i in items:
        status = i.get("status", "")
        agent = i.get("origin_agent", "")
        title_text = i.get("_title", i["_slug"])
        agent_str = agent[:12].rjust(12)
        print(f"  {dim(agent_str)}  {title_text[:60]}  {dim(status)}")
    print()

def cmd_decisions(args):
    """List decisions."""
    g = _resolve_group(args)
    items = list_decisions(g)

    if getattr(args, "json", False):
        print(json.dumps([{"slug": i["_slug"], "title": i.get("_title", i["_slug"]), "answers": i.get("answers", {}), "date": str(i.get("date", ""))} for i in items], indent=2))
        return

    print(f"\n{bold('Decisions')} — {g['name']} ({len(items)} total)\n")
    for i in items:
        title_text = i.get("_title", i["_slug"])
        answers = i.get("answers", {})
        answer_count = len(answers)
        print(f"  {green('decided')}  {title_text[:60]}  {dim(str(i.get('date', '')))}  {dim(f'{answer_count} answer(s)')}")
    print()

def cmd_agents(args):
    """List agents with health status."""
    g = _resolve_group(args)
    agents, subagents = collect_agents_with_identity(g)

    if getattr(args, "json", False):
        all_agents = agents + subagents
        print(json.dumps([{"name": a["name"], "health": a["health"], "integration": a["integration"], "open_observations": a["open_observations"]} for a in all_agents], indent=2))
        return

    print(f"\n{bold('Agents')} — {g['name']}\n")
    for a in agents:
        emoji = a.get("emoji", "")
        display = a.get("display_name", a["name"])
        dot = _health_dot(a["health"])
        obs = f"{a['open_observations']} open" if a["open_observations"] else ""
        print(f"  {dot} {emoji} {display} ({a['name']})  {dim(a['integration'])}  {dim(obs)}")
    if subagents:
        print(f"\n  {dim('Subagents:')}")
        for a in subagents:
            dot = _health_dot(a["health"])
            print(f"  {dot} {a.get('emoji', '')} {a['name']}  {dim(a['integration'])}")
    print()

def cmd_decide(args):
    """Interactively answer a proposal's questions."""
    import yaml

    g = _resolve_group(args)
    slug = args.slug
    proposals_dir = g["shared"] / "proposals"
    decisions_dir = g["shared"] / "decisions"

    path = proposals_dir / f"{slug}.md"
    if not path.exists():
        print(f"Error: Proposal '{slug}' not found.", file=sys.stderr)
        sys.exit(1)

    meta, body = parse_frontmatter(path.read_text())
    questions = meta.get("questions", [])
    if not questions:
        print("Error: Proposal has no questions.", file=sys.stderr)
        sys.exit(1)

    origin_agent = meta.get("origin_agent", "")
    print(f"\n{bold(slug)}\n")

    answers = {}
    for i, q in enumerate(questions, 1):
        print(f"  {cyan(str(i))}. {q['prompt']}")

        if q["type"] == "boolean":
            print(f"     {green('[a]')}pprove  {yellow('[d]')}efer  {red('[r]')}eject")
            while True:
                choice = input("     > ").strip().lower()
                if choice in ("a", "approve"):
                    answers[q["id"]] = "approved"
                    break
                elif choice in ("d", "defer"):
                    answers[q["id"]] = "deferred"
                    break
                elif choice in ("r", "reject"):
                    answers[q["id"]] = "rejected"
                    break
                print("     Invalid choice. Enter a/d/r.")

        elif q["type"] == "choice":
            options = q.get("options", [])
            for j, opt in enumerate(options, 1):
                print(f"     [{j}] {opt['label']}")
            if q.get("multi"):
                print("     (comma-separated numbers for multiple)")
                raw = input("     > ").strip()
                indices = [int(x.strip()) for x in raw.split(",") if x.strip().isdigit()]
                answers[q["id"]] = [options[idx - 1]["label"] for idx in indices if 1 <= idx <= len(options)]
            else:
                while True:
                    raw = input("     > ").strip()
                    if raw.isdigit() and 1 <= int(raw) <= len(options):
                        answers[q["id"]] = options[int(raw) - 1]["label"]
                        break
                    print(f"     Enter a number 1-{len(options)}.")

        elif q["type"] == "free-response":
            answers[q["id"]] = input("     > ").strip()

        print()

    # Create decision file
    today = datetime.now().strftime("%Y-%m-%d")
    dec_meta = {
        "proposal": f"{slug}.md",
        "decided_by": "cli",
        "date": today,
        "answers": answers,
        "execution_status": "pending",
    }
    frontmatter = yaml.dump(dec_meta, default_flow_style=False, sort_keys=False).strip()
    content = f"---\n{frontmatter}\n---\n"

    decisions_dir.mkdir(exist_ok=True)
    decision_path = decisions_dir / f"{slug}.md"
    decision_path.write_text(content)

    # Update proposal status
    update_frontmatter_field(path, "status", "decided")

    print(f"{green('✓')} Decision saved: shared/decisions/{slug}.md")
    for qid, ans in answers.items():
        if isinstance(ans, list):
            print(f"  {qid}: {', '.join(ans)}")
        else:
            print(f"  {qid}: {ans}")


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        prog="agency",
        description="Agency — AI Agent Management",
    )
    sub = parser.add_subparsers(dest="command")

    # serve
    p = sub.add_parser("serve", help="Start the web dashboard")
    p.add_argument("--port", type=int, default=8500)
    p.add_argument("--host", default="127.0.0.1", help="Bind address (default: loopback only; Agency has no auth)")

    # inbox
    p = sub.add_parser("inbox", help="What needs attention")
    p.add_argument("--group", "-g")
    p.add_argument("--json", action="store_true")

    # status
    p = sub.add_parser("status", help="Fleet overview across all groups")
    p.add_argument("--json", action="store_true")

    # observations
    p = sub.add_parser("observations", help="List observations")
    p.add_argument("--group", "-g")
    p.add_argument("--status", "-s")
    p.add_argument("--agent", "-a")
    p.add_argument("--json", action="store_true")

    # proposals
    p = sub.add_parser("proposals", help="List proposals")
    p.add_argument("--group", "-g")
    p.add_argument("--status", "-s")
    p.add_argument("--json", action="store_true")

    # decisions
    p = sub.add_parser("decisions", help="List decisions")
    p.add_argument("--group", "-g")
    p.add_argument("--json", action="store_true")

    # decide
    p = sub.add_parser("decide", help="Answer a proposal's questions")
    p.add_argument("slug", help="Proposal slug")
    p.add_argument("--group", "-g")

    # agents
    p = sub.add_parser("agents", help="List agents with health status")
    p.add_argument("--group", "-g")
    p.add_argument("--json", action="store_true")

    args = parser.parse_args()

    # No command → show help
    if not args.command:
        parser.print_help()
        return

    # Initialize config for all commands except serve (which does its own init)
    if args.command != "serve":
        if not CONFIG_PATH.exists():
            print("Error: No config.yaml found. Run 'agency serve' first.", file=sys.stderr)
            sys.exit(1)
        reload_groups()

    dispatch = {
        "serve": cmd_serve,
        "inbox": cmd_inbox,
        "status": cmd_status,
        "observations": cmd_observations,
        "proposals": cmd_proposals,
        "decisions": cmd_decisions,
        "decide": cmd_decide,
        "agents": cmd_agents,
    }
    dispatch[args.command](args)


if __name__ == "__main__":
    main()
