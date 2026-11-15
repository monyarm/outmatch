#!/bin/sh
# Refresh the upstream reference copies in this folder (the skill reads them; they are pinned snapshots).
# Usage: ./fetch-upstream.sh
set -eu
cd "$(dirname "$0")"

get() {  # get <url> <file>
    printf '%s -> %s\n' "$1" "$2"
    curl -sSL --fail -o "$2" "$1"
}

get https://raw.githubusercontent.com/pablocpas/CMR2Decomp/main/CLAUDE.md cmr2-claude.md
get https://raw.githubusercontent.com/adamXbot/brickpark-decomp/main/docs/LEVERS.md brickpark-levers.md
get https://raw.githubusercontent.com/isledecomp/isle/master/CONTRIBUTING.md isle-contributing.md
for doc in concepts interventions discovery costs platforms; do
    get "https://raw.githubusercontent.com/isledecomp/reprobit/master/docs/$doc.md" "reprobit-$doc.md"
done

printf '\nDone. Sizes:\n'
wc -c ./*.md
