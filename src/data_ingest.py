import json
import requests
from pathlib import Path

CACHE_DIR = Path(__file__).parent.parent / "data" / "cache"
_CARDS_URL = (
    "https://raw.githubusercontent.com/flibustier/"
    "pokemon-tcg-pocket-database/main/dist/cards.json"
)
_EXTRAS_URL = (
    "https://raw.githubusercontent.com/flibustier/"
    "pokemon-tcg-pocket-database/main/dist/cards.extra.json"
)
_MOCK_PATH = Path(__file__).parent.parent / "data" / "mock" / "tournament.json"
_BASE_API = "https://play.limitlesstcg.com/api"
_TOURNAMENT_CACHE = CACHE_DIR / "tournament.json"
# Map API set codes to catalog set codes where they differ
_SET_CODE_MAP = {
    "P-A": "PROMO-A",
    "P-B": "PROMO-B",
}


def fetch_card_catalog(force_refresh: bool = False) -> list[dict]:
    cache_path = CACHE_DIR / "cards.json"
    if cache_path.exists() and not force_refresh:
        return load_card_catalog()
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cards_resp = requests.get(_CARDS_URL, timeout=30)
    cards_resp.raise_for_status()
    cards: list[dict] = cards_resp.json()

    extras_resp = requests.get(_EXTRAS_URL, timeout=30)
    extras_resp.raise_for_status()
    extras: list[dict] = extras_resp.json()
    # extras are keyed by (set, number) — no pre-existing "id" field
    extras_by_key = {(e["set"], e["number"]): e for e in extras}
    for card in cards:
        extra = extras_by_key.get((card["set"], card["number"]), {})
        card.update(extra)
        # synthesise a stable id from set + zero-padded number
        card["id"] = f"{card['set']}-{str(card['number']).zfill(3)}"
    with open(cache_path, "w") as f:
        json.dump(cards, f, indent=2)
    return cards


def load_card_catalog() -> list[dict]:
    cache_path = CACHE_DIR / "cards.json"
    if not cache_path.exists():
        raise FileNotFoundError("Card catalog not cached. Call fetch_card_catalog() first.")
    with open(cache_path) as f:
        return json.load(f)


