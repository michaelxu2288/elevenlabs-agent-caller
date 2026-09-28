#!/usr/bin/env bash
# (Re)create the project virtualenv. Safe to re-run; needs no root.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${PYTHON:-$(command -v python3.12 || command -v python3)}"
"$PY" -m venv "$HERE/.venv"
"$HERE/.venv/bin/pip" install -q --disable-pip-version-check -r "$HERE/requirements.lock"
echo "ok: $("$HERE/.venv/bin/python" -c 'import aiohttp,sys; print(sys.version.split()[0], "aiohttp", aiohttp.__version__)')"
