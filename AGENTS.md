# Repository Guidelines

## Project Structure & Module Organization

The repository contains the `personal-mail-mcp` Python package under
`python/personal-mail-mcp/`. Production code lives in
`src/personal_mail_mcp/`; keep MCP transport, provider adapters, configuration,
and policy logic in focused modules there. Tests mirror behavior in `tests/`.
Checked-in provider templates belong in `examples/`, and the generated tool
catalog is `src/personal_mail_mcp/manifest.json`. Container files (`Dockerfile`,
`compose.yaml`) live at the package root.

## Build, Test, and Development Commands

Run commands from this repository root:

```bash
uv run pytest -q
uv tool run ruff check .
uv tool run ruff format --check .
uv run personal-mail-mcp-check-manifest
uv build
```

Use `uv lock` after changing dependencies.
For the HTTP service, configure `PERSONAL_MAIL_MCP_CONFIG` and
`PERSONAL_MAIL_MCP_TOKEN`, then run `personal-mail-mcp`. Docker Compose is for
generic IMAP/SMTP only; it cannot access macOS Mail.app or Keychain.

## Coding Style & Naming Conventions

Target Python 3.10+ with four-space indentation, type annotations, and concise
docstrings for public classes and functions. Follow Ruff formatting and imports;
do not hand-format against its output. Use `snake_case` for modules, functions,
and variables; `PascalCase` for classes; and explicit, stable account/message
identifiers. Never log credentials, full message bodies, attachment bytes, or
unnecessary filesystem paths.

## Testing Guidelines

Use pytest with `test_*.py` files and focused test names such as
`test_http_transport_rejects_missing_bearer_token`. Prefer deterministic fakes
for IMAP, SMTP, Keychain, and AppleScript interactions—tests must not require a
real mailbox or network access. Update manifest checks when tool schemas change.

## Commit & Pull Request Guidelines

Use imperative Conventional Commit-style subjects, for example
`feat: add authenticated HTTP mail container` or
`feat: US-004 - implement MCP runtime`. Keep commits scoped. PRs should explain
the user-visible change, security implications, configuration changes, and test
commands run. Include updated examples or documentation for new settings.

## Security & Configuration

Keep secrets out of TOML, Git, logs, and commits. Use Keychain on macOS or
read-only Docker secrets in containers. Bind HTTP only to loopback on the host,
require a 32+ character bearer token, and preserve draft-first, allowlist, and
idempotency protections for every outbound change.