def _aggregate_decklist(decklists: list[dict]) -> list[dict]:
    """Build a representative card list from multiple player decklists.

    Takes the most common cards (appearing in >=50% of decklists) and
    uses the rounded average copy count.
    """
    if not decklists:
        return []
    n = len(decklists)
    card_data: dict[str, dict] = {}
    for dl in decklists:
        for category in ("pokemon", "trainer"):
            for entry in dl.get(category, []):
                set_id = str(entry.get("set", ""))
                set_id = _SET_CODE_MAP.get(set_id, set_id)
                number = str(entry.get("number", "")).zfill(3)
                card_id = f"{set_id}-{number}"
                count = int(entry.get("count", 1))
                if card_id not in card_data:
                    card_data[card_id] = {"total": 0, "appearances": 0, "name": entry.get("name", "")}
                card_data[card_id]["total"] += count
                card_data[card_id]["appearances"] += 1
    result = []
    for card_id, data in card_data.items():
        if data["appearances"] >= max(1, n // 2):
            avg = round(data["total"] / data["appearances"])
            if avg > 0:
                result.append({"id": card_id, "count": avg, "name": data.get("name", "")})
    # Sort by count descending; cap total copy count at 20
    sorted_result = sorted(result, key=lambda x: x["count"], reverse=True)
    capped, total = [], 0
    for entry in sorted_result:
        if total + entry["count"] > 20:
            remaining = 20 - total
            if remaining > 0:
                capped.append({"id": entry["id"], "count": remaining})
                total = 20
            break
        capped.append(entry)
        total += entry["count"]
        if total >= 20:
            break
    return capped


def fetch_tournament_data(
    game: str = "POCKET",
    num_tournaments: int = 10,
    min_players: int = 32,
    force_refresh: bool = False,
) -> dict:
    """Fetch and aggregate tournament data from the Limitless TCG API.

    Pulls recent tournaments, aggregates archetype meta shares and win rates,
    builds a matchup matrix from pairings, and caches the result.
    """
    if _TOURNAMENT_CACHE.exists() and not force_refresh:
        with open(_TOURNAMENT_CACHE) as f:
            return json.load(f)

    # Step 1: get recent tournaments
    resp = requests.get(
        f"{_BASE_API}/tournaments",
        params={"game": game, "limit": 50},
        timeout=30,
    )
    resp.raise_for_status()
    tournaments = [t for t in resp.json() if t.get("players", 0) >= min_players]
    tournaments = tournaments[:num_tournaments]

    if not tournaments:
        raise ValueError(f"No {game} tournaments found with >= {min_players} players.")

    # Step 2: fetch standings + pairings per tournament
    player_decks: dict[str, dict[str, str]] = {}   # {tid: {player_id: deck_id}}
    deck_stats: dict[str, dict] = {}                # {deck_id: {...}}
    total_appearances = 0
    all_pairings: dict[str, list[dict]] = {}

    for t in tournaments:
        tid = t["id"]
        s = requests.get(f"{_BASE_API}/tournaments/{tid}/standings", timeout=30)
        s.raise_for_status()
        standings = s.json()

        p = requests.get(f"{_BASE_API}/tournaments/{tid}/pairings", timeout=30)
        p.raise_for_status()
        all_pairings[tid] = p.json()

        player_decks[tid] = {}
        for entry in standings:
            deck_info = entry.get("deck")
            if not deck_info:
                continue
            deck_id = deck_info["id"]
            player_id = entry.get("player", "")
            record = entry.get("record") or {}
            wins = int(record.get("wins", 0))
            losses = int(record.get("losses", 0))

            player_decks[tid][player_id] = deck_id
            if deck_id not in deck_stats:
                deck_stats[deck_id] = {
                    "name": deck_info["name"],
                    "wins": 0, "losses": 0,
                    "appearances": 0,
                    "decklists": [],
                }
            deck_stats[deck_id]["wins"] += wins
            deck_stats[deck_id]["losses"] += losses
            deck_stats[deck_id]["appearances"] += 1
            total_appearances += 1

            dl = entry.get("decklist")
            if dl and len(deck_stats[deck_id]["decklists"]) < 5:
                deck_stats[deck_id]["decklists"].append(dl)

    if total_appearances == 0:
        raise ValueError("No deck data found in fetched tournaments.")

    # Step 3: build matchup win/loss tallies
    matchup_results: dict[str, dict[str, dict]] = {}
    for tid, pairings in all_pairings.items():
        pdeck = player_decks.get(tid, {})
        for match in pairings:
            p1 = match.get("player1", "")
            p2 = match.get("player2", "")
            winner = match.get("winner")
            if not p1 or not p2 or winner in (-1, 0):
                continue
            d1 = pdeck.get(p1)
            d2 = pdeck.get(p2)
            if not d1 or not d2:
                continue
            for dk_a, dk_b, won in [(d1, d2, winner == p1), (d2, d1, winner == p2)]:
                matchup_results.setdefault(dk_a, {}).setdefault(
                    dk_b, {"wins": 0, "total": 0}
                )
                matchup_results[dk_a][dk_b]["wins"] += int(won)
                matchup_results[dk_a][dk_b]["total"] += 1

    # Step 4: select top archetypes by player count
    sorted_decks = sorted(
        deck_stats.items(), key=lambda x: x[1]["appearances"], reverse=True
    )
    top_n = min(15, len(sorted_decks))
    top_archetypes_raw = sorted_decks[:top_n]
    top_appearances = sum(v["appearances"] for _, v in top_archetypes_raw)

    archetypes = []
    for deck_id, stats in top_archetypes_raw:
        total_games = stats["wins"] + stats["losses"]
        win_rate = round(stats["wins"] / total_games, 4) if total_games > 0 else 0.5
        archetypes.append({
            "id": deck_id,
            "name": stats["name"],
            "meta_share": round(stats["appearances"] / top_appearances, 4),
            "win_rate": win_rate,
            "cards": _aggregate_decklist(stats["decklists"]),
        })

    # Step 5: matchup matrix (default 0.5 when insufficient data)
    matchup_matrix: dict[str, dict[str, float]] = {}
    for arch_a in archetypes:
        aid = arch_a["id"]
        matchup_matrix[aid] = {}
        for arch_b in archetypes:
            bid = arch_b["id"]
            if aid == bid:
                matchup_matrix[aid][bid] = 0.5
            else:
                data = matchup_results.get(aid, {}).get(bid, {})
                total = data.get("total", 0)
                wins = data.get("wins", 0)
                matchup_matrix[aid][bid] = round(wins / total, 4) if total >= 3 else 0.5

    result = {"archetypes": archetypes, "matchup_matrix": matchup_matrix}
    _TOURNAMENT_CACHE.parent.mkdir(parents=True, exist_ok=True)
    with open(_TOURNAMENT_CACHE, "w") as f:
        json.dump(result, f, indent=2)
    return result


def load_tournament_data() -> dict:
    """Load tournament data from cache (live data) or mock file as fallback."""
    if _TOURNAMENT_CACHE.exists():
        with open(_TOURNAMENT_CACHE) as f:
            return json.load(f)
    if not _MOCK_PATH.exists():
        raise FileNotFoundError(
            f"No tournament data found. Run fetch_tournament_data() first, "
            f"or create {_MOCK_PATH}."
        )
    with open(_MOCK_PATH) as f:
        return json.load(f)
