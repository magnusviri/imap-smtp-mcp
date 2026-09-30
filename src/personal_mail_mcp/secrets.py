"""Secret-provider interfaces and macOS Keychain integration."""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol


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


CommandRunner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


class KeychainSecretProvider:
    """Resolve generic-password entries from the current user's macOS Keychain."""

    def __init__(
        self,
        service: str = "personal-mail-mcp",
        *,
        platform: str | None = None,
        runner: CommandRunner | None = None,
    ) -> None:
        self._service = service
        self._platform = platform or sys.platform
        self._runner = runner or self._run_security

    def resolve(self, reference: SecretReference) -> SecretValue:
        if self._platform != "darwin":
            raise SecretResolutionError(
                "macOS Keychain references are supported only on macOS. "
                "Run personal-mail-mcp on macOS or inject a compatible secret provider."
            )
        try:
            result = self._runner(
                [
                    "security",
                    "find-generic-password",
                    "-s",
                    self._service,
                    "-a",
                    reference.name,
                    "-w",
                ]
            )
        except (OSError, subprocess.SubprocessError):
            raise SecretResolutionError(
                f"Could not access macOS Keychain entry {reference.name!r}. "
                "Verify that the security command is available."
            ) from None
        if result.returncode != 0:
            raise SecretResolutionError(
                f"macOS Keychain entry {reference.name!r} was not found or could not "
                "be read. Add it with the documented security command."
            )
        value = result.stdout.rstrip("\r\n")
        if not value:
            raise SecretResolutionError(
                f"macOS Keychain entry {reference.name!r} is empty."
            )
        return SecretValue(value)

    @staticmethod
    def _run_security(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
        )
