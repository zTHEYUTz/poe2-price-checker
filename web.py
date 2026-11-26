"""Local web UI for the POE2 build price checker.

Run:  python web.py   then open http://127.0.0.1:8765
Only listens on localhost. Reuses the pricing logic in poe2_price.py.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import threading
import time
import webbrowser
from urllib.parse import parse_qs, urlparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

import httpx

import poe2_price as pp

HOST, PORT = "127.0.0.1", int(os.environ.get("PORT", "8765"))
INDEX = Path(__file__).with_name("index.html")
BUILDS_FILE = Path(os.environ.get("POE2_BUILDS_FILE") or Path(__file__).with_name("saved_builds.json"))


class BuildStore:
    """Saved builds in a small JSON file next to the app (thread-safe)."""

    MAX_BUILDS = 300
    MAX_SOURCE = 500_000
    MAX_HISTORY = 200

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    def _load(self) -> list[dict[str, Any]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except (OSError, ValueError):
            return []

    def _save(self, builds: list[dict[str, Any]]) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(builds, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    def all(self) -> list[dict[str, Any]]:
        with self._lock:
            return self._load()

    def upsert(self, data: dict[str, Any]) -> dict[str, Any]:
        source = str(data.get("source") or "").strip()
        if not source:
            raise ValueError("ไม่มีโค้ดบิวให้บันทึก")
        if len(source) > self.MAX_SOURCE:
            raise ValueError("โค้ดบิวยาวเกินไป")
        with self._lock:
            builds = self._load()
            bid = str(data.get("id") or "")
            cur = next((b for b in builds if (bid and b.get("id") == bid) or b.get("source") == source), None)
            if cur is None:
                if len(builds) >= self.MAX_BUILDS:
                    raise ValueError(f"บันทึกได้สูงสุด {self.MAX_BUILDS} บิว — ลบบิวเก่าก่อน")
                cur = {"id": os.urandom(6).hex(), "source": source, "created": time.time()}
                builds.insert(0, cur)
            for key, limit in (("name", 100), ("title", 120), ("league", 80)):
                if data.get(key) is not None:
                    cur[key] = str(data[key])[:limit]
            for key in ("total", "ex", "div"):
                if isinstance(data.get(key), (int, float)):
                    cur[key] = float(data[key])
            if data.get("checked"):
                cur["checked"] = time.time()
                if isinstance(data.get("total"), (int, float)):
                    # one point per price check, newest last
                    hist = cur.setdefault("history", [])
                    hist.append({"t": cur["checked"], "total": float(data["total"]),
                                 "div": cur.get("div"), "ex": cur.get("ex")})
                    del hist[:-self.MAX_HISTORY]
            cur["source"] = source
            self._save(builds)
            return cur

    def delete(self, bid: str) -> bool:
        with self._lock:
            builds = self._load()
            keep = [b for b in builds if b.get("id") != bid]
            if len(keep) == len(builds):
                return False
            self._save(keep)
            return True


BUILDS = BuildStore(BUILDS_FILE)
SITE_HELP = {
    "mobalytics.gg": "เปิดหน้าบิวในเบราว์เซอร์ → หาปุ่ม \"PoB\" / \"Export to PoB\" (มักอยู่ใกล้ชื่อบิวหรือส่วน Equipment) "
                     "→ กดคัดลอกโค้ด → กลับมาวางในช่องนี้",
    "maxroll.gg": "เปิดหน้าบิว → กดปุ่ม \"PoB\" / \"Export\" ในส่วน Planner → คัดลอกโค้ด → กลับมาวางในช่องนี้",
}
ALLOWED_URL = re.compile(r"https?://(pobb\.in|pastebin\.com)/", re.I)


def make_client() -> httpx.AsyncClient:
    headers = {"User-Agent": os.environ.get("POE2_PRICER_UA", pp.DEFAULT_UA)}
    cookies = {"POESESSID": s} if (s := os.environ.get("POESESSID")) else None
    return httpx.AsyncClient(headers=headers, cookies=cookies, timeout=30)


async def load_source(source: str, client: httpx.AsyncClient) -> str:
    """Load PoB XML from a code or a pobb.in/pastebin link (no local files)."""
    source = source.strip()
    if source.startswith(("http://", "https://")):
        pp.raw_url_for(source)  # raises a friendly error for poe.ninja
        for host, how in SITE_HELP.items():
            if re.match(rf"https?://(www\.)?{re.escape(host)}/", source, re.I):
                raise ValueError(f"{host} กันโปรแกรมภายนอกอ่านหน้าเว็บ จึงดึงบิวจากลิงก์ไม่ได้ — {how}")
        if not ALLOWED_URL.match(source):
            raise ValueError("รองรับเฉพาะลิงก์ pobb.in หรือ pastebin.com (หรือวาง PoB code ตรง ๆ)")
        return await pp.load_build_xml(source, client)
    if source.startswith("<"):
        return source
    return pp.decode_pob_code(source)


_ninja_cache: dict[str, tuple[float, pp.NinjaPrices]] = {}
_cache_lock = threading.Lock()
NINJA_TTL = 600.0
_trade_stats: dict[str, dict[str, str]] = {}
_trade_last = [0.0]


async def resolve_league(league: str, client: httpx.AsyncClient) -> str:
    if league:
        return league
    r = await client.get(f"{pp.NINJA}/leagues")
    return str(r.json()[0]["id"])


async def get_ninja(league: str, client: httpx.AsyncClient) -> pp.NinjaPrices:
    """poe.ninja prices for a league, cached for NINJA_TTL seconds."""
    with _cache_lock:
        hit = _ninja_cache.get(league)
    if hit and time.monotonic() - hit[0] < NINJA_TTL:
        return hit[1]
    ninja = await pp.fetch_ninja(client, league)
    with _cache_lock:
        _ninja_cache[league] = (time.monotonic(), ninja)
    return ninja


async def make_trade(client: httpx.AsyncClient, league: str) -> pp.TradeClient:
    """Trade client sharing mod data and rate-limit timing across requests."""
    trade = pp.TradeClient(client, league, 6.0, 1.5)
    trade._last = _trade_last[0]
    if _trade_stats:
        trade.stats = _trade_stats
    else:
        await trade.load_stats()
        _trade_stats.update(trade.stats)
    return trade


def rates(ninja: pp.NinjaPrices) -> dict[str, Any]:
    return {"primary": ninja.primary, "ex": ninja.currency("Exalted Orb"),
            "div": ninja.currency("Divine Orb")}


async def get_catalog(league: str) -> dict[str, Any]:
    async with make_client() as client:
        league = await resolve_league(league, client)
        ninja = await get_ninja(league, client)
    return {"league": league, **rates(ninja), "items": ninja.catalog}


async def price_item_text(opts: dict[str, Any], emit: Callable[[dict[str, Any]], None]) -> None:
    """Price a single item copied from the game, streaming progress events."""
    item, gem_level = pp.parse_game_item(str(opts.get("text", "")))
    async with make_client() as client:
        league = await resolve_league(str(opts.get("league") or ""), client)
        emit({"type": "item", "league": league, "name": item.name, "base": item.base,
              "rarity": item.rarity, "slot": item.slot, "corrupted": item.corrupted,
              "mods": [m.__dict__ for m in item.mods]})
        emit({"type": "status", "msg": "กำลังดึงราคาจาก poe.ninja…"})
        ninja = await get_ninja(league, client)
        emit({"type": "rates", **rates(ninja)})

        line: pp.PriceLine
        if item.rarity == "UNIQUE":
            line = pp.price_unique(item, ninja)
        elif item.rarity == "GEM":
            line = pp.price_gem(pp.Gem(item.name, gem_level or 1, "Support" in item.slot), ninja)
        elif item.rarity in ("RARE", "MAGIC"):
            emit({"type": "status", "msg": "กำลังค้นของที่ mod ใกล้เคียงในเทรดไซต์… (ราว 10–30 วินาที)"})
            trade = await make_trade(client, league)
            try:
                line = await trade.price_item(item, float(opts.get("ratio") or 0.8), 6, ninja)
            finally:
                _trade_last[0] = trade._last
        else:
            val = ninja.currency(item.name)
            if val is None:
                val = ninja.currency(item.base)
            line = pp.PriceLine(item.slot, item.name, val, "poe.ninja" if val is not None else "-",
                                "" if val is not None else "ไม่พบราคาใน poe.ninja (ไอเทมธรรมดามักไม่มีราคาตลาด)",
                                query=pp.name_query(item.name, item.base, item.rarity),
                                icon=ninja.icon(item.name, item.base),
                                trend=ninja.trend(item.name, item.base))
        emit({"type": "line", **line.__dict__})
        emit({"type": "done"})


async def find_listings(opts: dict[str, Any], emit: Callable[[dict[str, Any]], None]) -> None:
    """Search the trade site again (cheapest online first) for one item."""
    query = opts.get("query")
    if not isinstance(query, dict) or not query:
        raise ValueError("ไม่มีข้อมูลสำหรับค้นในเทรด")
    async with make_client() as client:
        league = await resolve_league(str(opts.get("league") or ""), client)
        ninja = await get_ninja(league, client)
        emit({"type": "rates", **rates(ninja)})
        emit({"type": "status", "msg": "กำลังหาร้านที่ถูกที่สุด…"})
        trade = await make_trade(client, league)
        try:
            search_id, listings = await trade.search(query, ninja, fresh=True)
            wanted = opts.get("wanted")
            if isinstance(wanted, list):
                pp.mark_matches(listings, [str(w) for w in wanted][:20])
        finally:
            _trade_last[0] = trade._last
        emit({"type": "listings", "search_url": pp.trade_search_url(league, search_id),
              "listings": listings[:5]})
        emit({"type": "done"})


async def get_leagues() -> list[dict[str, str]]:
    async with make_client() as client:
        r = await client.get(f"{pp.NINJA}/leagues")
        r.raise_for_status()
        return [{"id": str(x.get("id")), "name": str(x.get("name") or x.get("id"))}
                for x in r.json()]


async def price_build(opts: dict[str, Any], emit: Callable[[dict[str, Any]], None]) -> None:
    """Price a build, streaming progress events through ``emit``."""
    async with make_client() as client:
        emit({"type": "status", "msg": "กำลังโหลดบิว…"})
        build = pp.parse_build(await load_source(str(opts.get("source", "")), client))

        league = await resolve_league(str(opts.get("league") or ""), client)
        emit({"type": "build", "title": build.title, "league": league,
              "items": len(build.items), "gems": len(build.gems)})
        emit({"type": "status", "msg": "กำลังดึงราคาจาก poe.ninja…"})
        ninja = await get_ninja(league, client)
        emit({"type": "rates", **rates(ninja)})

        def send(line: pp.PriceLine) -> None:
            emit({"type": "line", **line.__dict__})

        trade: pp.TradeClient | None = None
        trade_error = ""
        no_rares = bool(opts.get("no_rares"))
        ratio = float(opts.get("ratio") or 0.8)
        for item in build.items:
            if item.rarity == "UNIQUE":
                send(pp.price_unique(item, ninja))
            elif item.rarity in ("RARE", "MAGIC") and not no_rares and not trade_error:
                emit({"type": "status", "msg": f"ค้นราคาในเทรด: {item.slot} — {item.name}"})
                try:
                    if trade is None:
                        trade = await make_trade(client, league)
                    send(await trade.price_item(item, ratio, 6, ninja))
                except (RuntimeError, httpx.HTTPError) as exc:
                    # trade site blocked/failing: keep pricing everything else
                    trade_error = str(exc) or exc.__class__.__name__
                    emit({"type": "warn", "msg": f"{trade_error} — ไอเทม rare จะข้ามไป ส่วนอื่นคำนวณต่อตามปกติ"})
                    send(pp.PriceLine(item.slot, f"{item.rarity.title()} {item.base}", None, "-",
                                      "ค้นในเทรดไม่ได้ตอนนี้"))
                finally:
                    if trade is not None:
                        _trade_last[0] = trade._last
            elif item.rarity in ("RARE", "MAGIC") and trade_error:
                send(pp.PriceLine(item.slot, f"{item.rarity.title()} {item.base}", None, "-",
                                  "ค้นในเทรดไม่ได้ตอนนี้"))
            else:
                note = "ข้ามไป (ปิดการค้น rare)" if item.rarity in ("RARE", "MAGIC") else "ข้ามไป"
                send(pp.PriceLine(item.slot, f"{item.rarity.title()} {item.base}", None, "-", note))
            for rune in item.runes:
                send(pp.PriceLine("Rune", f"{rune} ({item.slot})", ninja.currency(rune), "poe.ninja",
                                  query={"type": rune}, icon=ninja.icon(rune), trend=ninja.trend(rune)))
        for gem in build.gems:
            send(pp.price_gem(gem, ninja))
        emit({"type": "done"})


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: Any) -> None:  # quieter console
        pass

    def _json(self, code: int, data: Any) -> None:
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        url = urlparse(self.path)
        if url.path in ("/", "/index.html"):
            body = INDEX.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif url.path == "/api/catalog":
            league = parse_qs(url.query).get("league", [""])[0]
            try:
                self._json(200, asyncio.run(get_catalog(league)))
            except (httpx.HTTPError, ValueError, KeyError, IndexError) as exc:
                self._json(502, {"error": str(exc)})
        elif url.path == "/api/builds":
            self._json(200, BUILDS.all())
        elif url.path == "/api/leagues":
            try:
                self._json(200, asyncio.run(get_leagues()))
            except (httpx.HTTPError, ValueError) as exc:
                self._json(502, {"error": str(exc)})
        else:
            self._json(404, {"error": "not found"})

    def _read_json(self) -> dict[str, Any] | None:
        if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
            self._json(415, {"error": "expected JSON"})
            return None
        length = min(int(self.headers.get("Content-Length", "0")), 2_000_000)
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            data = None
        if not isinstance(data, dict):
            self._json(400, {"error": "bad JSON"})
            return None
        return data

    def do_POST(self) -> None:
        if self.path in ("/api/builds/save", "/api/builds/delete"):
            data = self._read_json()
            if data is None:
                return
            try:
                if self.path.endswith("save"):
                    self._json(200, BUILDS.upsert(data))
                else:
                    self._json(200, {"deleted": BUILDS.delete(str(data.get("id") or ""))})
            except (ValueError, OSError) as exc:
                self._json(400, {"error": str(exc)})
            return
        handler = {"/api/price": price_build, "/api/item": price_item_text,
                   "/api/listings": find_listings}.get(self.path)
        if handler is None:
            self._json(404, {"error": "not found"})
            return
        if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
            self._json(415, {"error": "expected JSON"})
            return
        length = min(int(self.headers.get("Content-Length", "0")), 2_000_000)
        try:
            opts = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._json(400, {"error": "bad JSON"})
            return

        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        def emit(ev: dict[str, Any]) -> None:
            self.wfile.write((json.dumps(ev, ensure_ascii=False) + "\n").encode())
            self.wfile.flush()

        try:
            asyncio.run(handler(opts, emit))
        except (BrokenPipeError, ConnectionResetError):
            pass  # browser closed / cancelled
        except (ValueError, httpx.HTTPError, RuntimeError, KeyError, IndexError) as exc:
            try:
                emit({"type": "error", "msg": str(exc) or exc.__class__.__name__})
            except OSError:
                pass


def main() -> None:
    url = f"http://{HOST}:{PORT}"
    open_browser = "--open" in sys.argv and not os.environ.get("POE2_NO_BROWSER")
    try:
        srv = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError:
        # port taken: most likely the app is already running
        print(f"พอร์ต {PORT} ถูกใช้อยู่ — โปรแกรมอาจเปิดอยู่แล้วที่ {url}")
        if open_browser:
            webbrowser.open(url)
        return
    print(f"POE2 Price Checker: {url}  (ปิดหน้าต่างนี้หรือกด Ctrl+C เพื่อปิดโปรแกรม)")
    if open_browser:
        threading.Timer(0.8, webbrowser.open, args=(url,)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
