#!/usr/bin/env python3
"""
hs_crosssave.py - copy games from your HSReplay.net account into your local
deck tracker, so games played on one machine show up on the other.

  Windows: Hearthstone Deck Tracker (HDT)  -> DeckStats.xml / DefaultDeckStats.xml
  macOS:   HSTracker                       -> hstracker.realm (via hstracker-helper)

Authentication reuses the HSReplay.net login your tracker already has, so there
is nothing extra to configure. Close the tracker before running: both trackers
keep their stats in memory and would overwrite the changes when they exit.

Usage:
  python3 hs_crosssave.py              # sync
  python3 hs_crosssave.py --dry-run    # show what would be added, change nothing

Only standard-library modules are used. Requires Python 3.9+.
"""

import argparse
import collections
import datetime as dt
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET

API = "https://hsreplay.net/api/v1"
USER_AGENT = "hs-crosssave/1.0 (+personal sync script)"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# Games whose start times are this close are treated as the same game. Local
# games recorded by the tracker itself don't always carry the HSReplay id.
SAME_GAME_WINDOW_SECONDS = 120


class SyncError(Exception):
    pass


# --------------------------------------------------------------------------
# Hearthstone enums
# --------------------------------------------------------------------------

# BnetGameType (what HSReplay reports) -> (GameType name, GameMode name).
# Only deck-based modes are synced; Battlegrounds, Mercenaries and Duels are
# tracked separately by both trackers and are skipped.
BNET_GAME_TYPES = {
    1: ("GT_VS_FRIEND", "Friendly"),
    2: ("GT_RANKED", "Ranked"),
    3: ("GT_ARENA", "Arena"),
    4: ("GT_VS_AI", "Practice"),
    9: ("GT_CASUAL", "Casual"),
    10: ("GT_CASUAL", "Casual"),
    16: ("GT_TAVERNBRAWL", "Brawl"),
    17: ("GT_TB_1P_VS_AI", "Brawl"),
    18: ("GT_TB_2P_COOP", "Brawl"),
    30: ("GT_RANKED", "Ranked"),
    31: ("GT_CASUAL", "Casual"),
    40: ("GT_FSG_BRAWL_VS_FRIEND", "Brawl"),
    41: ("GT_FSG_BRAWL", "Brawl"),
    42: ("GT_FSG_BRAWL_1P_VS_AI", "Brawl"),
    43: ("GT_FSG_BRAWL_2P_COOP", "Brawl"),
    45: ("GT_RANKED", "Ranked"),
    58: ("GT_RANKED", "Ranked"),
    59: ("GT_CASUAL", "Casual"),
    63: ("GT_RANKED", "Ranked"),
    64: ("GT_CASUAL", "Casual"),
    68: ("GT_CASUAL", "Casual"),
    73: ("GT_UNDERGROUND_ARENA", "Arena"),
    76: ("GT_VS_AI", "Practice"),
}

# GameType name -> int value (HSTracker stores the int).
GAME_TYPE_VALUES = {
    "GT_VS_AI": 1, "GT_VS_FRIEND": 2, "GT_ARENA": 5, "GT_RANKED": 7,
    "GT_CASUAL": 8, "GT_TAVERNBRAWL": 16, "GT_TB_1P_VS_AI": 17,
    "GT_TB_2P_COOP": 18, "GT_FSG_BRAWL_VS_FRIEND": 19, "GT_FSG_BRAWL": 20,
    "GT_FSG_BRAWL_1P_VS_AI": 21, "GT_FSG_BRAWL_2P_COOP": 22,
    "GT_UNDERGROUND_ARENA": 42,
}

# GameMode name -> HSTracker int value.
HSTRACKER_GAME_MODES = {
    "Ranked": 1, "Casual": 2, "Arena": 3, "Brawl": 4, "Friendly": 5, "Practice": 6,
}

# HSReplay FormatType -> HDT Format name (HSTracker uses the lowercase form).
FORMATS = {1: "Wild", 2: "Standard", 3: "Classic", 4: "Twist"}

