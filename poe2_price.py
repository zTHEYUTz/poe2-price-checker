"""POE2 build price checker.

Reads a Path of Building (PoE2) build from many sources, then prices every
equipped item, socketed jewel and gem with live market data:

* Uniques, runes, soul cores, lineage/uncut gems -> poe.ninja economy API
* Rare / magic items -> official trade site (trade2) search, based on mods

Usage:
    python poe2_price.py <pob-code | url | file> [--league LEAGUE]
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import re
import statistics
import sys
import threading
import time
import xml.etree.ElementTree as ET
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

NINJA = "https://poe.ninja/poe2/api/economy"
TRADE = "https://www.pathofexile.com/api/trade2"
DEFAULT_UA = "poe2-build-pricer/1.0 (personal use; contact: you@example.com)"

EXCHANGE_TYPES = [
    "Currency", "UncutGems", "LineageSupportGems", "Runes", "SoulCores",
    "Idols", "Essences", "Abyss", "Ritual", "Delirium", "Breach",
]
UNIQUE_TYPES = [
    "UniqueWeapons", "UniqueArmours", "UniqueAccessories", "UniqueFlasks",
    "UniqueCharms", "UniqueJewels",
]
# Trade-site currency ids -> poe.ninja currency names
TRADE_CURRENCY = {
    "exalted": "Exalted Orb", "divine": "Divine Orb", "chaos": "Chaos Orb",
    "alch": "Orb of Alchemy", "annul": "Orb of Annulment", "regal": "Regal Orb",
    "vaal": "Vaal Orb", "mirror": "Mirror of Kalandra", "aug": "Orb of Augmentation",
    "transmute": "Orb of Transmutation", "chance": "Orb of Chance",
}
HEADER_KEYS = (
    "Rarity", "Unique ID", "Item Level", "Quality", "Sockets", "LevelReq",
    "Implicits", "Armour", "Evasion", "Energy Shield", "Ward", "Spirit",
    "Catalyst", "Radius", "Limited to", "Selected Variant", "Variant",
    "League", "Source", "Requires", "Has Alt Variant", "Prefix", "Suffix",
)
NUM_RE = re.compile(r"[+-]?\d+(?:\.\d+)?")
TAG_RE = re.compile(r"\{([^}]*)\}")


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------
@dataclass
class Mod:
    """A single item modifier line."""

    text: str
    kind: str  # implicit | explicit | enchant | rune | crafted
    affix: str = ""  # prefix | suffix (known from advanced copy / trade data)
    tier: str = ""   # e.g. "P6", "S2"


@dataclass
class Item:
    """An item parsed from Path of Building item text."""

    slot: str
    rarity: str
    name: str
    base: str
    mods: list[Mod] = field(default_factory=list)
    runes: list[str] = field(default_factory=list)
    corrupted: bool = False


@dataclass
class Gem:
    """A gem from the build's skill setup."""

    name: str
    level: int
    support: bool
    gem_id: str = ""
    swap: bool = False  # used only with the weapon-swap set


@dataclass
class PriceLine:
    """One priced row of the final report."""

    label: str
    detail: str
    value: float | None  # in poe.ninja primary currency
    source: str
    note: str = ""
    # Trade-site data for "go to cheapest seller" / refresh
    query: dict[str, Any] | None = None
    search_url: str = ""
    listings: list[dict[str, Any]] = field(default_factory=list)
    icon: str = ""  # item picture URL (poe.ninja or trade site)
    trend: dict[str, Any] | None = None  # 7-day {change, spark} from poe.ninja
    wanted: list[str] = field(default_factory=list)  # the item's mods used for the search
    swap: bool = False  # belongs to the weapon-swap set (not needed to play the build)
    low: bool = False   # price is based on very little market data


@dataclass
class Build:
    """Parsed build contents."""

    title: str
    items: list[Item]
    gems: list[Gem]


# --------------------------------------------------------------------------
# Loading a build from many sources
# --------------------------------------------------------------------------
def decode_pob_code(code: str) -> str:
    """Decode a PoB export code (url-safe base64 + zlib) into XML text.

    Raises:
        ValueError: If the code is not a valid PoB code.
    """
    cleaned = re.sub(r"\s+", "", code).replace("-", "+").replace("_", "/")
    cleaned += "=" * (-len(cleaned) % 4)
    try:
        raw = base64.b64decode(cleaned)
        return zlib.decompress(raw).decode("utf-8")
    except (ValueError, zlib.error) as exc:
        raise ValueError("ไม่ใช่ PoB code ที่ถูกต้อง") from exc


def raw_url_for(url: str) -> str:
    """Map a build-sharing URL to the URL that returns the raw PoB code."""
    if m := re.match(r"https?://pobb\.in/([\w-]+)", url):
        return f"https://pobb.in/{m.group(1)}/raw"
    if m := re.match(r"https?://pastebin\.com/(?:raw/)?(\w+)", url):
        return f"https://pastebin.com/raw/{m.group(1)}"
    if m := re.match(r"https?://poe\.ninja/", url):
        raise ValueError(
            "poe.ninja ไม่อนุญาตให้โปรแกรมภายนอกดึงข้อมูลบิว — "
            "เปิดหน้าบิวแล้วกดปุ่ม Path of Building เพื่อคัดลอกโค้ดมาวางแทน"
        )
    return url


async def load_build_xml(source: str, client: httpx.AsyncClient) -> str:
    """Return PoB XML from a code, URL, or local file (.xml or code text)."""
    path = Path(source)
    if len(source) < 4096 and path.exists():
        text = path.read_text(encoding="utf-8").strip()
        return text if text.startswith("<") else decode_pob_code(text)
    if source.startswith(("http://", "https://")):
        resp = await client.get(raw_url_for(source), follow_redirects=True)
        resp.raise_for_status()
        body = resp.text.strip()
        if body.startswith("<?xml") or body.startswith("<PathOfBuilding"):
            return body
        return decode_pob_code(body)
    return decode_pob_code(source)


