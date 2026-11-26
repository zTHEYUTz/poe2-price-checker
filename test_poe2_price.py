"""Offline tests (no network) for poe2_price."""

import base64
import zlib

import pytest

import poe2_price as pp

XML = """<?xml version="1.0" encoding="UTF-8"?>
<PathOfBuilding2>
  <Build level="92" className="Witch" ascendClassName="Infernalist"/>
  <Items activeItemSet="1">
    <Item id="1">Rarity: UNIQUE
Atziri's Disdain
Gold Circlet
Item Level: 82
Quality: 20
Implicits: 0
+40 to maximum Mana
</Item>
    <Item id="2">Rarity: RARE
Doom Grip
Vaal Gloves
Item Level: 81
Rune: Greater Rune of Leadership
Implicits: 1
{tags:life}+12 to maximum Life
{range:0.5}+85 to maximum Life
+32% to Fire Resistance
{crafted}+10 to Strength
</Item>
    <Item id="3">Rarity: RARE
Spider Eye
Sapphire
Implicits: 0
12% increased Spell Damage
</Item>
    <Item id="4">Rarity: RARE
Unused Thing
Iron Ring
Implicits: 0
+5 to Dexterity
</Item>
    <ItemSet id="1">
      <Slot name="Helmet" itemId="1"/>
      <Slot name="Gloves" itemId="2"/>
      <Slot name="Ring 1" itemId="0"/>
    </ItemSet>
  </Items>
  <Tree activeSpec="1">
    <Spec><Sockets><Socket nodeId="123" itemId="3"/></Sockets></Spec>
  </Tree>
  <Skills activeSkillSet="1">
    <SkillSet id="1">
      <Skill enabled="true">
        <Gem nameSpec="Fireball" level="20" gemId="Metadata/Items/Gems/SkillGemFireball"/>
        <Gem nameSpec="Rupture the Soul" level="1" gemId="Metadata/Items/Gems/SupportGemRuptureTheSoul"/>
      </Skill>
      <Skill enabled="true" source="Item:1">
        <Gem nameSpec="Granted Skill" level="10" gemId="x"/>
      </Skill>
    </SkillSet>
  </Skills>
</PathOfBuilding2>"""


def make_code(xml: str) -> str:
    raw = base64.b64encode(zlib.compress(xml.encode())).decode()
    return raw.replace("+", "-").replace("/", "_").rstrip("=")


def test_decode_roundtrip() -> None:
    assert pp.decode_pob_code(make_code(XML)) == XML


def test_decode_invalid() -> None:
    with pytest.raises(ValueError):
        pp.decode_pob_code("not a code!!")


def test_parse_build() -> None:
    b = pp.parse_build(XML)
    assert b.title == "Infernalist lvl 92"
    assert [i.slot for i in b.items] == ["Helmet", "Gloves", "Tree Jewel"]
    gloves = b.items[1]
    assert gloves.rarity == "RARE" and gloves.base == "Vaal Gloves"
    assert gloves.runes == ["Greater Rune of Leadership"]
    kinds = [(m.kind, m.text) for m in gloves.mods]
    assert kinds[0] == ("implicit", "+12 to maximum Life")
    assert ("explicit", "+85 to maximum Life") in kinds
    assert ("crafted", "+10 to Strength") in kinds
    assert [g.name for g in b.gems] == ["Fireball", "Rupture the Soul"]
    assert b.gems[1].support and not b.gems[0].support


@pytest.mark.parametrize("a,b", [
    ("+85 to maximum Life", "+# to maximum Life"),
    ("32% to Fire Resistance", "+#% to Fire Resistance"),
    ("Adds 5 to 10 Fire Damage", "Adds # to # Fire Damage"),
])
def test_normalize(a: str, b: str) -> None:
    assert pp.normalize_stat(a) == pp.normalize_stat(b)


def test_stat_filter() -> None:
    tc = pp.TradeClient.__new__(pp.TradeClient)
    tc.stats = {"explicit": {pp.normalize_stat("+# to maximum Life"): "explicit.stat_1"}}
    f = tc.stat_filter(pp.Mod("+100 to maximum Life", "explicit"), 0.8)
    assert f == {"id": "explicit.stat_1", "disabled": False, "value": {"min": 80.0}}
    assert tc.stat_filter(pp.Mod("Unknown mod 5", "explicit"), 0.8) is None


def test_pricing_helpers() -> None:
    ninja = pp.NinjaPrices(
        primary="Divine Orb",
        exchange={"divine orb": 1.0, "exalted orb": 0.01,
                  "uncut skill gem (level 20)": 0.5, "rupture the soul": 3.0},
        uniques={"atziri's disdain": 2.0},
    )
    b = pp.parse_build(XML)
    assert pp.price_unique(b.items[0], ninja).value == 2.0
    assert pp.price_gem(b.gems[0], ninja).value == 0.5
    assert pp.price_gem(b.gems[1], ninja).value == 3.0
    assert pp.fmt(2.0, ninja) == "2.00 div / 200 ex"
    assert pp.fmt(None, ninja) == "?"