# HSReplay hero_class_name -> HDT hero name (HSTracker uses the lowercase form).
HERO_CLASSES = {
    "DEATHKNIGHT": "Deathknight", "DEMONHUNTER": "DemonHunter", "DRUID": "Druid",
    "HUNTER": "Hunter", "MAGE": "Mage", "PALADIN": "Paladin", "PRIEST": "Priest",
    "ROGUE": "Rogue", "SHAMAN": "Shaman", "WARLOCK": "Warlock", "WARRIOR": "Warrior",
}

# PlayState of the friendly player at game end.
PLAYSTATE_WON, PLAYSTATE_LOST, PLAYSTATE_TIED, PLAYSTATE_CONCEDED = 4, 5, 6, 8

# Region encoded in account_hi -> HDT Region name.
REGIONS = {1: "US", 2: "EU", 3: "ASIA", 5: "CHINA"}


# --------------------------------------------------------------------------
# HSReplay.net
# --------------------------------------------------------------------------

def api_get(url, token):
    req = urllib.request.Request(url, headers={
        "Authorization": "Bearer " + token,
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    })
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                body = e.read(300).decode("utf-8", "replace")
                if "Just a moment" in body:
                    raise SyncError("HSReplay.net's Cloudflare check blocked the request. "
                                    "Try again later or from another network.")
                raise SyncError("HSReplay.net rejected the login token (HTTP %d). Open your "
                                "deck tracker once so it refreshes its HSReplay login, close "
                                "it, then run this again." % e.code)
            if e.code == 429 or e.code >= 500:
                time.sleep(2 ** attempt * 2)
                continue
            raise SyncError("HSReplay.net returned HTTP %d for %s" % (e.code, url))
        except urllib.error.URLError as e:
            if attempt == 3:
                raise SyncError("Could not reach HSReplay.net: %s" % e.reason)
            time.sleep(2 ** attempt * 2)
    raise SyncError("HSReplay.net kept failing for %s" % url)


def fetch_game_list(token, username):
    url = API + "/games/?username=" + urllib.parse.quote(username)
    games = []
    while url:
        page = api_get(url, token)
        games.extend(page.get("results", []))
        url = page.get("next")
    return games


def fetch_game_detail(token, shortid):
    return api_get("%s/games/%s/" % (API, shortid), token)


def parse_time(s):
    # e.g. 2026-10-19T01:32:26.527791Z
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def describe_game(detail):
    """Turn an HSReplay game into a tracker-neutral dict, or None to skip it."""
    gg = detail["global_game"]
    mapping = BNET_GAME_TYPES.get(gg.get("game_type"))
    if mapping is None:
        return None
    game_type, game_mode = mapping
    me, opp = detail["friendly_player"], detail["opposing_player"]
    player_hero = HERO_CLASSES.get((me.get("hero_class_name") or "").upper())
    opponent_hero = HERO_CLASSES.get((opp.get("hero_class_name") or "").upper())
    if player_hero is None or opponent_hero is None:
        return None

    state = me.get("final_state")
    if state == PLAYSTATE_WON or (state is None and detail.get("won")):
        result = "Win"
    elif state == PLAYSTATE_TIED:
        result = "Draw"
    else:
        result = "Loss"

    deck = detail.get("friendly_deck") or {}
    cards = collections.Counter(deck.get("cards") or [])
    start = parse_time(gg["match_start"])
    end = parse_time(gg["match_end"]) if gg.get("match_end") else start
    region = REGIONS.get(((me.get("account_hi") or 0) >> 32) & 0xFF, "UNKNOWN")

    return {
        "shortid": detail["shortid"],
        "start": start,
        "end": end,
        "game_type": game_type,
        "game_mode": game_mode,
        "format": FORMATS.get(gg.get("format")) if game_mode in ("Ranked", "Casual") else None,
        "result": result,
        "conceded": PLAYSTATE_CONCEDED in (state, opp.get("final_state")),
        "coin": not me.get("is_first", False),
        # HSReplay counts turns for both players; trackers count your own turns.
        "turns": ((gg.get("num_turns") or 0) + 1) // 2,
        "player_name": me.get("name") or "",
        "opponent_name": opp.get("name") or "",
        "player_hero": player_hero,
        "opponent_hero": opponent_hero,
        "hero_id": me.get("hero_id") or "",
        "legend_rank": me.get("legend_rank") or 0,
        "opponent_legend_rank": opp.get("legend_rank") or 0,
        "build": detail.get("build") or gg.get("build"),
        "scenario_id": gg.get("scenario_id") or 0,
        "season": gg.get("ladder_season") or 0,
        "brawl_season": gg.get("brawl_season") or 0,
        "friendly_player_id": detail.get("friendly_player_id") or 0,
        "arena_wins": me.get("wins") or 0,
        "arena_losses": me.get("losses") or 0,
        "region": region,
        "cards": cards,
        # A full list (not just the cards revealed during the game) is needed
        # to match the game to a deck.
        "complete_deck": deck.get("shortid") is not None and sum(cards.values()) >= 30,
    }


