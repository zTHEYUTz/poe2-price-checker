"""Build the static GitHub Pages site (search tab with saved poe.ninja prices).

Usage:  python build_site.py [output_dir]      (default: _site)

Writes:
  <out>/index.html              the normal page, switched to static mode
  <out>/data/leagues.json       [{id, name, slug}]
  <out>/data/<slug>.json        {league, primary, ex, div, updated, items: [...]}
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx

import poe2_price as pp

HERE = Path(__file__).parent
STATIC_FLAG = "<script>window.POE2_STATIC = true;</script>\n</head>"


def slug(league_id: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", league_id.lower()).strip("-") or "league"


async def build(out: Path) -> int:
    data_dir = out / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    headers = {"User-Agent": os.environ.get("POE2_PRICER_UA", pp.DEFAULT_UA)}
    async with httpx.AsyncClient(headers=headers, timeout=30) as client:
        r = await client.get(f"{pp.NINJA}/leagues")
        r.raise_for_status()
        leagues = [{"id": str(x["id"]), "name": str(x.get("name") or x["id"]), "slug": slug(str(x["id"]))}
                   for x in r.json()]
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        written = []
        for lg in leagues:
            ninja = await pp.fetch_ninja(client, lg["id"])
            pp.mark_exchangeable(ninja.catalog, await pp.trade_exchange_ids(client))
            if not ninja.catalog:
                print(f"  ! {lg['id']}: no prices, skipped", file=sys.stderr)
                continue
            payload = {
                "league": lg["id"], "primary": ninja.primary,
                "ex": ninja.currency("Exalted Orb"), "div": ninja.currency("Divine Orb"),
                "chaos": ninja.currency("Chaos Orb"),
                "updated": now, "items": ninja.catalog,
            }
            (data_dir / f"{lg['slug']}.json").write_text(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
            written.append(lg)
            print(f"  {lg['id']}: {len(ninja.catalog)} items")
    if not written:
        print("no league data fetched", file=sys.stderr)
        return 1
    (data_dir / "leagues.json").write_text(json.dumps(written, ensure_ascii=False), encoding="utf-8")

    page = (HERE / "index.html").read_text(encoding="utf-8")
    assert "</head>" in page
    (out / "index.html").write_text(page.replace("</head>", STATIC_FLAG, 1), encoding="utf-8")
    (out / ".nojekyll").write_text("", encoding="utf-8")
    print(f"site written to {out}")
    return 0


def main() -> int:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "_site")
    try:
        return asyncio.run(build(out))
    except httpx.HTTPError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
