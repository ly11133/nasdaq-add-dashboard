#!/usr/bin/env python3
"""Export a small, read-only snapshot bundle for static hosting.

The dashboard normally reads the local Python API.  GitHub Pages cannot run
that API, so this script copies the already-produced observations into one
sanitized JSON file.  It never copies the SQLite database or backup files.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen


def fetch(base: str, path: str) -> Any:
    request = Request(base.rstrip("/") + path, headers={"Accept": "application/json"})
    with urlopen(request, timeout=30) as response:
        return json.load(response)


def public(value: Any) -> Any:
    """Remove local filesystem paths before data is made public."""

    if isinstance(value, dict):
        return {
            key: "published static snapshot" if key == "audit_directory" else public(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [public(item) for item in value]
    if isinstance(value, str) and "/Users/" in value:
        return "[local path omitted]"
    return value


def build(base: str) -> dict[str, Any]:
    profiles = fetch(base, "/api/profiles")
    histories: dict[str, Any] = {}
    snapshots: dict[str, dict[str, Any]] = {}
    latest: dict[str, str] = {}
    for profile in profiles:
        profile_id = profile["id"]
        history = fetch(base, "/api/history?" + urlencode({"profile": profile_id}))
        histories[profile_id] = history
        snapshots[profile_id] = {}
        if history:
            latest[profile_id] = history[0]["id"]
        for record in history:
            query = urlencode({"profile": profile_id, "id": record["id"]})
            snapshots[profile_id][record["id"]] = public(fetch(base, "/api/snapshot?" + query))

    return {
        "schema": "PUBLIC_STATIC_SNAPSHOT_V1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "profiles": public(profiles),
        "history": public(histories),
        "latest": latest,
        "snapshots": snapshots,
        "real_world": public(fetch(base, "/api/real-world-profiles")),
        "phase2e": public(fetch(base, "/api/phase2e-status")),
        "phase2f": public(fetch(base, "/api/phase2f-status")),
        "phase3a": public(fetch(base, "/api/phase3a-status")),
        "phase3b": public(fetch(base, "/api/phase3b-status")),
        "status": public(fetch(base, "/api/status")),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://127.0.0.1:8766", help="running local dashboard URL")
    parser.add_argument("--output", default=str(Path(__file__).with_name("static-data.json")))
    args = parser.parse_args()
    payload = build(args.base)
    output = Path(args.output)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {output} ({output.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
