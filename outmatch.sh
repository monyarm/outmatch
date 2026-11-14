#!/usr/bin/env bash
# outmatch: match functions outside decompdecomp, submit matches through its live API.
# Runs from anywhere; the harness needs to live in this directory for its module imports.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$here"
exec "${PYTHON:-python3}" -m harness.cli "$@"