# --------------------------------------------------------------------------
# Parsing PoB XML
# --------------------------------------------------------------------------
def _clean_mod(line: str) -> tuple[str, set[str]]:
    tags = {t.split(":")[0].lower() for t in TAG_RE.findall(line)}
    return TAG_RE.sub("", line).strip(), tags


def parse_item_text(text: str, slot: str) -> Item:
    """Parse PoB item text into an Item."""
    lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
    rarity = "NORMAL"
    if lines and lines[0].startswith("Rarity:"):
        rarity = lines.pop(0).split(":", 1)[1].strip().upper()
    name = base = ""
    if rarity in ("RARE", "UNIQUE") and len(lines) >= 2:
        name, base = lines[0], lines[1]
        lines = lines[2:]
    elif lines:
        name = base = lines.pop(0)

    item = Item(slot=slot, rarity=rarity, name=name, base=base)
    implicit_left = 0
    in_mods = False
    for ln in lines:
        key = ln.split(":", 1)[0]
        if ln.startswith("Implicits:"):
            implicit_left = int(NUM_RE.findall(ln)[0])
            in_mods = True
            continue
        if ln.startswith("Rune:"):
            item.runes.append(ln.split(":", 1)[1].strip())
            continue
        if ln == "Corrupted":
            item.corrupted = True
            continue
        if not in_mods and (key in HEADER_KEYS or key.startswith("Requires")):
            continue
        if not in_mods and ":" in ln and not NUM_RE.search(key):
            continue  # other "Key: value" header lines
        text_clean, tags = _clean_mod(ln)
        if not text_clean:
            continue
        if implicit_left > 0:
            kind = "implicit"
            implicit_left -= 1
        elif "enchant" in tags:
            kind = "enchant"
        elif "rune" in tags:
            kind = "rune"
        elif "crafted" in tags:
            kind = "crafted"
        else:
            kind = "explicit"
        item.mods.append(Mod(text_clean, kind))
    return item


GAME_SEP = re.compile(r"^-{4,}$")
GAME_SUFFIX = re.compile(r"\s*\((implicit|rune|enchant|crafted|fractured|desecrated|augmented)\)\s*$", re.I)
GAME_RANGE = re.compile(r"\(\s*[+-]?\d+(?:\.\d+)?\s*[-–—]\s*[+-]?\d+(?:\.\d+)?\s*\)")
ADV_HEADER = re.compile(r"^\{\s*(Prefix|Suffix)\s+Modifier(?:[^}]*?Tier:\s*(\d+))?[^}]*\}", re.I)
GAME_SKIP = {"corrupted", "unidentified", "mirrored", "split", "unmodifiable", "sanctified"}


def parse_game_item(text: str) -> tuple[Item, int]:
    """Parse item text copied in-game with Ctrl+C (or Ctrl+Alt+C).

    Returns the item and, for gems, its level (0 otherwise).

    Raises:
        ValueError: If the text does not look like copied item text.
    """
    lines = [ln.strip() for ln in text.replace("\r", "").split("\n")]
    sections: list[list[str]] = [[]]
    for ln in lines:
        if GAME_SEP.match(ln):
            sections.append([])
        elif ln:
            sections[-1].append(ln)
    sections = [s for s in sections if s]
    head = sections[0] if sections else []
    rarity_line = next((h for h in head if h.startswith("Rarity:")), None)
    if rarity_line is None:
        raise ValueError("ไม่ใช่ข้อความไอเทมจากเกม — ในเกมชี้ที่ไอเทมแล้วกด Ctrl+C แล้วนำมาวาง")
    item_class = next((h.split(":", 1)[1].strip() for h in head if h.startswith("Item Class:")), "")
    rarity = rarity_line.split(":", 1)[1].strip().upper()
    names = [h for h in head if not h.startswith(("Item Class:", "Rarity:"))]
    name = names[0] if names else ""
    base = names[1] if len(names) > 1 else name
    item = Item(slot=item_class or rarity.title(), rarity=rarity, name=name, base=base)

    gem_level = 0
    for sec in sections[1:]:
        affix = tier = ""
        for ln in sec:
            low = ln.lower()
            if ln.startswith("{"):  # Ctrl+Alt+C header for the mod lines below it
                hm = ADV_HEADER.match(ln)
                affix = hm.group(1).lower() if hm else ""
                tier = f"{affix[:1].upper()}{hm.group(2)}" if hm and hm.group(2) else ""
                continue
            if low in GAME_SKIP:
                item.corrupted = item.corrupted or low == "corrupted"
                continue
            if ln.startswith("{") or ln.startswith("("):  # advanced-copy headers / reminder text
                continue
            key = ln.split(":", 1)[0]
            if ":" in ln and not NUM_RE.search(key):
                if key == "Level" and rarity == "GEM":
                    gem_level = int((NUM_RE.findall(ln) or ["0"])[0])
                continue  # "Key: value" property lines (Quality, Armour, Requires…)
            m = GAME_SUFFIX.search(ln)
            tag = m.group(1).lower() if m else ""
            clean = GAME_RANGE.sub("", GAME_SUFFIX.sub("", ln)).strip()
            if not clean or not NUM_RE.search(clean) and tag == "":
                continue  # flavour / description text
            kind = {"implicit": "implicit", "rune": "rune", "enchant": "enchant"}.get(tag, "explicit")
            item.mods.append(Mod(clean, kind, affix if kind == "explicit" else "",
                                 tier if kind == "explicit" else ""))
    return item, gem_level


