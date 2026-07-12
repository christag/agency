# Deployment

## Running Directly

```bash
pip install -e .
agency serve
# or: python -m agency.app
```

Agency serves on `http://127.0.0.1:8500` by default.

## Network exposure

Agency binds to `127.0.0.1` (loopback) by default and ships **no authentication** — the admin
surface can create agent groups, edit prompts, and dispatch LLM tools that execute code.

To reach the dashboard from another machine, do **not** simply pass `--host 0.0.0.0`. That exposes
an unauthenticated admin surface to your whole network. Instead, put Agency behind a reverse proxy
(Traefik, nginx, Caddy) that terminates TLS and enforces authentication, and have the proxy forward
to Agency on loopback. TLS/HSTS belong on that proxy, not on Agency.

## Dependencies

```
fastapi>=0.139,<1.0, starlette>=1.3.1,<2, uvicorn[standard]>=0.49,<1.0, jinja2>=3.1.6,<4,
markdown>=3.10,<4, pyyaml>=6.0.2,<7, markupsafe>=3.0,<4, python-multipart>=0.0.20,<0.1, nh3>=0.3,<1.0
```

All defined in `pyproject.toml`. Install with `pip install -e .`.

For a reproducible, pinned install use the lock file instead: `pip install -r requirements.lock`
(runtime closure only; regenerate it after changing dependencies).

## Running as a systemd User Service (Linux)

A service template is provided at `agency.service.example`. Copy and customize it:

```bash
cp agency.service.example ~/.config/systemd/user/agency.service
# Edit the file to set your paths

systemctl --user daemon-reload
systemctl --user enable --now agency.service
```

### Service Management

```bash
systemctl --user status agency.service       # Check status
systemctl --user restart agency.service      # Restart after code changes
journalctl --user -u agency.service -f       # Stream logs
```

## Running on macOS

Run directly with `python -m agency.app`. For persistence, create a launchd agent or use a process manager like `brew services`.

## Platform Support

Agency runs on any OS with Python 3.11+:

- **Linux** — full support including systemd dispatch timers
- **macOS** — full support including launchd dispatch timers
- **Windows** — app runs, dispatch timers require manual Task Scheduler setup

## Notes

- Agency assumes local/trusted access. There is no built-in authentication. Use a reverse proxy (Traefik, nginx, Caddy) if you need auth.
- Use a **user-level** systemd service on Linux, not system-level. System services cannot access user home directories on immutable OSes like Fedora Kinoite.
- The Python venv should be at `.venv/` in the project directory. The service file should reference `.venv/bin/python -m agency.app`.
