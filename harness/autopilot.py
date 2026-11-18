"""Start, stop or read decompdecomp's autopilot from outside the app (/api/autopilot, the steering panel's button).

`--all` works on every project the app knows, from any folder: it never looks for the project outmatch is run in
(nor at OUTMATCH_PROJECT), so it can't import harness.common, which does at import time.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import urllib.error
import urllib.request
from pathlib import Path

APP_HOME = Path(os.environ.get("DECOMPNET_HOME", Path.home() / "Decomp" / ".decomp"))


def api(path: str, body: dict | None = None) -> dict:
    info = json.loads((APP_HOME / "server.json").read_text(encoding="utf-8"))
    req = urllib.request.Request(f"http://127.0.0.1:{info['port']}{path}",
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"User-Agent": f"decompdecomp-window/{info['secret']}", "X-Decomp": "1",
                                          "Content-Type": "application/json"},
                                 method="POST" if body is not None else "GET")
    with urllib.request.urlopen(req, timeout=120) as res:
        return json.loads(res.read().decode("utf-8") or "{}")


def slugs() -> list[str]:
    with sqlite3.connect(f"file:{APP_HOME / 'decomp.db'}?mode=ro", uri=True) as db:
        return [s for (s,) in db.execute("SELECT slug FROM projects ORDER BY id")]


def one(slug: str, action: str) -> dict:
    try:
        ov = api("/api/autopilot", {"slug": slug, "action": action}) if action != "status" else \
            api(f"/api/project?slug={slug}")
    except (urllib.error.URLError, OSError, ValueError) as e:
        body = e.read().decode("utf-8", "replace")[:300] if isinstance(e, urllib.error.HTTPError) else str(e)
        return {"slug": slug, "error": body}
    ap = ov.get("autopilot") or {}
    return {"slug": slug, **{k: ap.get(k) for k in ("state", "phase", "twins", "matched", "tried", "total",
                                                    "no_drafts") if ap.get(k) not in (None, "")}}


def main(argv: list[str]) -> int:
    action = "stop" if "--stop" in argv else "status" if "--status" in argv else "start"
    if "--all" in argv:
        targets = slugs()
    else:
        from . import common  # the project this is run in (or OUTMATCH_PROJECT / OUTMATCH_SLUG)
        targets = [common.SLUG]
    bad = 0
    for slug in targets:
        row = one(slug, action)
        bad += "error" in row
        print(json.dumps(row), flush=True)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
