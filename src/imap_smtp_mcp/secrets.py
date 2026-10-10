"""Secret-provider interfaces backed by Python keyring.alt."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from getpass import getpass
from pathlib import Path
from typing import Protocol

from keyrings.alt.file import EncryptedKeyring


logger = logging.getLogger(__name__)


class SecretResolutionError(RuntimeError):
    """A credential lookup failed without disclosing credential material."""


@dataclass(frozen=True)
class SecretReference:
    """A non-secret name used to locate an OS-managed credential."""

    name: str

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("Secret reference names cannot be empty.")


class SecretValue:
    """A resolved credential whose normal representations are always redacted."""

    __slots__ = ("__value",)

    def __init__(self, value: str) -> None:
        if not value:
            raise SecretResolutionError("The credential store returned an empty value.")
        self.__value = value

    def reveal(self) -> str:
        """Return the value only at the provider authentication boundary."""
        return self.__value

    def __repr__(self) -> str:
        return "SecretValue('[REDACTED]')"

    def __str__(self) -> str:
        return "[REDACTED]"


class SecretProvider(Protocol):
    """Resolves named references without persisting secrets in configuration."""

    def resolve(self, reference: SecretReference) -> SecretValue: ...


class KeyringAltSecretProvider:
    """Resolve secrets from the encrypted cross-platform keyrings.alt backend."""

    def __init__(self, service: str = "imap-smtp-mcp", *, backend=None) -> None:
        self._service = service
        self._backend = backend or EncryptedKeyring()

    def resolve(self, reference: SecretReference) -> SecretValue:
        try:
            value = self._backend.get_password(self._service, reference.name)
        except Exception:  # noqa: BLE001 - backend errors can contain secret paths
            raise SecretResolutionError(
                f"Could not access keyring entry {reference.name!r}."
            ) from None
        if not value:
            raise SecretResolutionError(
                f"Keyring entry {reference.name!r} was not found or is empty."
            )
        logger.info("Credential resolved from the encrypted keyring.")
        return SecretValue(value)


def store_main() -> None:
    """Store a secret without passing its value through argv or configuration."""
    import argparse

    parser = argparse.ArgumentParser(description="Store a imap-smtp-mcp secret.")
    parser.add_argument("reference", help="The config.toml secret_ref value")
    parser.add_argument("--service", default="imap-smtp-mcp")
    arguments = parser.parse_args()
    value = getpass("Credential or OAuth access token: ")
    if not value:
        raise SystemExit("Credential cannot be empty.")
    EncryptedKeyring().set_password(arguments.service, arguments.reference, value)


class FileSecretProvider:
    """Resolve one Docker-secret file per reference from a private directory."""

    def __init__(self, directory: str | Path) -> None:
        self._directory = Path(directory)

    def resolve(self, reference: SecretReference) -> SecretValue:
        if Path(reference.name).name != reference.name:
            raise SecretResolutionError("Secret reference is not a safe file name.")
        path = self._directory / reference.name
        try:
            if path.is_symlink() or not path.is_file():
                raise OSError
            value = path.read_text(encoding="utf-8").rstrip("\r\n")
        except OSError:
            raise SecretResolutionError(
                f"Secret file for reference {reference.name!r} was not found or could not be read."
            ) from None
        return SecretValue(value)
