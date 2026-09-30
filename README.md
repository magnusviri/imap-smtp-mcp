# Personal Mail MCP

`personal-mail-mcp` is a private, single-user MCP server for accessing personal
email from a local MCP host. It is an independent Python package and does not
modify or proxy a hosted mail service.

Read tools return at most 240 characters of each message preview. Full text and
HTML bodies are available only from `get_message`. `get_attachment` returns
base64 content in memory and never writes to an arbitrary filesystem path; the
default inline attachment limit is 1 MiB, after which retrieval fails with an
actionable size error.

This package provides the stdio server runtime, validated local account
configuration, and the typed public tool catalog. Provider integrations are
added independently behind the runtime's service boundary.

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

The checked-in MCP manifest is generated directly from the runtime schemas.
Regenerate it after changing a tool contract, and use the check command in CI:

```console
uv run --project python/personal-mail-mcp personal-mail-mcp-generate-manifest
uv run --project python/personal-mail-mcp personal-mail-mcp-check-manifest
```

## Configure accounts

Configuration is TOML and contains server settings plus one or more named
accounts. Keep it in a user-only readable location (for example,
`~/.config/personal-mail-mcp/config.toml` with mode `0600`). Every account must
declare its allowed sender addresses and selected mailboxes explicitly.

An account managed by Mail.app identifies the exact Mail.app account to use:

```toml
[server]
request_timeout_seconds = 30
keychain_service = "personal-mail-mcp"

[[accounts]]
id = "icloud"
type = "apple-mail"
mail_account = "iCloud"
sender_addresses = ["me@example.com"]
mailboxes = ["Inbox", "Sent"]
```

A generic account specifies TLS endpoints and refers to a macOS Keychain item;
it never stores the credential itself:

```toml
[[accounts]]
id = "personal"
type = "imap-smtp"
sender_addresses = ["owner@example.com"]
mailboxes = ["INBOX", "Archive", "Sent"]
username = "owner@example.com"
secret_ref = "personal-imap-smtp"

[accounts.imap]
host = "imap.example.com"
port = 993
tls = true

[accounts.smtp]
host = "smtp.example.com"
port = 465
tls = true

[accounts.outbound_policy]
allowed_recipients = ["family@example.net"]
allowed_recipient_domains = ["example.org"]
max_attachment_bytes = 10485760
rate_limit_messages = 20
rate_limit_window_seconds = 3600
```

Outbound mail is draft-first and denied unless every recipient matches either
`allowed_recipients` or `allowed_recipient_domains`. Sending a draft, replying,
and forwarding all require `confirm_send = true` and a caller-generated
idempotency key. The same key returns the original delivery result on retry
without sending another message. Per-account sender ownership, attachment-size,
and rate-limit checks run before provider delivery.

Draft content can be kept in a user-only SQLite file through the local draft
store. Keep this file separate from the operating-system credential store. The
append-only audit log contains recipient and subject hashes, an idempotency key,
and a hash of the provider result; it does not contain credentials, addresses,
subjects, or message bodies.

Create the referenced generic-password entry without putting its value on the
command line (the `security` command prompts for it):

```console
security add-generic-password -U -s personal-mail-mcp -a personal-imap-smtp -w
```

The `keychain_service` value maps to `-s`, and each account's `secret_ref` maps
to `-a`. Plaintext fields such as `password`, `token`, or `api_key` are rejected.
Apple Keychain references can only be resolved on macOS; tests and other secure
runtimes can inject an implementation of the `SecretProvider` interface.

## Test

```console
uv run --project python/personal-mail-mcp pytest -q
```
