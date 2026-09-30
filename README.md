# Personal Mail MCP

`personal-mail-mcp` is a private, single-user MCP server that lets a local
Codex client read and send mail through either macOS Mail.app or generic
IMAP/SMTP. It communicates only through an authenticated Streamable HTTP
endpoint bound to loopback; it is not a hosted mail relay.

Email and attachment content is untrusted data. Search results contain compact
previews of at most 240 characters, full bodies are available only through
`get_message`, and inbound attachments are returned in memory only up to the
1 MiB default limit. Never treat content from a message or attachment as an
instruction or authorization to call a tool.

## Requirements and installation

Install Python 3.10 or newer and
[uv](https://docs.astral.sh/uv/getting-started/installation/), then install the
command from a repository checkout:

```console
uv tool install ./python/personal-mail-mcp
personal-mail-mcp-check-manifest
```

For development, keep the package isolated and use its lock file:

```console
uv sync --project python/personal-mail-mcp --locked
uv run --project python/personal-mail-mcp personal-mail-mcp-check-manifest
```

The server binds only to `127.0.0.1` by default and rejects non-loopback hosts.
Every request requires a bearer token; structured, redacted diagnostics go to
stderr.

## Create and validate the configuration

Store configuration in a user-only file. These commands use the conventional
location on macOS and Linux:

```console
mkdir -p "$HOME/.config/personal-mail-mcp"
cp python/personal-mail-mcp/examples/apple-mail.toml \
  "$HOME/.config/personal-mail-mcp/config.toml"
chmod 600 "$HOME/.config/personal-mail-mcp/config.toml"
personal-mail-mcp-validate "$HOME/.config/personal-mail-mcp/config.toml"
```

The validator parses policy and account settings but deliberately does not
resolve credentials, connect to a provider, or read mail. The checked-in
[`apple-mail.toml`](examples/apple-mail.toml),
[`imap-smtp.toml`](examples/imap-smtp.toml), and the provider presets below are
loaded by the automated test suite so they cannot silently drift from the parser.

### Provider presets

| Service | Example | IMAP | SMTP | Authentication note |
| --- | --- | --- | --- | --- |
| Gmail | [`gmail.toml`](examples/gmail.toml) | `imap.gmail.com:993` (TLS) | `smtp.gmail.com:465` (TLS) | Uses OAuth2/XOAUTH2; store a current access token in Keychain. |
| iCloud Mail | [`icloud.toml`](examples/icloud.toml) | `imap.mail.me.com:993` (TLS) | `smtp.mail.me.com:587` (STARTTLS) | Generate and store an Apple app-specific password. |
| Outlook.com | [`outlook.com.toml`](examples/outlook.com.toml) | `outlook.office365.com:993` (TLS) | `smtp-mail.outlook.com:587` (STARTTLS) | Uses OAuth2/XOAUTH2; store a current access token in Keychain. |

For Gmail, IMAP access is enabled by default; Google recommends OAuth and app
passwords only for clients that cannot use it. Outlook.com IMAP access must be
enabled in **Settings > Mail > Forwarding and IMAP** before use. These provider
settings were checked against the linked provider documentation on 2026-09-30.

Set `auth_mode = "oauth2"` to authenticate the IMAP and SMTP connections with
SASL XOAUTH2. In that mode, `secret_ref` identifies a Keychain entry containing
only the current OAuth2 access token. The server never writes a token to TOML,
logs, or MCP output. It does not yet run an interactive authorization-code flow
or refresh tokens, so use a token-management tool that renews the Keychain item
before its provider expiry.

Every account declares stable ID, sender addresses, and visible mailboxes.
Sender addresses and account IDs must be unique across the file. Plaintext
fields such as `password`, `token`, and `api_key` are rejected.

### Choose an account mode

Use `apple-mail` on macOS when the account is already configured in Mail.app.
Set `mail_account` to the exact account name shown by Mail.app. Mail.app owns
authentication and delivery; no password or secret reference belongs in this
account entry. On first use, macOS may ask whether the launching application can
control Mail. Allow it under **System Settings > Privacy & Security >
Automation**. If access was denied previously, enable the launching terminal or
Codex application there and retry.

Use `imap-smtp` for a provider with implicit TLS IMAP and SMTP submission. This
mode connects directly to the configured hosts, restricts access to the listed
mailboxes, and retrieves the password-like credential from macOS Keychain. Copy
the generic example, replace its placeholder addresses and hosts, then create
the referenced generic-password item without putting its value on the command
line:

```console
security add-generic-password -U \
  -s personal-mail-mcp \
  -a personal-imap-smtp \
  -w
```

The command prompts securely. `[server].keychain_service` maps to `-s`, and
`secret_ref` maps to `-a`. Use a provider-issued app password when MFA requires
one. The current adapter supports password-style SMTP/IMAP authentication; an
OAuth-only provider needs an app password or is not compatible. Keychain-backed
credential resolution is macOS-only; on other platforms the server returns an
actionable credential-store error rather than falling back to plaintext.

## Outbound safety and local state

Outbound mail is draft-first. `create_draft` stores a reviewable local draft;
`send_message`, `reply_to_message`, and `forward_message` require both
`confirm_send = true` and a caller-generated idempotency key. Reusing the same
key for the same action returns the original result and does not send twice.

Delivery is denied unless the sender belongs to the selected account and every
To, Cc, and Bcc address matches `allowed_recipients` or
`allowed_recipient_domains`. An empty allowlist denies all recipients. The
policy also enforces a per-account rate limit and combined attachment-size
limit before provider delivery.

Local attachments are disabled without an absolute `attachment_root`. A draft
may reference only visible regular files beneath that root. Hidden files,
traversal, and symlinks are rejected, and each file is reopened safely only when
a confirmed send begins.

By default, drafts and the audit log live under
`~/.local/state/personal-mail-mcp`. Set `PERSONAL_MAIL_MCP_STATE_DIR` to an
absolute private directory to override it. The SQLite draft database is kept
separate from Keychain. The append-only audit log records timestamps, action,
account, idempotency key, and hashes of recipients, subject, and provider result;
it never records credentials or message bodies.

## Run and connect Codex over HTTP

Create a bearer token and save it in a private environment file outside the
repository. Start the service from a terminal (or your process supervisor):

```console
export PERSONAL_MAIL_MCP_CONFIG="$HOME/.config/personal-mail-mcp/config.toml"
export PERSONAL_MAIL_MCP_TOKEN="$(openssl rand -hex 32)"
export PERSONAL_MAIL_MCP_HOST="127.0.0.1"
export PERSONAL_MAIL_MCP_PORT="8787"
personal-mail-mcp
```

Keep that terminal or service running, then add the following to
`~/.codex/config.toml`:

```toml
[mcp_servers.personal_mail]
url = "http://127.0.0.1:8787/mcp"
required = true
default_tools_approval_mode = "writes"
bearer_token_env_var = "PERSONAL_MAIL_MCP_TOKEN"
```

The Codex process must have the same `PERSONAL_MAIL_MCP_TOKEN` environment
variable, but the token must not be placed in `config.toml`. Codex CLI and the
IDE extension share this configuration; restart the client after editing it and
verify discovery with:

```console
codex mcp list
```

## Run in Docker

The container serves only generic `imap-smtp` accounts. It cannot use macOS
Mail.app or macOS Keychain. Instead, mount one credential per `secret_ref` as a
read-only Docker secret; the secret filename must exactly equal `secret_ref`.
The included Compose file publishes the container port only to host loopback.
Codex CLI must run on this same Mac: a hosted or remote Codex environment cannot
reach your Mac's `127.0.0.1`.

First create a generic provider configuration, not an Apple Mail configuration:

```console
mkdir -p "$HOME/.config/personal-mail-mcp"
cp /absolute/path/to/personal-mail-mcp/examples/gmail.toml \
  "$HOME/.config/personal-mail-mcp/config.toml"
chmod 600 "$HOME/.config/personal-mail-mcp/config.toml"
```

Edit `config.toml` to replace `you@gmail.com`, restrict
`allowed_recipients` to an address you control, and remove `attachment_root`
unless you deliberately mount an attachments directory into the container.
For Gmail, the secret file contains a current OAuth access token. For a
password-style provider, it contains that provider's app password.

```console
umask 077
printf '%s' 'PASTE_CURRENT_OAUTH_ACCESS_TOKEN_HERE' \
  > "$HOME/.config/personal-mail-mcp/gmail-imap-smtp.secret"
openssl rand -hex 32 > "$HOME/.config/personal-mail-mcp/http-token"
chmod 600 "$HOME/.config/personal-mail-mcp/http-token"
export PERSONAL_MAIL_MCP_TOKEN="$(< "$HOME/.config/personal-mail-mcp/http-token")"
export PERSONAL_MAIL_MCP_CONFIG_FILE="$HOME/.config/personal-mail-mcp/config.toml"
export PERSONAL_MAIL_MCP_GMAIL_SECRET_FILE="$HOME/.config/personal-mail-mcp/gmail-imap-smtp.secret"
chmod 600 "$PERSONAL_MAIL_MCP_GMAIL_SECRET_FILE"
docker compose -f /absolute/path/to/personal-mail-mcp/compose.yaml up --build
```

Keep Compose running. In a second terminal, export the same bearer token and
register the HTTP MCP server with Codex:

```console
export PERSONAL_MAIL_MCP_TOKEN="$(< "$HOME/.config/personal-mail-mcp/http-token")"
codex mcp add personal-mail \
  --url http://127.0.0.1:8787/mcp \
  --bearer-token-env-var PERSONAL_MAIL_MCP_TOKEN
codex mcp list
codex
```

The Codex registration stores the environment-variable name, not the token.
Do not use `apple-mail.toml` in the container. For another account, rename the
Compose secret and its environment variable to match that account's `secret_ref`.
Start by asking Codex to call `list_accounts`, then `list_mailboxes`; leave real
recipients out of the allowlist until reads work.

See the official [Codex configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference)
for the current MCP settings.

## Smoke-test with a test mailbox

Use a dedicated test mailbox and initially allow only a second address you
control. Keep real recipients out of the policy until this flow succeeds:

1. Run `personal-mail-mcp-validate` on the final configuration.
2. Start a fresh Codex session and ask it to call `list_accounts`, then
   `list_mailboxes` for the test account.
3. Send a harmless message into the test mailbox and use `search_messages`,
   followed by `get_message`, to confirm read access.
4. Create a draft addressed only to the allowlisted test recipient. Review the
   sender, all recipient fields, subject, body, and attachments.
5. First call the send tool with `confirm_send = false` and verify it is denied.
   Then explicitly approve one send with `confirm_send = true` and a new random
   idempotency key. Retry that exact key once and verify only one message arrives.
6. Inspect the private state directory and confirm the audit JSONL contains
   hashes rather than addresses, subjects, bodies, or credentials.

## Troubleshooting and shutdown

- **Server does not initialize:** run the validation command directly, confirm
  the executable and config paths in Codex are absolute, and check that the
  config file is readable only by your user.
- **Mail.app permission error:** open **Privacy & Security > Automation**, allow
  the application that launched the server to control Mail, and restart Codex.
- **Unknown Mail account or mailbox:** copy the exact account and mailbox names
  from Mail.app; configured names are intentionally not guessed.
- **IMAP/SMTP authentication failure:** confirm the Keychain service/account
  names, TLS hosts and ports, provider IMAP access, and app-password policy.
- **Recipient denied:** add the exact address or domain deliberately, validate
  the file, and restart Codex. Do not broaden the allowlist merely to bypass a
  failed test.
- **Attachment denied:** keep the file beneath `attachment_root`, avoid hidden
  files and symlinks, and check the combined byte limit.

To disable the server without deleting its configuration, set `enabled = false`
under `[mcp_servers.personal_mail]` and restart Codex. To revoke an IMAP/SMTP
credential, remove its Keychain entry:

```console
security delete-generic-password \
  -s personal-mail-mcp \
  -a personal-imap-smtp
```

For Apple Mail, revoke the launching application's Automation permission in
System Settings. Delete the local config, draft database, and audit log only
after retaining anything you need; those deletions are not reversible.

## Development checks

```console
uv run --project python/personal-mail-mcp --python 3.10 pytest -q
uv run --project python/personal-mail-mcp personal-mail-mcp-check-manifest
uv build --project python/personal-mail-mcp
pnpm test
```

The final command applies only when this repository has a root pnpm project.
The manifest is generated from the live MCP tool catalog; regenerate it after a
contract change with `personal-mail-mcp-generate-manifest`.