def is_known(game, known_ids, known_times):
    if game["shortid"] in known_ids:
        return True
    ts = game["start"].timestamp()
    return any(abs(ts - t) <= SAME_GAME_WINDOW_SECONDS and opp == game["opponent_hero"].lower()
               for t, opp in known_times)


# --------------------------------------------------------------------------
# Windows: Hearthstone Deck Tracker
# --------------------------------------------------------------------------

def dpapi_decrypt(data):
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = DATA_BLOB(len(data), buf)
    blob_out = DATA_BLOB()
    if not ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


def pick(d, *keys):
    for k in keys:
        if isinstance(d, dict) and d.get(k) is not None:
            return d[k]
    return None


VERSION_PARTS = ("Build", "Major", "Minor", "Revision")


class HdtBackend:
    name = "Hearthstone Deck Tracker"

    def __init__(self, data_dir):
        self.dir = data_dir or os.path.join(os.environ.get("APPDATA", ""), "HearthstoneDeckTracker")
        if not os.path.isdir(self.dir):
            raise SyncError("HDT data folder not found: %s (use --hdt-dir)" % self.dir)
        self.deck_stats_path = os.path.join(self.dir, "DeckStats.xml")
        self.default_stats_path = os.path.join(self.dir, "DefaultDeckStats.xml")
        self.decks_path = os.path.join(self.dir, "PlayerDecks.xml")
        for prefix, uri in (("xsi", "http://www.w3.org/2001/XMLSchema-instance"),
                            ("xsd", "http://www.w3.org/2001/XMLSchema")):
            ET.register_namespace(prefix, uri)

    def tracker_running(self):
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq HearthstoneDeckTracker.exe"],
                             capture_output=True, text=True).stdout
        return "HearthstoneDeckTracker.exe" in out

    def credentials(self):
        path = os.path.join(self.dir, "hsreplay_oauth")
        if not os.path.exists(path):
            raise SyncError("HDT is not logged in to HSReplay.net (no %s). Log in from HDT's "
                            "HSReplay settings first." % path)
        with open(path, "rb") as f:
            data = json.loads(dpapi_decrypt(f.read()).decode("utf-8-sig"))
        token = pick(pick(data, "TokenData") or {}, "access_token", "AccessToken")
        username = pick(pick(data, "Account") or {}, "username", "Username")
        if not token or not username:
            raise SyncError("Could not read the HSReplay login from HDT. Log in again from "
                            "HDT's HSReplay settings.")
        return token, username

    @staticmethod
    def _load(path, root_tag):
        if os.path.exists(path):
            return ET.parse(path)
        return ET.ElementTree(ET.Element(root_tag))

    def load(self):
        self.deck_stats = self._load(self.deck_stats_path, "DeckStatsList")
        self.default_stats = self._load(self.default_stats_path, "DefaultDeckStats")

        known_ids, known_times = set(), []
        for tree in (self.deck_stats, self.default_stats):
            for g in tree.iter("Game"):
                for tag in ("UploadId", "ReplayUrl"):
                    v = g.findtext("HsReplay/" + tag)
                    if v:
                        known_ids.add(v.rstrip("/").rsplit("/", 1)[-1])
                start = g.findtext("StartTime")
                if start:
                    try:
                        ts = dt.datetime.fromisoformat(_trim_fraction(start)).timestamp()
                        known_times.append((ts, (g.findtext("OpponentHero") or "").lower()))
                    except ValueError:
                        pass
        self.known_ids, self.known_times = known_ids, known_times

        # Every version of every deck, as (deck id, version, card counts).
        self.deck_versions = []
        if os.path.exists(self.decks_path):
            for deck in ET.parse(self.decks_path).getroot().findall("Deck"):
                deck_id = deck.findtext("DeckId")
                for d in [deck] + deck.findall("DeckHistory/Deck"):
                    cards = collections.Counter()
                    for c in d.findall("Cards/Card"):
                        cards[c.findtext("Id")] += int(c.findtext("Count") or 1)
                    # HDT compares all four version parts, so keep them as written.
                    ver = d.find("Version")
                    version = [(tag, ver.findtext(tag)) for tag in VERSION_PARTS] \
                        if ver is not None else [("Major", "1"), ("Minor", "0")]
                    self.deck_versions.append((deck_id, version, cards, deck.findtext("Name")))

    def is_known(self, game):
        return is_known(game, self.known_ids, self.known_times)

    def place(self, game):
        if game["complete_deck"]:
            for deck_id, version, cards, name in self.deck_versions:
                if cards == game["cards"]:
                    return ("deck", deck_id, version, name)
        return ("default", None, None, "no deck (%s)" % game["player_hero"])

    def add(self, games):
        for path in (self.deck_stats_path, self.default_stats_path):
            backup(path)
        for game, placement in games:
            kind, deck_id, version, _ = placement
            if kind == "deck":
                stats = _find_or_add_deck_stats(
                    self.deck_stats.getroot(), "Deck", lambda ds: ds.findtext("DeckId") == deck_id,
                    deck_id=deck_id)
            else:
                stats = _find_or_add_deck_stats(
                    self.default_stats.getroot(), "DeckStats",
                    lambda ds: ds.findtext("Name") == game["player_hero"],
                    name=game["player_hero"])
            stats.find("Games").append(hdt_game_element(game, deck_id, version))
        for tree, path in ((self.deck_stats, self.deck_stats_path),
                           (self.default_stats, self.default_stats_path)):
            ET.indent(tree, space="  ")
            write_atomic(path, lambda f, t=tree: t.write(f, encoding="utf-8", xml_declaration=True))


