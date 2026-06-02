"""Roster import from the Sportradar embed API.

Supports two league tenants:
  bv    — Basketball Victoria  (embed/2, sub=statistics)
  nbl1  — NBL1                 (embed/3, no sub param)

Fetching is synchronous and should be run via asyncio.to_thread to avoid
blocking the event loop.
"""
from __future__ import annotations

import gzip
import json
import urllib.request
import zlib
from typing import Any

LEAGUE_CONFIGS: dict[str, dict[str, str]] = {
    "bv": {
        "url": ("https://embed-api.eui.connect.sportradar.com/v1/embed/2/"
                "fixture_detail?sub=statistics&fixtureId={fid}"),
        "referer": "https://www.basketballvictoria.com.au/",
        "origin":  "https://www.basketballvictoria.com.au",
    },
    "nbl1": {
        "url": ("https://embed-api.eui.connect.sportradar.com/v1/embed/3/"
                "fixture_detail?fixtureId={fid}"),
        "referer": "https://www.nbl1.com.au/",
        "origin":  "https://www.nbl1.com.au",
    },
}


def fetch_fixture(fixture_id: str, league: str = "bv") -> dict[str, Any]:
    """Blocking fetch + decode of the Sportradar fixture detail.
    Call via asyncio.to_thread so the event loop isn't blocked."""
    cfg = LEAGUE_CONFIGS.get(league) or LEAGUE_CONFIGS["bv"]
    url = cfg["url"].format(fid=fixture_id)
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:151.0) Gecko/20100101 Firefox/151.0",
        "Accept": "*/*",
        "Accept-Language": "en-GB,en;q=0.9",
        "Accept-Encoding": "gzip, deflate",
        "Referer": cfg["referer"],
        "Origin":  cfg["origin"],
    })
    with urllib.request.urlopen(req, timeout=15) as resp:
        raw = resp.read()
        enc = resp.headers.get("Content-Encoding", "").lower()
    if enc == "gzip":
        raw = gzip.decompress(raw)
    elif enc == "deflate":
        raw = zlib.decompress(raw)
    return json.loads(raw)


def parse_fixture(data: dict[str, Any]) -> dict[str, Any]:
    """Extract team names + player numbers/names from the API blob.
    Points and fouls are deliberately ignored per operator spec."""
    fixture = data.get("data", {}).get("banner", {}).get("fixture", {}) or {}
    competitors = fixture.get("competitors", []) or []
    home_info = next((c for c in competitors if c.get("isHome")), None)
    away_info = next((c for c in competitors if c.get("isHome") is False), None)

    stats_base = (data.get("data", {})
                      .get("statistics", {})
                      .get("data", {})
                      .get("base", {})) or {}

    def _persons(side: str) -> list[dict]:
        block = stats_base.get(side, {}) or {}
        persons = block.get("persons") or []
        if not persons:
            return []
        rows = persons[0].get("rows", []) or []
        out = []
        for r in rows:
            number = str(r.get("bib") or "").strip()
            name = (r.get("personName") or "").strip()
            if not name and not number:
                continue
            out.append({"number": number, "name": name, "played": False})
        return out

    return {
        "home": {
            "name":    (home_info or {}).get("name") or "HOME",
            "players": _persons("home"),
        },
        "away": {
            "name":    (away_info or {}).get("name") or "AWAY",
            "players": _persons("away"),
        },
    }
