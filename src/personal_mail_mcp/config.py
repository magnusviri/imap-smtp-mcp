"""Validated configuration for the local personal-mail server."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised on Python 3.10
    import tomli as tomllib

from personal_mail_mcp.secrets import (
    KeychainSecretProvider,
    SecretProvider,
    SecretReference,
    SecretResolutionError,
    SecretValue,
)

_ACCOUNT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_PLAINTEXT_SECRET_KEYS = {
    "password",
    "passwd",
    "token",
    "access_token",
    "refresh_token",
    "api_key",
    "secret",
}


class ConfigurationError(ValueError):
    """A safe, user-actionable configuration error."""


@dataclass(frozen=True)
class ServerSettings:
    """Settings shared by all local accounts."""

    request_timeout_seconds: float = 30.0
    keychain_service: str = "personal-mail-mcp"


@dataclass(frozen=True)
class EndpointSettings:
    """A TLS mail endpoint."""

    host: str
    port: int
    tls: bool


@dataclass(frozen=True)
class AppleMailAccount:
    """A configured account owned by macOS Mail.app."""

    id: str
    type: Literal["apple-mail"]
    sender_addresses: tuple[str, ...]
    mailboxes: tuple[str, ...]
    mail_account: str


@dataclass(frozen=True)
class ImapSmtpAccount:
    """A configured generic IMAP and SMTP account."""

    id: str
    type: Literal["imap-smtp"]
    sender_addresses: tuple[str, ...]
    mailboxes: tuple[str, ...]
    username: str
    secret_ref: SecretReference
    imap: EndpointSettings
    smtp: EndpointSettings


Account = AppleMailAccount | ImapSmtpAccount


@dataclass(frozen=True)
class PersonalMailConfig:
    """Complete validated configuration with no resolved credential material."""

    server: ServerSettings
    accounts: tuple[Account, ...]

    def account(self, account_id: str) -> Account:
        """Return one configured account by its stable ID."""
        try:
            return next(
                account for account in self.accounts if account.id == account_id
            )
        except StopIteration:
            raise ConfigurationError(f"Unknown account ID {account_id!r}.") from None

    def resolve_account_secret(
        self,
        account_id: str,
        provider: SecretProvider | None = None,
    ) -> SecretValue:
        """Resolve an IMAP/SMTP credential with an injectable provider."""
        account = self.account(account_id)
        if not isinstance(account, ImapSmtpAccount):
            raise ConfigurationError(
                f"Account {account_id!r} does not use a credential reference."
            )
        selected_provider = provider or KeychainSecretProvider(
            self.server.keychain_service
        )
        try:
            return selected_provider.resolve(account.secret_ref)
        except SecretResolutionError:
            raise
        # Provider implementations are external to this model. Sanitize every
        # provider failure so its message cannot disclose credential material.
        except Exception:  # noqa: BLE001
            raise SecretResolutionError(
                f"Could not resolve secret reference {account.secret_ref.name!r}."
            ) from None


def load_config(path: str | Path) -> PersonalMailConfig:
    """Read and validate a personal-mail TOML configuration file."""
    config_path = Path(path)
    try:
        with config_path.open("rb") as config_file:
            raw = tomllib.load(config_file)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ConfigurationError(
            f"Could not read configuration file {str(config_path)!r}: "
            f"{error.__class__.__name__}."
        ) from None
    return parse_config(raw)


def parse_config(raw: Mapping[str, Any]) -> PersonalMailConfig:
    """Validate an already decoded TOML mapping."""
    _reject_plaintext_secrets(raw)
    server = _parse_server(_mapping(raw.get("server", {}), "server"))
    raw_accounts = raw.get("accounts")
    if not isinstance(raw_accounts, list) or not raw_accounts:
        raise ConfigurationError("At least one [[accounts]] entry is required.")

    accounts: list[Account] = []
    seen_ids: set[str] = set()
    seen_addresses: set[str] = set()
    for index, value in enumerate(raw_accounts):
        data = _mapping(value, f"accounts[{index}]")
        account_id = _required_string(data, "id", f"accounts[{index}]")
        if not _ACCOUNT_ID_RE.fullmatch(account_id):
            raise ConfigurationError(f"Account ID {account_id!r} is invalid.")
        normalized_id = account_id.casefold()
        if normalized_id in seen_ids:
            raise ConfigurationError(f"Duplicate account ID {account_id!r}.")
        seen_ids.add(normalized_id)

        sender_addresses = _string_list(
            data, "sender_addresses", account_id, validate_email=True
        )
        for address in sender_addresses:
            normalized_address = address.casefold()
            if normalized_address in seen_addresses:
                raise ConfigurationError(f"Duplicate sender address {address!r}.")
            seen_addresses.add(normalized_address)
        mailboxes = _string_list(data, "mailboxes", account_id)

        account_type = _required_string(data, "type", account_id)
        common = {
            "id": account_id,
            "sender_addresses": sender_addresses,
            "mailboxes": mailboxes,
        }
        if account_type == "apple-mail":
            accounts.append(
                AppleMailAccount(
                    type="apple-mail",
                    mail_account=_required_string(data, "mail_account", account_id),
                    **common,
                )
            )
        elif account_type == "imap-smtp":
            accounts.append(
                ImapSmtpAccount(
                    type="imap-smtp",
                    username=_required_string(data, "username", account_id),
                    secret_ref=SecretReference(
                        _required_string(data, "secret_ref", account_id)
                    ),
                    imap=_parse_endpoint(data.get("imap"), account_id, "imap"),
                    smtp=_parse_endpoint(data.get("smtp"), account_id, "smtp"),
                    **common,
                )
            )
        else:
            raise ConfigurationError(
                f"Account {account_id!r} has unknown type {account_type!r}; "
                "expected 'apple-mail' or 'imap-smtp'."
            )
    return PersonalMailConfig(server=server, accounts=tuple(accounts))


def _parse_server(data: Mapping[str, Any]) -> ServerSettings:
    timeout = data.get("request_timeout_seconds", 30.0)
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or timeout <= 0
    ):
        raise ConfigurationError("server.request_timeout_seconds must be positive.")
    service = data.get("keychain_service", "personal-mail-mcp")
    if not isinstance(service, str) or not service.strip():
        raise ConfigurationError("server.keychain_service must be a non-empty string.")
    return ServerSettings(float(timeout), service.strip())


def _parse_endpoint(value: Any, account_id: str, name: str) -> EndpointSettings:
    data = _mapping(value, f"account {account_id!r} {name}")
    host = _required_string(data, "host", f"account {account_id!r} {name}")
    port = data.get("port")
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ConfigurationError(
            f"Account {account_id!r} {name}.port must be an integer from 1 to 65535."
        )
    tls = data.get("tls")
    if not isinstance(tls, bool):
        raise ConfigurationError(f"Account {account_id!r} {name}.tls must be boolean.")
    return EndpointSettings(host=host, port=port, tls=tls)


def _mapping(value: Any, location: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ConfigurationError(f"{location} must be a TOML table.")
    return value


def _required_string(data: Mapping[str, Any], key: str, location: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(f"{location}.{key} is required and must be a string.")
    return value.strip()


def _string_list(
    data: Mapping[str, Any],
    key: str,
    account_id: str,
    *,
    validate_email: bool = False,
) -> tuple[str, ...]:
    value = data.get(key)
    if not isinstance(value, list) or not value:
        raise ConfigurationError(
            f"Account {account_id!r} {key} must be a non-empty list."
        )
    if any(not isinstance(item, str) or not item.strip() for item in value):
        raise ConfigurationError(
            f"Account {account_id!r} {key} must contain only non-empty strings."
        )
    result = tuple(item.strip() for item in value)
    normalized = [item.casefold() for item in result]
    if len(normalized) != len(set(normalized)):
        raise ConfigurationError(f"Account {account_id!r} has duplicate {key}.")
    if validate_email and any(not _EMAIL_RE.fullmatch(item) for item in result):
        raise ConfigurationError(
            f"Account {account_id!r} has an invalid sender address."
        )
    return result


def _reject_plaintext_secrets(value: Any, location: str = "configuration") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized_key = str(key).casefold().replace("-", "_")
            if normalized_key in _PLAINTEXT_SECRET_KEYS:
                raise ConfigurationError(
                    f"Plaintext credential field {str(key)!r} is forbidden at {location}; "
                    "use secret_ref with the operating system credential store."
                )
            _reject_plaintext_secrets(child, f"{location}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_plaintext_secrets(child, f"{location}[{index}]")