def _trim_fraction(s):
    # .NET writes 7 fractional digits; older Pythons accept exactly 3 or 6.
    return re.sub(r"\.(\d+)", lambda m: "." + m.group(1)[:6].ljust(6, "0"), s, count=1)


def _find_or_add_deck_stats(root, item_tag, match, deck_id=None, name=None):
    container = root.find("DeckStats")
    if container is None:
        container = ET.SubElement(root, "DeckStats")
    for ds in container.findall(item_tag):
        if match(ds):
            if ds.find("Games") is None:
                ET.SubElement(ds, "Games")
            return ds
    ds = ET.SubElement(container, item_tag)
    ET.SubElement(ds, "DeckId").text = deck_id or "00000000-0000-0000-0000-000000000000"
    ET.SubElement(ds, "Games")
    if name:
        ET.SubElement(ds, "Name").text = name
    return ds


def hdt_game_element(game, deck_id, version):
    g = ET.Element("Game")

    def add(tag, value):
        ET.SubElement(g, tag).text = value

    add("GameId", str(uuid.uuid4()))
    add("PlayerHero", game["player_hero"])
    add("OpponentHero", game["opponent_hero"])
    add("Coin", "true" if game["coin"] else "false")
    add("GameMode", game["game_mode"])
    add("Result", game["result"])
    add("Turns", str(game["turns"]))
    add("StartTime", game["start"].astimezone().isoformat())
    add("EndTime", game["end"].astimezone().isoformat())
    add("PlayerName", game["player_name"])
    add("OpponentName", game["opponent_name"])
    add("WasConceded", "true" if game["conceded"] else "false")
    if game["legend_rank"]:
        add("LegendRank", str(game["legend_rank"]))
    if game["opponent_legend_rank"]:
        add("OpponentLegendRank", str(game["opponent_legend_rank"]))
    if game["build"]:
        add("HearthstoneBuild", str(game["build"]))
    add("GameType", game["game_type"])
    if game["season"] and game["game_mode"] == "Ranked":
        add("RankedSeasonId", str(game["season"]))
    if game["game_mode"] == "Arena":
        add("ArenaWins", str(game["arena_wins"]))
        add("ArenaLosses", str(game["arena_losses"]))
    add("Region", game["region"])
    if game["format"]:
        add("Format", game["format"])
    if deck_id:
        add("DeckId", deck_id)
        v = ET.SubElement(g, "PlayerDeckVersion")
        for tag, value in version:
            if value is not None:
                ET.SubElement(v, tag).text = value
    # With an upload id set, HDT's "Show replay" opens the HSReplay.net page
    # instead of looking for a local replay file.
    hs = ET.SubElement(g, "HsReplay")
    ET.SubElement(hs, "UploadId").text = game["shortid"]
    ET.SubElement(hs, "ReplayUrl").text = "https://hsreplay.net/replay/" + game["shortid"]
    return g