def skill_is_swap_only(attrib: dict[str, str]) -> bool:
    """True when a PoB skill group is only used with the weapon-swap set.

    Path of Building 2 saves ``set1`` / ``set2`` ("true"/"false") for the two weapon
    sets; a group with only set 2 belongs to the swap weapons. Older files may
    instead name the slot, e.g. ``slot="Weapon 1 Swap"``.
    """
    if "swap" in (attrib.get("slot") or "").lower():
        return True
    return attrib.get("set1") == "false" and attrib.get("set2") == "true"


def parse_build(xml_text: str) -> Build:
    """Extract equipped items, tree jewels and gems from PoB XML."""
    root = ET.fromstring(xml_text)
    build_el = root.find("Build")
    title = "Unknown build"
    if build_el is not None:
        title = (
            f"{build_el.get('ascendClassName') or build_el.get('className', '?')}"
            f" lvl {build_el.get('level', '?')}"
        )

    items: list[Item] = []
    items_el = root.find("Items")
    if items_el is not None:
        by_id = {el.get("id"): (el.text or "") for el in items_el.findall("Item")}
        active = items_el.get("activeItemSet", "1")
        sets = items_el.findall("ItemSet")
        chosen = next((s for s in sets if s.get("id") == active), sets[0] if sets else None)
        slots = chosen.findall("Slot") if chosen is not None else items_el.findall("Slot")
        seen: set[str] = set()
        for slot in slots:
            iid = slot.get("itemId", "0")
            if iid != "0" and iid in by_id and iid not in seen:
                seen.add(iid)
                items.append(parse_item_text(by_id[iid], slot.get("name", "?")))
        # Jewels socketed in the passive tree
        tree = root.find("Tree")
        if tree is not None:
            spec_id = tree.get("activeSpec", "1")
            specs = tree.findall("Spec")
            idx = int(spec_id) - 1 if spec_id.isdigit() else 0
            spec = specs[idx] if 0 <= idx < len(specs) else (specs[0] if specs else None)
            if spec is not None:
                for sock in spec.iter("Socket"):
                    iid = sock.get("itemId", "0")
                    if iid != "0" and iid in by_id and iid not in seen:
                        seen.add(iid)
                        items.append(parse_item_text(by_id[iid], "Tree Jewel"))

    gems: list[Gem] = []
    skills_el = root.find("Skills")
    if skills_el is not None:
        active = skills_el.get("activeSkillSet", "1")
        sets = skills_el.findall("SkillSet")
        chosen_s = next((s for s in sets if s.get("id") == active), sets[0] if sets else skills_el)
        for skill in chosen_s.findall("Skill"):
            if skill.get("enabled", "true") == "false":
                continue
            if skill.get("source"):  # granted by an item, not a real gem
                continue
            swap = skill_is_swap_only(skill.attrib)
            for g in skill.findall("Gem"):
                gid = g.get("gemId", "") or g.get("skillId", "")
                name = g.get("nameSpec") or g.get("skillId") or "?"
                gems.append(Gem(
                    name=name,
                    level=int(g.get("level", "1") or 1),
                    support="Support" in gid or "Support" in (g.get("skillId") or ""),
                    gem_id=gid,
                    swap=swap,
                ))
    return Build(title=title, items=items, gems=gems)


# --------------------------------------------------------------------------
# poe.ninja prices
# --------------------------------------------------------------------------
@dataclass
class NinjaPrices:
    """Prices from poe.ninja, keyed by lowercase name, in primary currency."""

    primary: str
    exchange: dict[str, float]
    uniques: dict[str, float]
    # Searchable catalog: one entry per priced name (name, category, value, icon)
    catalog: list[dict[str, Any]] = field(default_factory=list)

    def icon(self, *names: str) -> str:
        """Picture URL for the first of ``names`` found in the catalog."""
        idx = self.__dict__.get("_icons")
        if idx is None:
            idx = {str(c.get("name", "")).lower(): str(c.get("icon") or "") for c in self.catalog}
            self.__dict__["_icons"] = idx
        return next((idx[n.lower()] for n in names if idx.get(n.lower())), "")

    def listings(self, name: str) -> int | None:
        """How many listings poe.ninja saw for a unique (None if not a unique)."""
        idx = self.__dict__.get("_listings")
        if idx is None:
            idx = {str(c.get("name", "")).lower(): c["listings"] for c in self.catalog if "listings" in c}
            self.__dict__["_listings"] = idx
        return idx.get(name.lower())

    def trend(self, *names: str) -> dict[str, Any] | None:
        """7-day price trend ({change, spark}) for the first of ``names`` in the catalog."""
        idx = self.__dict__.get("_trends")
        if idx is None:
            idx = {str(c.get("name", "")).lower(): {"change": c.get("change"), "spark": c.get("spark")}
                   for c in self.catalog if c.get("spark")}
            self.__dict__["_trends"] = idx
        return next((idx[n.lower()] for n in names if n.lower() in idx), None)

    def currency(self, name: str) -> float | None:
        return self.exchange.get(name.lower())


def spark_of(raw: Any) -> dict[str, Any]:
    """poe.ninja sparkline -> {change: total % over 7 days, spark: [cumulative %, ...]}."""
    if not isinstance(raw, dict) or not raw.get("data"):
        return {}
    data = [round(float(v), 2) if isinstance(v, (int, float)) else None for v in raw["data"]]
    if not any(v is not None for v in data):
        return {}
    change = raw.get("totalChange")
    return {"change": round(float(change), 2) if isinstance(change, (int, float)) else None, "spark": data}


_EXCHANGE_IDS: set[str] = set()


