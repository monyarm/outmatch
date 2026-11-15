#!/usr/bin/env bash
# outmatch: match functions outside decompdecomp, submit matches through its live API.
# Run it from inside the decompdecomp project to work on (or set OUTMATCH_PROJECT); it finds the harness beside itself.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PYTHONPATH="$here${PYTHONPATH:+:$PYTHONPATH}"
exec "${PYTHON:-python3}" -m harness.cli "$@"
