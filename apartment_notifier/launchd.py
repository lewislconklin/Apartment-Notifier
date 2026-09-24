from __future__ import annotations

import os
from pathlib import Path
import platform
import plistlib
import subprocess
import sys
import tempfile


LABEL = "com.apartment-notifier.monitor"


class LaunchdError(RuntimeError):
    pass


def launch_agent_definition(
    *,
    project_directory: Path,
    python_executable: Path,
    config_path: Path,
    stdout_path: Path,
    stderr_path: Path,
) -> dict[str, object]:
    return {
        "Label": LABEL,
        "ProgramArguments": [
            str(python_executable.resolve()),
            "-m",
            "apartment_notifier",
            "--config",
            str(config_path.resolve()),
            "run",
        ],
        "WorkingDirectory": str(project_directory.resolve()),
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 10,
        "ProcessType": "Background",
        "EnvironmentVariables": {"PYTHONUNBUFFERED": "1"},
        "StandardOutPath": str(stdout_path.resolve()),
        "StandardErrorPath": str(stderr_path.resolve()),
    }


def install(config_path: Path) -> Path:
    _require_macos()
    raw_config_path = os.path.expandvars(os.path.expanduser(str(config_path)))
    if "$" in raw_config_path:
        raise LaunchdError(
            f"Config path contains an unset environment variable: {raw_config_path}"
        )
    config_path = Path(raw_config_path).resolve()
    project_directory = Path(__file__).resolve().parents[1]
    logs_directory = Path.home() / "Library" / "Logs" / "Apartment Notifier"
    logs_directory.mkdir(parents=True, exist_ok=True)
    agent_path = _agent_path()
    agent_path.parent.mkdir(parents=True, exist_ok=True)
    definition = launch_agent_definition(
        project_directory=project_directory,
        python_executable=Path(sys.executable),
        config_path=config_path,
        stdout_path=logs_directory / "notifier.log",
        stderr_path=logs_directory / "notifier-error.log",
    )
    _write_plist(agent_path, definition)

    domain = f"gui/{os.getuid()}"
    subprocess.run(
        ["launchctl", "bootout", domain, str(agent_path)],
        check=False,
        capture_output=True,
        text=True,
    )
    _launchctl(["bootstrap", domain, str(agent_path)])
    return agent_path


def start() -> Path:
    _require_macos()
    agent_path = _agent_path()
    if not agent_path.exists():
        raise LaunchdError(
            "Launch agent is not installed. Run install-launch-agent first."
        )
    domain = f"gui/{os.getuid()}"
    subprocess.run(
        ["launchctl", "bootout", domain, str(agent_path)],
        check=False,
        capture_output=True,
        text=True,
    )
    _launchctl(["bootstrap", domain, str(agent_path)])
    return agent_path


def stop() -> Path:
    _require_macos()
    agent_path = _agent_path()
    if not agent_path.exists():
        raise LaunchdError(
            "Launch agent is not installed. Run install-launch-agent first."
        )
    domain = f"gui/{os.getuid()}"
    subprocess.run(
        ["launchctl", "bootout", domain, str(agent_path)],
        check=False,
        capture_output=True,
        text=True,
    )
    return agent_path


def uninstall() -> Path:
    _require_macos()
    agent_path = _agent_path()
    domain = f"gui/{os.getuid()}"
    if agent_path.exists():
        subprocess.run(
            ["launchctl", "bootout", domain, str(agent_path)],
            check=False,
            capture_output=True,
            text=True,
        )
        agent_path.unlink()
    return agent_path


def status() -> str:
    _require_macos()
    domain_target = f"gui/{os.getuid()}/{LABEL}"
    try:
        result = subprocess.run(
            ["launchctl", "print", domain_target],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "not loaded").strip()
        raise LaunchdError(f"Launch agent is not running: {detail}") from exc
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise LaunchdError(f"Could not inspect launchd: {exc}") from exc
    return result.stdout


def _agent_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def _write_plist(path: Path, definition: dict[str, object]) -> None:
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("wb", dir=path.parent, delete=False) as handle:
            plistlib.dump(definition, handle, sort_keys=True)
            temporary_path = Path(handle.name)
        temporary_path.chmod(0o644)
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def _launchctl(arguments: list[str]) -> None:
    try:
        subprocess.run(
            ["launchctl", *arguments],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except FileNotFoundError as exc:
        raise LaunchdError("The launchctl command was not found") from exc
    except subprocess.TimeoutExpired as exc:
        raise LaunchdError("launchctl did not respond within 30 seconds") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "unknown launchctl error").strip()
        raise LaunchdError(f"launchctl failed: {detail}") from exc


def _require_macos() -> None:
    if platform.system() != "Darwin":
        raise LaunchdError("launchd management requires macOS")