async def trade_exchange_ids(client: httpx.AsyncClient) -> set[str]:
    """Ids usable on the trade site's bulk exchange (cached; empty set if unreachable)."""
    if _EXCHANGE_IDS:
        return _EXCHANGE_IDS
    try:
        r = await client.get(f"{TRADE}/data/static")
        r.raise_for_status()
        _EXCHANGE_IDS.update(str(e["id"]) for g in r.json().get("result", []) for e in g.get("entries", [])
                             if e.get("id"))
    except (httpx.HTTPError, ValueError, KeyError):
        pass
    return _EXCHANGE_IDS


def mark_exchangeable(catalog: list[dict[str, Any]], ids: set[str]) -> None:
    """Set ``xid`` on catalog entries the trade site's bulk exchange knows."""
    for c in catalog:
        if c.get("id") and c["id"] in ids:
            c["xid"] = c["id"]


async def fetch_ninja(client: httpx.AsyncClient, league: str) -> NinjaPrices:
    """Download all needed poe.ninja categories concurrently."""

    async def get(path: str, typ: str) -> dict[str, Any]:
        r = await client.get(f"{NINJA}/{path}", params={"league": league, "type": typ})
        if r.status_code != 200:
            return {}
        data: dict[str, Any] = r.json()
        return data

    ex_results = await asyncio.gather(
        *(get("exchange/current/overview", t) for t in EXCHANGE_TYPES)
    )
    un_results = await asyncio.gather(
        *(get("stash/current/item/overview", t) for t in UNIQUE_TYPES)
    )

    def icon(path: Any) -> str:
        p = str(path or "")
        return p if p.startswith("http") or not p else f"https://web.poecdn.com{p}"

    exchange: dict[str, float] = {}
    catalog: dict[str, dict[str, Any]] = {}
    primary = "?"
    for typ, data in zip(EXCHANGE_TYPES, ex_results):
        core = data.get("core", {})
        # item names live in the top-level "items" list; "core.items" only
        # holds the reference currencies
        meta = {str(i.get("id")): i for i in [*core.get("items", []), *data.get("items", [])]}
        prim_id = str(core.get("primary", ""))
        if prim_id in meta:
            primary = str(meta[prim_id].get("name", ""))
            exchange[primary.lower()] = 1.0
        for line in data.get("lines", []):
            m = meta.get(str(line.get("id")), {})
            nm = str(m.get("name", ""))
            val = line.get("primaryValue")
            if nm and isinstance(val, (int, float)):
                exchange[nm.lower()] = float(val)
                vol = line.get("volumePrimaryValue")
                catalog.setdefault(nm.lower(), {
                    "name": nm, "category": typ, "value": float(val), "icon": icon(m.get("image")),
                    "id": str(line.get("id") or ""),
                    # traded volume is given in primary currency; divide by price for a count
                    "volume": round(float(vol) / float(val)) if isinstance(vol, (int, float)) and val else None,
                    **spark_of(line.get("sparkline"))})

    uniques: dict[str, float] = {}
    for typ, data in zip(UNIQUE_TYPES, un_results):
        best: dict[str, tuple[int, float, dict[str, Any]]] = {}
        for line in data.get("lines", []):
            nm = str(line.get("name", "")).lower()
            val = line.get("primaryValue")
            if not nm or not isinstance(val, (int, float)):
                continue
            # prefer non-corrupted lines with most listings
            score = int(line.get("listingCount") or 0) - (10**6 if line.get("corrupted") else 0)
            if nm not in best or score > best[nm][0]:
                best[nm] = (score, float(val), line)
        for k, (_, v, line) in best.items():
            uniques[k] = v
            catalog.setdefault(k, {"name": line.get("name"), "base": line.get("baseType", ""),
                                   "category": typ, "value": v, "icon": icon(line.get("icon")),
                                   "listings": int(line.get("listingCount") or 0),
                                   **spark_of(line.get("sparkLine"))})
    return NinjaPrices(primary=primary, exchange=exchange, uniques=uniques,
                       catalog=list(catalog.values()))


def price_gem(gem: Gem, ninja: NinjaPrices) -> PriceLine:
    """Price a gem: lineage gems by name, others by uncut gem of same level."""
    by_name = ninja.currency(gem.name)
    if by_name is not None:
        return PriceLine("Gem", gem.name, by_name, "poe.ninja", query={"type": gem.name},
                         icon=ninja.icon(gem.name), trend=ninja.trend(gem.name))
    kind = "Support" if gem.support else "Skill"
    for nm, val in ninja.exchange.items():
        if f"uncut {kind.lower()} gem" in nm and re.search(rf"\b{gem.level}\b", nm):
            return PriceLine("Gem", f"{gem.name} (Uncut {kind} Lv{gem.level})", val, "poe.ninja",
                             icon=ninja.icon(nm), trend=ninja.trend(nm))
    return PriceLine("Gem", f"{gem.name} Lv{gem.level}", None, "-", "ไม่พบราคา")


# --------------------------------------------------------------------------
# Official trade site (rare / magic items)
# --------------------------------------------------------------------------
TRADE_SITE = "https://www.pathofexile.com/trade2/search/poe2"
SET_TAG = re.compile(r"<<set:\w+>>")


def trade_search_url(league: str, search_id: str) -> str:
    from urllib.parse import quote
    return f"{TRADE_SITE}/{quote(league)}/{search_id}"


def name_query(name: str, base: str, rarity: str) -> dict[str, Any]:
    """Trade query that finds an item by its name (uniques) or type (everything else)."""
    if rarity == "UNIQUE" and base and base != name:
        return {"name": name, "type": base}
    return {"type": name}