# --------------------------------------------------------------------------
# macOS: HSTracker
# --------------------------------------------------------------------------

HSTRACKER_DEFAULTS = "net.hearthsim.hstracker"


class HSTrackerBackend:
    name = "HSTracker"

    def __init__(self, realm_path, helper_path):
        self.realm = realm_path or os.path.expanduser(
            "~/Library/Application Support/HSTracker/hstracker.realm")
        if not os.path.exists(self.realm):
            raise SyncError("HSTracker database not found: %s" % self.realm)
        self.helper = helper_path or os.path.join(
            SCRIPT_DIR, "hstracker-helper", ".build", "release", "hstracker-helper")
        if not os.path.exists(self.helper):
            raise SyncError("hstracker-helper is not built. Run:\n  cd \"%s\" && swift build -c release"
                            % os.path.join(SCRIPT_DIR, "hstracker-helper"))

    def tracker_running(self):
        return subprocess.run(["pgrep", "-x", "HSTracker"], capture_output=True).returncode == 0

    def credentials(self):
        def read(key):
            r = subprocess.run(["defaults", "read", HSTRACKER_DEFAULTS, key],
                               capture_output=True, text=True)
            return r.stdout.strip() if r.returncode == 0 else None

        token, username = read("hsreplay_oauth_token"), read("hsreplay_username")
        if not token or not username:
            raise SyncError("HSTracker is not logged in to HSReplay.net. Log in under "
                            "HSTracker > Preferences > HSReplay first.")
        return token, username

    def _helper(self, *args):
        r = subprocess.run([self.helper] + list(args), capture_output=True, text=True)
        if r.returncode != 0:
            raise SyncError("hstracker-helper failed: " + r.stderr.strip())
        return r.stdout

    def load(self):
        data = json.loads(self._helper("export", self.realm))
        self.known_ids = {g["hsReplayId"] for g in data["games"] if g["hsReplayId"]}
        self.known_times = [(g["startTime"], g["opponentHero"]) for g in data["games"]]
        self.decks = [dict(d, cards=collections.Counter(d["cards"])) for d in data["decks"]]
        self.new_decks = []

    def is_known(self, game):
        return is_known(game, self.known_ids, self.known_times)

    def place(self, game):
        player_class = game["player_hero"].lower()
        arena = game["game_mode"] == "Arena"
        if game["complete_deck"]:
            for d in self.decks:
                if d["playerClass"] == player_class and d["cards"] == game["cards"] \
                        and d["isArena"] == arena:
                    return ("deck", d["deckId"], None, d["name"])
            name = "%s %s (synced)" % ("Arena" if arena else "", game["player_hero"])
        else:
            # Without the full list, park the game in a per-class placeholder deck.
            name = "Unknown %s deck (synced)" % game["player_hero"]
            for d in self.decks:
                if d["name"] == name:
                    return ("deck", d["deckId"], None, d["name"])
        deck = {
            "deckId": str(uuid.uuid4()).upper(),
            "name": name.strip(),
            "playerClass": player_class,
            "heroId": game["hero_id"],
            "isArena": arena,
            "cards": game["cards"] if game["complete_deck"] else collections.Counter(),
        }
        self.decks.append(deck)
        self.new_decks.append(deck)
        return ("new deck", deck["deckId"], None, deck["name"])

    def add(self, games):
        payload = {
            "newDecks": [dict(d, cards=dict(d["cards"])) for d in self.new_decks],
            "games": [hstracker_game(game, placement[1]) for game, placement in games],
        }
        backup(self.realm)
        fd, path = tempfile.mkstemp(suffix=".json")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(payload, f)
            print(self._helper("import", self.realm, path).strip())
        finally:
            os.remove(path)


