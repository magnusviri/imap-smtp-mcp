# Personal Mail MCP

`personal-mail-mcp` is a private, single-user MCP server for accessing personal
email from a local MCP host. It is an independent Python package and does not
modify or proxy a hosted mail service.

This initial package contains only the stdio server runtime. Mail accounts and
tools will be added in later changes.

## Requirements

- Python 3.10 or newer
- [uv](https://docs.astral.sh/uv/)

## Run locally

From the repository root, install the locked dependencies and start the server:

```console
uv sync --project python/personal-mail-mcp
uv run --project python/personal-mail-mcp personal-mail-mcp
```

The command uses standard input and standard output exclusively for MCP protocol
messages. Diagnostics are written to standard error so they cannot corrupt the
protocol stream.

## Test

```console
uv run --project python/personal-mail-mcp pytest -q
```
