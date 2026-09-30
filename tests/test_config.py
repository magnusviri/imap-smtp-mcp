from __future__ import annotations

import copy
import subprocess
from dataclasses import dataclass

import pytest
from personal_mail_mcp.config import (
    AppleMailAccount,
    ConfigurationError,
    ImapSmtpAccount,
    load_config,
    parse_config,
)
from personal_mail_mcp.secrets import (
    KeychainSecretProvider,
    SecretReference,
    SecretResolutionError,
    SecretValue,
)


@pytest.fixture
def valid_config() -> dict[str, object]:
    return {
        "server": {
            "request_timeout_seconds": 20,
            "keychain_service": "personal-mail-test",
        },
        "accounts": [
            {
                "id": "icloud",
                "type": "apple-mail",
                "sender_addresses": ["me@example.com"],
                "mailboxes": ["Inbox", "Sent"],
                "mail_account": "iCloud",
            },
            {
                "id": "work",
                "type": "imap-smtp",
                "sender_addresses": ["me@work.example"],
                "mailboxes": ["INBOX", "Archive"],
                "username": "me@work.example",
                "secret_ref": "work-mail",
                "imap": {"host": "imap.work.example", "port": 993, "tls": True},
                "smtp": {"host": "smtp.work.example", "port": 465, "tls": True},
            },
        ],
    }


def test_valid_configuration_builds_typed_accounts(
    valid_config: dict[str, object],
) -> None:
    config = parse_config(valid_config)

    assert config.server.request_timeout_seconds == 20
    assert config.server.keychain_service == "personal-mail-test"
    assert isinstance(config.account("icloud"), AppleMailAccount)
    work = config.account("work")
    assert isinstance(work, ImapSmtpAccount)
    assert work.imap.port == 993
    assert work.smtp.tls is True
    assert work.mailboxes == ("INBOX", "Archive")


def test_outbound_policy_configuration_is_typed(valid_config) -> None:
    raw = copy.deepcopy(valid_config)
    raw["accounts"][1]["outbound_policy"] = {
        "allowed_recipients": ["Friend@Example.NET"],
        "allowed_recipient_domains": ["Example.ORG"],
        "max_attachment_bytes": 1234,
        "rate_limit_messages": 4,
        "rate_limit_window_seconds": 90,
    }

    policy = parse_config(raw).account("work").outbound_policy

    assert policy.allowed_recipients == ("friend@example.net",)
    assert policy.allowed_recipient_domains == ("example.org",)
    assert policy.max_attachment_bytes == 1234
    assert policy.rate_limit_messages == 4
    assert policy.rate_limit_window_seconds == 90


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("allowed_recipients", ["not-an-address"]),
        ("allowed_recipient_domains", ["@example.org"]),
        ("max_attachment_bytes", -1),
        ("rate_limit_messages", 0),
        ("rate_limit_window_seconds", True),
    ],
)
def test_invalid_outbound_policy_is_rejected(valid_config, field, value) -> None:
    raw = copy.deepcopy(valid_config)
    raw["accounts"][1]["outbound_policy"] = {field: value}

    with pytest.raises(ConfigurationError):
        parse_config(raw)


def test_loads_documented_toml_shape(tmp_path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        """
[server]
request_timeout_seconds = 30
keychain_service = "personal-mail-mcp"

[[accounts]]
id = "personal"
type = "imap-smtp"
sender_addresses = ["owner@example.com"]
mailboxes = ["INBOX", "Sent"]
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
""".strip()
        + "\n",
        encoding="utf-8",
    )

    config = load_config(path)

    assert config.accounts[0].id == "personal"


def test_unknown_account_type_is_rejected(valid_config: dict[str, object]) -> None:
    raw = copy.deepcopy(valid_config)
    raw["accounts"][0]["type"] = "exchange"  # type: ignore[index]

    with pytest.raises(ConfigurationError, match="unknown type 'exchange'"):
        parse_config(raw)


@pytest.mark.parametrize(
    ("field", "duplicate", "message"),
    [
        ("id", "icloud", "Duplicate account ID"),
        ("sender_addresses", ["ME@EXAMPLE.COM"], "Duplicate sender address"),
    ],
)
def test_duplicate_account_identity_is_rejected(
    valid_config: dict[str, object], field: str, duplicate: object, message: str
) -> None:
    raw = copy.deepcopy(valid_config)
    raw["accounts"][1][field] = duplicate  # type: ignore[index]

    with pytest.raises(ConfigurationError, match=message):
        parse_config(raw)