def listing_info(entry: dict[str, Any], ninja: NinjaPrices) -> dict[str, Any] | None:
    """Summarize one fetched trade listing (seller, price, whisper)."""
    listing = (entry or {}).get("listing") or {}
    price = listing.get("price") or {}
    if not isinstance(price.get("amount"), (int, float)):
        return None
    cur = str(price.get("currency", ""))
    rate = ninja.currency(TRADE_CURRENCY.get(cur, ""))
    acc = listing.get("account") or {}
    return {
        "seller": acc.get("lastCharacterName") or acc.get("name") or "?",
        "account": acc.get("name") or "",
        "amount": float(price["amount"]),
        "currency": cur,
        "value": float(price["amount"]) * rate if rate is not None else None,
        "whisper": SET_TAG.sub("", str(listing.get("whisper") or "")),
        "instant": bool(listing.get("hideout_token")),
        "indexed": listing.get("indexed") or "",
        "icon": str(((entry or {}).get("item") or {}).get("icon") or ""),
        "item": item_summary((entry or {}).get("item") or {}),
    }


LINK_TAG = re.compile(r"\[([^\]|]+)\|([^\]]+)\]")
PLAIN_TAG = re.compile(r"\[([^\]|]+)\]")
MOD_KEYS = (
    ("enchantMods", "enchant"), ("runeMods", "rune"), ("implicitMods", "implicit"),
    ("fracturedMods", "fractured"), ("explicitMods", "explicit"),
    ("desecratedMods", "desecrated"), ("craftedMods", "crafted"),
)


def clean_trade_text(text: Any) -> str:
    """Strip PoE2 markup like ``[EnergyShield|Energy Shield]`` -> ``Energy Shield``."""
    t = LINK_TAG.sub(r"\2", str(text or ""))
    return PLAIN_TAG.sub(r"\1", t).strip()


def item_summary(item: dict[str, Any]) -> dict[str, Any]:
    """Compact view of a trade-site item: name, base, item level, properties, mods."""
    props: list[str] = []
    for p in item.get("properties") or []:
        name = clean_trade_text(p.get("name"))
        vals = [clean_trade_text(v[0]) for v in p.get("values") or [] if v]
        if not name:
            continue
        if "{0}" in name:
            for i, v in enumerate(vals):
                name = name.replace(f"{{{i}}}", v)
            props.append(name)
        else:
            props.append(f"{name}: {', '.join(vals)}" if vals else name)
    def mod_info(m: Any, kind: str) -> dict[str, Any] | None:
        # mods are strings, or (PoE2) {"description": ..., "mods": [{"name", "tier": "P6", "magnitudes"}]}
        info: dict[str, Any] = {"kind": kind}
        if isinstance(m, dict):
            subs = [x for x in m.get("mods") or [] if str(x.get("tier") or "")[:1] in ("P", "S")]
            if subs:
                tiers = [str(x["tier"]) for x in subs]
                info["tier"] = "+".join(tiers)
                kinds = {t[:1] for t in tiers}
                info["affix"] = ("prefix" if kinds == {"P"} else "suffix" if kinds == {"S"} else "mixed")
                info["name"] = " + ".join(str(x.get("name")) for x in subs if x.get("name"))
                if len(subs) == 1:  # a line made of several affixes has no single roll range
                    ranges = [f"{g.get('min')}–{g.get('max')}" if g.get("min") != g.get("max") else str(g.get("min"))
                              for g in subs[0].get("magnitudes") or [] if g.get("min") is not None]
                    if ranges:
                        info["range"] = ", ".join(ranges)
            m = m.get("description") or m.get("text") or ""
        info["text"] = clean_trade_text(m)
        return info if info["text"] else None

    mods = [x for key, kind in MOD_KEYS for m in item.get(key) or [] if (x := mod_info(m, kind))]
    return {
        "name": clean_trade_text(item.get("name")),
        "base": clean_trade_text(item.get("typeLine") or item.get("baseType")),
        "rarity": str(item.get("rarity") or ""),
        "ilvl": item.get("ilvl"),
        "corrupted": bool(item.get("corrupted")),
        "props": props[:8],
        "mods": mods,
    }


_BASES: set[str] = set()
_BASES_LOCK = threading.Lock()


def resolve_base(name: str, base: str, bases: set[str]) -> str:
    """The item's real base type.

    Rare items carry it on their own line; magic items only have a name like
    "Hale Vaal Gloves of the Penguin", so we pick the longest known base type
    contained in it. Returns "" when nothing matches.
    """
    if base in bases:
        return base
    text = f"{name} {base}"
    found = [b for b in bases if b and re.search(rf"(?<!\w){re.escape(b)}(?!\w)", text)]
    return max(found, key=len) if found else ""


def mark_matches(listings: list[dict[str, Any]], wanted: list[str]) -> None:
    """Flag which of each listing's mods the searched item also has.

    Sets ``match`` on every explicit/implicit mod and ``matched``/``wanted`` counts on
    the listing, so the page can show "ตรง 3/4 mod" and tick the matching lines.
    """
    want = {normalize_stat(w) for w in wanted if w}
    if not want:
        return
    for x in listings:
        hit: set[str] = set()
        for m in (x.get("item") or {}).get("mods") or []:
            if m.get("kind") not in ("explicit", "implicit", "fractured", "desecrated", "crafted"):
                continue
            t = normalize_stat(m.get("text", ""))
            m["match"] = t in want
            if m["match"]:
                hit.add(t)
        x["matched"] = len(hit)
        x["wanted"] = len(want)


def normalize_stat(text: str) -> str:
    """Turn a mod line or trade stat text into a comparable template."""
    t = text.lower().replace("+#", "#")
    t = NUM_RE.sub("#", t).replace("+#", "#")
    t = re.sub(r"\(#-#\)", "#", t)
    return re.sub(r"\s+", " ", t).strip()


