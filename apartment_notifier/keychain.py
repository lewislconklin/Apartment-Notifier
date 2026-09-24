from __future__ import annotations

import platform
import subprocess


class KeychainError(RuntimeError):
    pass


def read_password(service: str, account: str) -> str:
    _require_macos()
    try:
        result = subprocess.run(
            [
                "security",
                "find-generic-password",
                "-s",
                service,
                "-a",
                account,
                "-w",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except FileNotFoundError as exc:
        raise KeychainError("The macOS security command was not found") from exc
    except subprocess.TimeoutExpired as exc:
        raise KeychainError("macOS Keychain did not respond within 30 seconds") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or "Keychain item not found").strip()
        raise KeychainError(
            f"Could not read Keychain item for service {service!r} and "
            f"account {account!r}: {detail}"
        ) from exc
    value = result.stdout.rstrip("\r\n")
    if not value:
        raise KeychainError("The matching Keychain password is empty")
    return value


def store_password(service: str, account: str, password: str) -> None:
    _require_macos()
    if not password:
        raise KeychainError("Cannot store an empty password")
    try:
        subprocess.run(
            [
                "security",
                "add-generic-password",
                "-U",
                "-s",
                service,
                "-a",
                account,
                "-w",
            ],
            check=True,
            capture_output=True,
            input=f"{password}\n",
            text=True,
            timeout=30,
        )
    except FileNotFoundError as exc:
        raise KeychainError("The macOS security command was not found") from exc
    except subprocess.TimeoutExpired as exc:
        raise KeychainError("macOS Keychain did not respond within 30 seconds") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or "unknown Keychain error").strip()
        raise KeychainError(f"Could not save the Keychain item: {detail}") from exc


def _require_macos() -> None:
    if platform.system() != "Darwin":
        raise KeychainError("macOS Keychain integration requires macOS")
