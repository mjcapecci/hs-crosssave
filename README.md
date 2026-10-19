# hs-crosssave

Copies games from your HSReplay.net account into your local deck tracker, so
games played on one machine appear in the tracker on the other.

| Machine | Tracker | Where games are written |
|---|---|---|
| Windows | Hearthstone Deck Tracker | `%APPDATA%\HearthstoneDeckTracker\DeckStats.xml` / `DefaultDeckStats.xml` |
| macOS | HSTracker | `~/Library/Application Support/HSTracker/hstracker.realm` |

It uses the HSReplay.net login your tracker already has, so there's no API key or
password to set up. Make sure each tracker is logged in to the **same**
HSReplay.net account and is uploading replays.

## Setup

**Windows:** install Python 3.9+ from python.org. Nothing else is needed.

**macOS:** Python 3.9+ plus a one-time build of the small database helper (needs
Xcode Command Line Tools; the first build takes about a minute):

```sh
cd hstracker-helper && swift build -c release
```

## Use

1. Quit the tracker (HDT or HSTracker). Both keep stats in memory and would
   overwrite the synced games when they exit, so the script refuses to run
   while the tracker is open.
2. Preview: `python3 hs_crosssave.py --dry-run` (on Windows, `py hs_crosssave.py --dry-run`)
3. Sync: `python3 hs_crosssave.py`

It's safe to run as often as you like. Games already in the tracker are skipped
(matched by HSReplay id, or by start time and opponent class for games the
tracker recorded itself). Before every write, the files are backed up to
`backups/` next to the script.

If you see "HSReplay.net rejected the login token", open the tracker once so it
refreshes its HSReplay login, quit it, and run the script again.

## Where synced games show up

Each game is attached to the local deck whose card list matches exactly.

- **HDT:** games without a matching deck go to HDT's built-in "no deck" stats
  for that class (`DefaultDeckStats.xml`). "Show replay" opens the game on
  HSReplay.net.
- **HSTracker:** HSTracker only shows games that belong to a deck, so a deck
  named e.g. `Arena Warlock (synced)` is created for unmatched decks, including
  arena runs played on the other machine.

Only deck-based modes are synced: Ranked, Casual, Arena, Brawl, Friendly and
Practice. Battlegrounds, Mercenaries and Duels are skipped.

## Notes

- `hstracker-helper` is pinned to realm-swift 10.32.3, which bundles the same
  Realm Core (12.11.0) as HSTracker 3.7.0. A newer Realm could silently upgrade
  the database file to a format HSTracker can't read. If a future HSTracker
  update changes its Realm version, check before syncing.
- The script relies on things it doesn't control, any of which can change
  without notice:
  - HSReplay.net's `/api/v1/games/` endpoints, which aren't a documented
    public API. Changed field names or game type numbers are the most likely
    break.
  - Where and how each tracker stores its HSReplay login (`hsreplay_oauth` for
    HDT, the `net.hearthsim.hstracker` defaults for HSTracker).
  - HDT's XML file layout and HSTracker's database schema.

  After a Hearthstone, HSReplay.net or tracker update, run `--dry-run` first.
  Watch for a Python traceback (usually a `KeyError`), a sudden "Nothing to
  sync" when you know there are new games, or new game modes being skipped. A
  bad write can always be undone from `backups/`.
- Optional flags: `--hdt-dir` (if HDT keeps its data outside `%APPDATA%`),
  `--hstracker-realm`, `--hstracker-helper`.
