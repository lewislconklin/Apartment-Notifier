#!/bin/sh
set -eu

PROJECT_DIRECTORY=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
CHECK_ONLY=false

if [ "${1:-}" = "--check" ]; then
    CHECK_ONLY=true
elif [ "$#" -gt 0 ]; then
    echo "Usage: ./setup-macos.sh [--check]" >&2
    exit 2
fi

if [ "$(uname -s)" != "Darwin" ]; then
    echo "This setup helper supports macOS. The Python package can still be installed manually elsewhere." >&2
    exit 2
fi

python_is_compatible() {
    "$1" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1
}

find_python() {
    if [ -n "${APARTMENT_NOTIFIER_PYTHON:-}" ]; then
        if command -v "$APARTMENT_NOTIFIER_PYTHON" >/dev/null 2>&1 && python_is_compatible "$APARTMENT_NOTIFIER_PYTHON"; then
            command -v "$APARTMENT_NOTIFIER_PYTHON"
            return 0
        fi
        echo "APARTMENT_NOTIFIER_PYTHON is not Python 3.11 or newer: $APARTMENT_NOTIFIER_PYTHON" >&2
        return 1
    fi

    for candidate in python3.13 python3.12 python3.11 python3; do
        if command -v "$candidate" >/dev/null 2>&1 && python_is_compatible "$candidate"; then
            command -v "$candidate"
            return 0
        fi
    done
    return 1
}

if ! PYTHON=$(find_python); then
    cat >&2 <<'MESSAGE'
Apartment Notifier requires Python 3.11 or newer.

Install a current universal macOS installer from https://www.python.org/downloads/macos/
or, if you use Homebrew, run: brew install python@3.12

This works on both Apple Silicon and Intel Macs. Then run ./setup-macos.sh again.
MESSAGE
    exit 2
fi

VERSION=$($PYTHON -c 'import platform; print(platform.python_version())')
ARCHITECTURE=$(uname -m)
echo "Using $PYTHON (Python $VERSION, $ARCHITECTURE)."

if [ "$CHECK_ONLY" = true ]; then
    echo "Setup prerequisites are satisfied; no files were changed."
    exit 0
fi

VIRTUAL_ENVIRONMENT="$PROJECT_DIRECTORY/.venv"
if [ ! -x "$VIRTUAL_ENVIRONMENT/bin/python" ]; then
    echo "Creating $VIRTUAL_ENVIRONMENT"
    "$PYTHON" -m venv "$VIRTUAL_ENVIRONMENT"
fi

if ! python_is_compatible "$VIRTUAL_ENVIRONMENT/bin/python"; then
    echo "The existing .venv uses an unsupported Python version." >&2
    echo "Move .venv aside, then rerun ./setup-macos.sh." >&2
    exit 2
fi

echo "Installing Apartment Notifier into the local virtual environment..."
SITE_PACKAGES=$("$VIRTUAL_ENVIRONMENT/bin/python" -c 'import site; print(site.getsitepackages()[0])')
printf '%s\n' "$PROJECT_DIRECTORY" > "$SITE_PACKAGES/apartment-notifier.pth"

echo
echo "Setup complete."
if [ -f "$PROJECT_DIRECTORY/config.toml" ]; then
    echo "Your existing config.toml was preserved."
    echo "Next: ./apartment-notifier validate"
else
    echo "Next: ./apartment-notifier configure"
fi
