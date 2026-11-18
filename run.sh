#!/bin/sh
set -eu

project_dir=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
cd "$project_dir"

if [ -x .venv/bin/python ]; then
    exec .venv/bin/python -B main.py
fi
exec python3 -B main.py