@pytest.mark.parametrize("endpoint", ["imap", "smtp"])
@pytest.mark.parametrize("port", [0, 65536, "993", True])
def test_invalid_ports_are_rejected(
    valid_config: dict[str, object], endpoint: str, port: object
) -> None:
    raw = copy.deepcopy(valid_config)
    raw["accounts"][1][endpoint]["port"] = port  # type: ignore[index]

    with pytest.raises(ConfigurationError, match=r"port must be an integer"):
        parse_config(raw)


@pytest.mark.parametrize(
    ("field_path", "message"),
    [
        (("username",), "username is required"),
        (("secret_ref",), "secret_ref is required"),
        (("imap",), "imap must be a TOML table"),
        (("smtp",), "smtp must be a TOML table"),
        (("imap", "host"), "host is required"),
        (("smtp", "tls"), "tls must be boolean"),
    ],
)
def test_missing_imap_smtp_fields_are_rejected(
    valid_config: dict[str, object], field_path: tuple[str, ...], message: str
) -> None:
    raw = copy.deepcopy(valid_config)
    account = raw["accounts"][1]  # type: ignore[index]
    if len(field_path) == 1:
        account.pop(field_path[0])
    else:
        account[field_path[0]].pop(field_path[1])

    with pytest.raises(ConfigurationError, match=message):
        parse_config(raw)


@pytest.mark.parametrize("field", ["password", "token", "access-token"])
def test_plaintext_credentials_are_rejected_without_disclosure(
    valid_config: dict[str, object], field: str
) -> None:
    raw = copy.deepcopy(valid_config)
    raw["accounts"][1][field] = "do-not-disclose"  # type: ignore[index]

    with pytest.raises(ConfigurationError) as captured:
        parse_config(raw)

    assert "Plaintext credential field" in str(captured.value)
    assert "do-not-disclose" not in str(captured.value)


def test_secret_value_is_redacted_from_normal_representations() -> None:
    value = SecretValue("highly-sensitive")

    assert str(value) == "[REDACTED]"
    assert "highly-sensitive" not in repr(value)
    assert value.reveal() == "highly-sensitive"


def test_non_macos_keychain_lookup_has_actionable_error() -> None:
    provider = KeychainSecretProvider(platform="linux")

    with pytest.raises(SecretResolutionError, match="supported only on macOS"):
        provider.resolve(SecretReference("mail-login"))


def test_keychain_command_uses_named_reference_and_redacts_failures() -> None:
    commands: list[list[str]] = []

    def failed_runner(command):
        commands.append(list(command))
        return subprocess.CompletedProcess(
            command, returncode=44, stdout="exposed-secret", stderr="exposed-secret"
        )

    provider = KeychainSecretProvider(
        "personal-mail-test", platform="darwin", runner=failed_runner
    )
    with pytest.raises(SecretResolutionError) as captured:
        provider.resolve(SecretReference("work-mail"))

    assert commands == [
        [
            "security",
            "find-generic-password",
            "-s",
            "personal-mail-test",
            "-a",
            "work-mail",
            "-w",
        ]
    ]
    assert "exposed-secret" not in str(captured.value)


def test_keychain_resolves_value_into_redacted_wrapper() -> None:
    def successful_runner(command):
        return subprocess.CompletedProcess(
            command, returncode=0, stdout="resolved-credential\n", stderr=""
        )

    provider = KeychainSecretProvider(platform="darwin", runner=successful_runner)

    resolved = provider.resolve(SecretReference("work-mail"))

    assert resolved.reveal() == "resolved-credential"
    assert "resolved-credential" not in repr(resolved)


@dataclass
class FakeSecretProvider:
    expected_reference: str
    value: str

    def resolve(self, reference: SecretReference) -> SecretValue:
        assert reference.name == self.expected_reference
        return SecretValue(self.value)


def test_configuration_accepts_injected_secret_provider(
    valid_config: dict[str, object],
) -> None:
    config = parse_config(valid_config)
    provider = FakeSecretProvider("work-mail", "fake-credential")

    resolved = config.resolve_account_secret("work", provider)

    assert resolved.reveal() == "fake-credential"


def test_injected_provider_exception_cannot_leak_secret(
    valid_config: dict[str, object],
) -> None:
    class LeakyProvider:
        def resolve(self, reference: SecretReference) -> SecretValue:
            raise RuntimeError("provider leaked fake-credential")

    config = parse_config(valid_config)

    with pytest.raises(SecretResolutionError) as captured:
        config.resolve_account_secret("work", LeakyProvider())

    assert "fake-credential" not in str(captured.value)
