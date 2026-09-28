#!/usr/bin/env bash
# Unit + integration tests (offline, ~30 s). The Claude-brain dry run is separate: ./vc dry-run
cd "$(dirname "$0")/.." && exec .venv/bin/python -m unittest discover -s tests -v "$@"