def hstracker_game(game, deck_id):
    return {
        "deckId": deck_id,
        "hsReplayId": game["shortid"],
        "playerHero": game["player_hero"].lower(),
        "opponentHero": game["opponent_hero"].lower(),
        "coin": game["coin"],
        "gameMode": HSTRACKER_GAME_MODES[game["game_mode"]],
        "gameType": GAME_TYPE_VALUES[game["game_type"]],
        "result": {"Win": 1, "Loss": 2, "Draw": 3}[game["result"]],
        "turns": game["turns"],
        "startTime": game["start"].timestamp(),
        "endTime": game["end"].timestamp(),
        "playerName": game["player_name"],
        "opponentName": game["opponent_name"],
        "wasConceded": game["conceded"],
        "legendRank": game["legend_rank"],
        "opponentLegendRank": game["opponent_legend_rank"],
        "friendlyPlayerId": game["friendly_player_id"],
        "scenarioId": game["scenario_id"],
        "season": game["season"],
        "rankedSeasonId": game["season"] if game["game_mode"] == "Ranked" else 0,
        "brawlSeasonId": game["brawl_season"],
        "arenaWins": game["arena_wins"] if game["game_mode"] == "Arena" else 0,
        "arenaLosses": game["arena_losses"] if game["game_mode"] == "Arena" else 0,
        "hearthstoneBuild": game["build"],
        "format": game["format"].lower() if game["format"] else None,
    }


# --------------------------------------------------------------------------
# Shared helpers
# --------------------------------------------------------------------------

def backup(path):
    if not os.path.exists(path):
        return
    folder = os.path.join(SCRIPT_DIR, "backups")
    os.makedirs(folder, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = os.path.join(folder, "%s.%s" % (os.path.basename(path), stamp))
    shutil.copy2(path, dest)
    print("Backed up %s -> %s" % (os.path.basename(path), dest))


def write_atomic(path, writer):
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        writer(f)
    os.replace(tmp, path)


def main():
    ap = argparse.ArgumentParser(description="Copy HSReplay.net games into your local deck tracker.")
    ap.add_argument("--dry-run", action="store_true", help="show what would be added, change nothing")
    ap.add_argument("--hdt-dir", help="HDT data folder (default: %%APPDATA%%\\HearthstoneDeckTracker)")
    ap.add_argument("--hstracker-realm", help="path to hstracker.realm (default: HSTracker's own)")
    ap.add_argument("--hstracker-helper", help="path to the built hstracker-helper binary")
    args = ap.parse_args()

    system = platform.system()
    if system == "Windows":
        backend = HdtBackend(args.hdt_dir)
    elif system == "Darwin":
        backend = HSTrackerBackend(args.hstracker_realm, args.hstracker_helper)
    else:
        raise SyncError("Only Windows (HDT) and macOS (HSTracker) are supported.")

    if not args.dry_run and backend.tracker_running():
        raise SyncError("%s is running. Close it first so it doesn't overwrite the synced "
                        "games when it exits." % backend.name)

    token, username = backend.credentials()
    print("Fetching game list for %s from HSReplay.net..." % username)
    listed = fetch_game_list(token, username)
    print("HSReplay.net returned %d game(s)." % len(listed))

    backend.load()
    to_add = []
    for item in sorted(listed, key=lambda x: x["global_game"]["match_start"]):
        if item["shortid"] in backend.known_ids:
            continue
        if BNET_GAME_TYPES.get(item["global_game"].get("game_type")) is None:
            continue
        game = describe_game(fetch_game_detail(token, item["shortid"]))
        if game is None or backend.is_known(game):
            continue
        to_add.append((game, backend.place(game)))

    if not to_add:
        print("Nothing to sync: %s already has every game." % backend.name)
        return
    for game, (kind, _, _, deck_name) in to_add:
        print("  %s  %-8s %-4s %s vs %s  -> %s%s" % (
            game["start"].astimezone().strftime("%Y-%m-%d %H:%M"), game["game_mode"],
            game["result"], game["player_hero"], game["opponent_hero"], deck_name,
            " (new)" if kind == "new deck" else ""))
    if args.dry_run:
        print("Dry run: %d game(s) would be added." % len(to_add))
        return
    backend.add(to_add)
    print("Added %d game(s) to %s." % (len(to_add), backend.name))


if __name__ == "__main__":
    try:
        main()
    except SyncError as e:
        print("error: %s" % e, file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)