class RateLimiter:
    """Paces trade API calls from the limits the server reports.

    GGG sends, per endpoint policy, ``X-Rate-Limit-Rules`` (e.g. ``Ip``),
    ``X-Rate-Limit-Ip: max:period:penalty,...`` and
    ``X-Rate-Limit-Ip-State: current:period:restricted,...``. We keep our own
    request timestamps per policy and never go past ``max - margin`` in any
    window, so the long penalty (often 30 min) is never triggered.
    Shared by every TradeClient in the process (thread-safe).
    """

    def __init__(self, margin: int = 1) -> None:
        self.margin = margin
        self._lock = threading.Lock()
        self._rules: dict[str, list[tuple[int, float]]] = {}  # key -> [(max, period)]
        self._hits: dict[str, list[float]] = {}
        self.blocked_until = 0.0

    def known(self, key: str) -> bool:
        with self._lock:
            return key in self._rules

    def delay_for(self, key: str, now: float | None = None) -> float:
        """Seconds to wait so a request on ``key`` stays within limits (-1 = blocked)."""
        now = time.monotonic() if now is None else now
        with self._lock:
            if self.blocked_until > now:
                return -1.0
            hits = self._hits.get(key, [])
            wait = 0.0
            for mx, period in self._rules.get(key, []):
                window = sorted(t for t in hits if t > now - period)
                allowed = max(1, mx - self.margin)
                if len(window) >= allowed:
                    # wait until enough old hits fall out of the window
                    oldest = window[len(window) - allowed]
                    wait = max(wait, oldest + period - now + 0.25)
            return wait

    def record(self, key: str, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        with self._lock:
            hits = self._hits.setdefault(key, [])
            hits.append(now)
            longest = max((p for _, p in self._rules.get(key, [])), default=600.0)
            self._hits[key] = [t for t in hits if t > now - longest]

    def update(self, key: str, headers: Any, now: float | None = None) -> None:
        """Learn limits from response headers and sync with the server's counts."""
        now = time.monotonic() if now is None else now
        rules: list[tuple[int, float]] = []
        states: list[int] = []
        for rule in str(headers.get("X-Rate-Limit-Rules", "")).split(","):
            rule = rule.strip()
            if not rule:
                continue
            for part in str(headers.get(f"X-Rate-Limit-{rule}", "")).split(","):
                bits = part.split(":")
                if len(bits) >= 2 and bits[0].isdigit():
                    rules.append((int(bits[0]), float(bits[1])))
            for part in str(headers.get(f"X-Rate-Limit-{rule}-State", "")).split(","):
                bits = part.split(":")
                if len(bits) >= 2 and bits[0].isdigit():
                    states.append(int(bits[0]))
        if not rules:
            return
        with self._lock:
            self._rules[key] = rules
            hits = self._hits.setdefault(key, [])
            # hits the server counted that we did not make (other runs on this IP):
            # assume they happened just now, which is the cautious choice
            for (_, period), current in zip(rules, states):
                mine = sum(1 for t in hits if t > now - period)
                hits.extend([now] * max(0, current - mine))

    def block(self, seconds: float, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        with self._lock:
            self.blocked_until = max(self.blocked_until, now + seconds)


RATE = RateLimiter()


class SearchCache:
    """Remembers trade search results for a few minutes (thread-safe)."""

    def __init__(self, ttl: float = 300.0) -> None:
        self.ttl = ttl
        self._lock = threading.Lock()
        self._data: dict[str, tuple[float, Any]] = {}

    @staticmethod
    def key(league: str, query: dict[str, Any]) -> str:
        return league + "|" + json.dumps(query, sort_keys=True, ensure_ascii=False)

    def get(self, key: str, now: float | None = None) -> Any:
        now = time.monotonic() if now is None else now
        with self._lock:
            hit = self._data.get(key)
            if hit and now - hit[0] < self.ttl:
                return hit[1]
            self._data.pop(key, None)
            return None

    def put(self, key: str, value: Any, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        with self._lock:
            self._data[key] = (now, value)
            if len(self._data) > 500:
                for k in [k for k, (t, _) in self._data.items() if now - t >= self.ttl]:
                    del self._data[k]


SEARCH_CACHE = SearchCache()


def blocked_message(seconds: float) -> str:
    return f"เทรดไซต์จำกัดการค้นชั่วคราว — ลองใหม่ในอีกประมาณ {max(1, round(seconds / 60))} นาที"


class TradeClient:
    """Minimal, polite client for the PoE2 trade API."""

    def __init__(self, client: httpx.AsyncClient, league: str,
                 search_delay: float, fetch_delay: float) -> None:
        self.c = client
        self.league = league
        self.search_delay = search_delay
        self.fetch_delay = fetch_delay
        self._last = 0.0
        self.stats: dict[str, dict[str, str]] = {}  # kind -> template -> id

    async def _wait(self, delay: float) -> None:
        gap = time.monotonic() - self._last
        if gap < delay:
            await asyncio.sleep(delay - gap)
        self._last = time.monotonic()

    async def _pace(self, key: str, delay: float) -> None:
        """Wait as the shared limiter says (or ``delay`` until limits are known)."""
        if not RATE.known(key):
            await self._wait(delay)
        while True:
            wait = RATE.delay_for(key)
            if wait < 0:
                raise RuntimeError(blocked_message(RATE.blocked_until - time.monotonic()))
            if wait <= 0:
                break
            if wait > 5:
                print(f"  ⏳ รอโควตาเทรด {wait:.0f} วินาที", file=sys.stderr)
            await asyncio.sleep(wait)
        RATE.record(key)
        self._last = time.monotonic()

    async def _request(self, method: str, url: str, delay: float, **kw: Any) -> dict[str, Any]:
        key = "search" if "/search/" in url else "fetch" if "/fetch/" in url else "data"
        for _ in range(3):
            await self._pace(key, delay)
            r = await self.c.request(method, url, **kw)
            RATE.update(key, r.headers)
            if r.status_code == 429:
                wait = float(r.headers.get("Retry-After", "60"))
                RATE.block(wait)
                if wait > 90:
                    raise RuntimeError(blocked_message(wait))
                print(f"  ⏳ trade API rate limit — รอ {wait:.0f} วินาที", file=sys.stderr)
                await asyncio.sleep(wait)
                continue
            r.raise_for_status()
            data: dict[str, Any] = r.json()
            return data
        raise RuntimeError("trade API rate limited too many times")

    async def bases(self) -> set[str]:
        """All item base types from the trade site (cached for the process)."""
        with _BASES_LOCK:
            if _BASES:
                return _BASES
        data = await self._request("GET", f"{TRADE}/data/items", 1.0)
        found = {str(e["type"]) for g in data.get("result", []) for e in g.get("entries", [])
                 if e.get("type") and not e.get("name")}
        with _BASES_LOCK:
            _BASES.update(found)
            return _BASES

    async def load_stats(self) -> None:
        data = await self._request("GET", f"{TRADE}/data/stats", 1.0)
        for group in data.get("result", []):
            for e in group.get("entries", []):
                kind = str(e.get("type", group.get("id", "")))
                self.stats.setdefault(kind, {}).setdefault(normalize_stat(e["text"]), e["id"])

    def stat_filter(self, mod: Mod, ratio: float) -> dict[str, Any] | None:
        kinds = {"implicit": ["implicit"], "rune": ["rune", "augment"],
                 "enchant": ["enchant"]}.get(mod.kind, ["explicit"])
        tmpl = normalize_stat(mod.text)
        for k in kinds:
            sid = self.stats.get(k, {}).get(tmpl)
            if sid:
                nums = [abs(float(n)) for n in NUM_RE.findall(mod.text)]
                f: dict[str, Any] = {"id": sid, "disabled": False}
                if nums:
                    f["value"] = {"min": round(sum(nums) / len(nums) * ratio, 1)}
                return f
        return None

    async def search(self, query: dict[str, Any], ninja: NinjaPrices,
                     limit: int = 10, fresh: bool = False) -> tuple[str, list[dict[str, Any]]]:
        """Cheapest-first search, cached for a few minutes.

        ``fresh=True`` always asks the site (and refreshes the cache).
        """
        ckey = SearchCache.key(self.league, {**query, "_limit": limit})
        if not fresh and (hit := SEARCH_CACHE.get(ckey)) is not None:
            return hit  # type: ignore[no-any-return]
        result = await self._search(query, ninja, limit)
        SEARCH_CACHE.put(ckey, result)
        return result

    async def _search(self, query: dict[str, Any], ninja: NinjaPrices,
                      limit: int) -> tuple[str, list[dict[str, Any]]]:
        """Run an uncached cheapest-first search for online sellers.

        Returns the search id and up to ``limit`` summarized listings.
        """
        q = {**query, "status": {"option": "online"}}
        res = await self._request("POST", f"{TRADE}/search/poe2/{self.league}",
                                  self.search_delay, json={"query": q, "sort": {"price": "asc"}})
        ids: list[str] = res.get("result", [])[:limit]
        if not ids:
            return str(res.get("id", "")), []
        fetched = await self._request(
            "GET", f"{TRADE}/fetch/{','.join(ids)}", self.fetch_delay,
            params={"query": res.get("id", "")})
        out = [x for e in fetched.get("result", []) if (x := listing_info(e, ninja))]
        return str(res.get("id", "")), out

    async def price_item(self, item: Item, ratio: float, max_mods: int,
                         ninja: NinjaPrices) -> PriceLine:
        mods = [m for m in item.mods if m.kind in ("explicit", "implicit")]
        pairs = [(m, f) for m in mods if (f := self.stat_filter(m, ratio))][:max_mods]
        filters = [f for _, f in pairs]
        wanted = [m.text for m, _ in pairs]
        # same base type on every attempt: same mods on another kind of item is not the same item
        base = resolve_base(item.name, item.base, await self.bases())
        label = f"{item.slot}"
        detail = f"{item.rarity.title()} {base or item.base}".strip()
        if not filters:
            return PriceLine(label, detail, None, "-", "จับคู่ mod กับเทรดไม่ได้")

        common: dict[str, Any] = {"type_filters": {"filters": {"rarity": {"option": "nonunique"}}}}
        if not item.corrupted:  # corrupted copies sell cheaper and can't be modified
            common["misc_filters"] = {"filters": {"corrupted": {"option": "false"}}}
        steps = [("and", len(filters), "mod ตรงทั้งหมด"),
                 ("count", max(1, len(filters) - 1), "ผ่อน 1 mod"),
                 ("count", max(1, len(filters) // 2), "ผ่อนครึ่งหนึ่ง")]
        attempts: list[tuple[dict[str, Any], str]] = []
        for kind, need, note in steps:
            group: dict[str, Any] = {"type": kind, "filters": filters}
            if kind == "count":
                if need >= len(filters) or (attempts and need == attempts[-1][0]["stats"][0].get("value", {}).get("min")):
                    continue  # same as a previous attempt
                group["value"] = {"min": need}
            q: dict[str, Any] = {"stats": [group], "filters": common}
            if base:
                q["type"] = base
            attempts.append((q, note))

        for q, note in attempts:
            search_id, listings = await self.search(q, ninja)
            mark_matches(listings, wanted)
            prices = [x["value"] for x in listings if x["value"] is not None]
            if prices:
                note += f", {len(prices)} รายการ"
                if not base:
                    note += " · ไม่รู้ฐานไอเทม ผลอาจปนของชนิดอื่น"
                return PriceLine(label, detail, statistics.median(prices), "trade", note,
                                 query=q, search_url=trade_search_url(self.league, search_id),
                                 listings=listings[:5], wanted=wanted,
                                 icon=next((x["icon"] for x in listings if x.get("icon")), ""),
                                 low=len(prices) < LOW_TRADE_MATCHES)
        return PriceLine(label, detail, None, "trade", "ไม่พบของขายฐานเดียวกันที่ mod ใกล้เคียง",
                         query=attempts[-1][0], wanted=wanted)


# --------------------------------------------------------------------------
# Orchestration & output
# --------------------------------------------------------------------------
LOW_LISTINGS = 5      # fewer poe.ninja listings than this -> unreliable unique price
LOW_TRADE_MATCHES = 3  # fewer comparable trade listings than this -> unreliable rare price


def is_swap_slot(slot: str) -> bool:
    """PoB names the weapon-swap set "Weapon 1 Swap" / "Weapon 2 Swap"."""
    return "swap" in slot.lower()


def price_unique(item: Item, ninja: NinjaPrices) -> PriceLine:
    val = ninja.uniques.get(item.name.lower())
    return PriceLine(item.slot, item.name, val, "poe.ninja" if val is not None else "-",
                     "" if val is not None else "ไม่พบใน poe.ninja",
                     query=name_query(item.name, item.base, "UNIQUE"),
                     icon=ninja.icon(item.name), trend=ninja.trend(item.name),
                     low=val is not None and (ninja.listings(item.name) or 0) < LOW_LISTINGS)


def fmt(value: float | None, ninja: NinjaPrices) -> str:
    if value is None:
        return "?"
    ex, div = ninja.currency("Exalted Orb"), ninja.currency("Divine Orb")
    parts = []
    if div:
        parts.append(f"{value / div:.2f} div")
    if ex:
        parts.append(f"{value / ex:.0f} ex")
    return " / ".join(parts) or f"{value:.2f} {ninja.primary}"


async def run(args: argparse.Namespace) -> int:
    headers = {"User-Agent": os.environ.get("POE2_PRICER_UA", DEFAULT_UA)}
    cookies = {"POESESSID": s} if (s := os.environ.get("POESESSID")) else None
    async with httpx.AsyncClient(headers=headers, cookies=cookies, timeout=30) as client:
        xml_text = await load_build_xml(args.source, client)
        build = parse_build(xml_text)

        league = args.league
        if not league:
            r = await client.get(f"{NINJA}/leagues")
            league = r.json()[0]["id"]
        print(f"บิว: {build.title} | ลีก: {league}")
        ninja = await fetch_ninja(client, league)

        lines: list[PriceLine] = []
        trade: TradeClient | None = None
        for item in build.items:
            if item.rarity == "UNIQUE":
                lines.append(price_unique(item, ninja))
            elif item.rarity in ("RARE", "MAGIC") and not args.no_rares:
                if trade is None:
                    trade = TradeClient(client, league, args.search_delay, args.fetch_delay)
                    await trade.load_stats()
                print(f"  🔎 ค้นราคา {item.slot}: {item.name}", file=sys.stderr)
                lines.append(await trade.price_item(item, args.ratio, args.max_mods, ninja))
            else:
                lines.append(PriceLine(item.slot, f"{item.rarity.title()} {item.base}",
                                       None, "-", "ข้ามไป"))
            for rune in item.runes:
                val = ninja.currency(rune)
                lines.append(PriceLine("  └ Rune", rune, val, "poe.ninja"))
        for gem in build.gems:
            lines.append(price_gem(gem, ninja))

    total = sum(ln.value for ln in lines if ln.value is not None)
    missing = sum(1 for ln in lines if ln.value is None)
    print()
    w = max((len(ln.label) for ln in lines), default=10)
    for ln in sorted(lines, key=lambda x: -(x.value or 0)):
        note = f"  ({ln.note})" if ln.note else ""
        print(f"{ln.label:<{w}}  {ln.detail[:42]:<42}  {fmt(ln.value, ninja):>20}  [{ln.source}]{note}")
    print("-" * 90)
    print(f"รวมประมาณ: {fmt(total, ninja)}   (ไม่มีราคา {missing} รายการ)")

    if args.json:
        Path(args.json).write_text(json.dumps(
            {"build": build.title, "league": league, "total_primary": total,
             "primary_currency": ninja.primary,
             "lines": [ln.__dict__ for ln in lines]}, ensure_ascii=False, indent=2),
            encoding="utf-8")
        print(f"บันทึกผลไว้ที่ {args.json}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="เช็คราคาบิว POE2 จาก PoB code / pobb.in / pastebin / ไฟล์")
    p.add_argument("source", help="PoB code, ลิงก์ pobb.in / pastebin, หรือ path ไฟล์ .xml/.txt")
    p.add_argument("--league", help="league id (ค่าเริ่มต้น: ลีกชาเลนจ์ปัจจุบันจาก poe.ninja)")
    p.add_argument("--no-rares", action="store_true", help="ไม่ค้นราคาไอเทม rare (เร็วขึ้นมาก)")
    p.add_argument("--ratio", type=float, default=0.8,
                   help="ค้นของที่ค่า mod อย่างน้อยกี่เท่าของบิว (default 0.8)")
    p.add_argument("--max-mods", type=int, default=6, help="จำนวน mod สูงสุดที่ใช้ค้น")
    p.add_argument("--search-delay", type=float, default=6.0, help="วินาทีระหว่างการค้นแต่ละครั้ง")
    p.add_argument("--fetch-delay", type=float, default=1.5)
    p.add_argument("--json", help="บันทึกผลเป็นไฟล์ JSON")
    args = p.parse_args()
    try:
        return asyncio.run(run(args))
    except (ValueError, httpx.HTTPError, RuntimeError) as exc:
        print(f"ผิดพลาด: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