def test_raw_url() -> None:
    assert pp.raw_url_for("https://pobb.in/abc-12") == "https://pobb.in/abc-12/raw"
    assert pp.raw_url_for("https://pastebin.com/XyZ1") == "https://pastebin.com/raw/XyZ1"
    with pytest.raises(ValueError):
        pp.raw_url_for("https://poe.ninja/poe2/builds/x/character/a/b")


GAME_RARE = """Item Class: Gloves
Rarity: Rare
Doom Grip
Vaal Gloves
--------
Quality: +20% (augmented)
Armour: 180 (augmented)
--------
Requires: Level 65, 100 Str
--------
Sockets: S
--------
Item Level: 81
--------
+12% increased Armour (rune)
--------
+15(10-20) to Strength (implicit)
--------
+85 to maximum Life
+32% to Fire Resistance
Adds 5 to 10 Physical Damage to Attacks
--------
Corrupted
"""

GAME_GEM = """Item Class: Skill Gems
Rarity: Gem
Fireball
--------
Level: 20
Quality: +10%
--------
Launches a fiery ball of 3 projectiles.
"""


def test_parse_game_item_rare() -> None:
    item, lvl = pp.parse_game_item(GAME_RARE)
    assert (item.rarity, item.name, item.base, item.slot) == ("RARE", "Doom Grip", "Vaal Gloves", "Gloves")
    assert item.corrupted and lvl == 0
    kinds = [(m.kind, m.text) for m in item.mods]
    assert ("rune", "+12% increased Armour") in kinds
    assert ("implicit", "+15 to Strength") in kinds
    assert ("explicit", "+85 to maximum Life") in kinds
    assert len([k for k, _ in kinds if k == "explicit"]) == 3


def test_parse_game_item_gem_and_invalid() -> None:
    item, lvl = pp.parse_game_item(GAME_GEM)
    assert (item.rarity, item.name, lvl) == ("GEM", "Fireball", 20)
    with pytest.raises(ValueError):
        pp.parse_game_item("hello world")


def test_rate_limiter_paces_from_headers() -> None:
    rl = pp.RateLimiter(margin=1)
    headers = {"X-Rate-Limit-Rules": "Ip", "X-Rate-Limit-Ip": "5:10:60,15:60:300",
               "X-Rate-Limit-Ip-State": "1:10:0,1:60:0"}
    rl.record("search", now=100.0)
    rl.update("search", headers, now=100.0)
    assert rl.known("search")
    for t in (101.0, 102.0, 103.0):
        assert rl.delay_for("search", now=t) == 0
        rl.record("search", now=t)
    # 4 hits in the 10 s window = max(5) - margin(1): must wait for the first to expire
    assert rl.delay_for("search", now=104.0) == pytest.approx(100.0 + 10 - 104.0 + 0.25)
    assert rl.delay_for("search", now=110.5) == 0


def test_rate_limiter_counts_unknown_hits_and_blocks() -> None:
    rl = pp.RateLimiter(margin=1)
    rl.update("fetch", {"X-Rate-Limit-Rules": "Ip", "X-Rate-Limit-Ip": "3:5:60",
                        "X-Rate-Limit-Ip-State": "2:5:0"}, now=50.0)
    assert rl.delay_for("fetch", now=50.0) > 0  # server already saw 2 of 3
    rl.block(1800, now=50.0)
    assert rl.delay_for("fetch", now=60.0) == -1
    assert rl.delay_for("other", now=1900.0) == 0


def test_search_cache_ttl() -> None:
    c = pp.SearchCache(ttl=300)
    k = c.key("L", {"type": "X"})
    assert k == c.key("L", {"type": "X"}) != c.key("M", {"type": "X"})
    c.put(k, ("id", []), now=0.0)
    assert c.get(k, now=299.0) == ("id", [])
    assert c.get(k, now=301.0) is None


def test_item_summary_cleans_trade_item() -> None:
    raw = {
        "name": "Doom Grip", "typeLine": "Vaal Gloves", "rarity": "Rare", "ilvl": 81, "corrupted": True,
        "properties": [{"name": "Quality", "values": [["+20%", 1]]},
                       {"name": "[Armour|Armour]", "values": [["180", 1]]},
                       {"name": "Stack Size {0}", "values": [["1/10", 0]]}],
        "implicitMods": ["+15 to [Strength|Strength]"],
        "runeMods": ["12% increased [Armour]",
                     {"description": "Bonded: +20 to maximum [Life|Life]", "domain": "rune", "hash": "x"}],
        "explicitMods": ["+85 to maximum [Life|Life]", "+32% to [Resistances|Fire Resistance]"],
    }
    s = pp.item_summary(raw)
    assert (s["name"], s["base"], s["ilvl"], s["corrupted"]) == ("Doom Grip", "Vaal Gloves", 81, True)
    assert s["props"] == ["Quality: +20%", "Armour: 180", "Stack Size 1/10"]
    assert s["mods"] == [
        {"text": "12% increased Armour", "kind": "rune"},
        {"text": "Bonded: +20 to maximum Life", "kind": "rune"},
        {"text": "+15 to Strength", "kind": "implicit"},
        {"text": "+85 to maximum Life", "kind": "explicit"},
        {"text": "+32% to Fire Resistance", "kind": "explicit"},
    ]
    assert pp.item_summary({})["mods"] == []
