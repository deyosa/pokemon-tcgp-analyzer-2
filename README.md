# Pokémon TCG Pocket — Meta Analyzer & Collection Manager

A data-driven web tool that helps Pokémon TCG Pocket players understand the competitive meta, evaluate their card collection, and make smarter decisions about which decks to build.

Live at **[pocket-meta.online](https://pocket-meta.online)**

---

## What It Does

- **Fetches live tournament data** from the Limitless TCG API and the community card catalog from GitHub
- **Analyzes your collection** against every top meta deck — shows completion percentage and exactly which cards you're missing
- **Estimates expected win rate (E[WR])** for any deck against the current meta field using a matchup matrix and meta-share weighting
- **Classifies cards by role** (win condition, engine, staple, tech, garnet) and runs a regression to show which roles drive win rates
- **Generates charts**: matchup heatmap, observed vs. predicted win rates, role contribution breakdown
- **Launches a browser UI** where you can manage your collection, build custom decks, explore archetypes, and get deck vs. deck analysis
- **Runs as a terminal CLI** — full feature parity with the browser UI, no browser required
- **Custom deck analysis** — build your own deck and get similarity-based matchup estimates using cosine similarity against known meta archetypes

---

## Quickstart

### Browser UI (Mac / Linux)
```bash
bash setup.sh
python3 main.py
```

### Browser UI (Windows)
```bat
setup.bat
python main.py
```

### CLI (terminal-only, no browser needed)
```bash
python3 main_cli.py          # interactive menu
python3 main_cli.py meta     # browse top archetypes directly
python3 main_cli.py analysis # deck vs. deck analysis
```

The setup script creates a virtual environment and installs all dependencies. On first run, both `main.py` and `main_cli.py` download and cache the card catalog and tournament data automatically — no manual setup needed.

---

## Requirements

- Python 3.10+
- Internet connection on first run (data is cached locally after that)

Python dependencies (auto-installed by setup scripts):

| Package | Purpose |
|---|---|
| `requests` | API and data fetching |
| `numpy` | Numerical computation |
| `scikit-learn` | Role regression model |
| `matplotlib` | Chart generation |
| `tabulate` | Terminal summary table |
| `flask` | HTTP server for browser UI |
| `gunicorn` | Production WSGI server |
| `pytest` | Test suite |
| `flake8` | Code style enforcement |

---

## Project Structure

```
.
├── main.py                   # Browser UI entry point — runs full pipeline then serves Flask app
├── main_cli.py               # CLI entry point — terminal-only, no browser or HTTP server
├── wsgi.py                   # Gunicorn entry point for production deployment
├── requirements.txt          # Python dependency list
├── setup.sh                  # Mac/Linux setup — creates venv and installs dependencies
├── setup.bat                 # Windows setup — same as setup.sh for Windows
│
├── src/
│   ├── data_ingest.py        # Fetches and caches card catalog + tournament data
│   ├── models.py             # Core dataclasses: Card, Deck, Collection
│   ├── matchup.py            # Expected win rate calculation using matchup matrix
│   ├── card_roles.py         # Role classification + linear regression model
│   ├── visualizations.py     # Matplotlib charts + terminal summary table
│   ├── web_collection.py     # Browser UI — Flask app, full HTML/CSS/JS single-file page
│   └── cli/
│       ├── state.py          # AppState dataclass shared across all CLI commands
│       ├── display.py        # Terminal output helpers (tables, menus, prompts)
│       ├── collection_io.py  # CSV import, random collection generator, fuzzy card search
│       ├── commands.py       # Meta, Collection, Catalog, Analysis CLI commands
│       └── menu.py           # Interactive numbered menu loop
│
├── tests/
│   ├── conftest.py           # Shared pytest fixtures
│   ├── test_models.py        # Tests for Card, Deck, Collection dataclasses
│   ├── test_matchup.py       # Tests for expected win rate calculation
│   ├── test_card_roles.py    # Tests for role classification and regression
│   ├── test_data_ingest.py   # Tests for API fetching and cache logic
│   ├── test_visualizations.py# Tests for chart generation
│   ├── test_web_collection.py# Tests for Flask routes and HTML generation
│   ├── test_cli_collection_io.py # Tests for CSV import and fuzzy search
│   └── test_cli_commands.py  # Tests for CLI command logic
│
├── data/
│   ├── cache/                # Auto-generated — cached API responses (gitignored)
│   │   ├── cards.json        # Card catalog from GitHub (fetched once, reused)
│   │   └── tournament.json   # Tournament data from Limitless TCG API (refreshed daily)
│   └── mock/
│       └── tournament.json   # Fallback mock data if API is unavailable
│
└── outputs/                  # Auto-generated — PNG charts (gitignored)
    ├── matchup_heatmap.png
    ├── wr_comparison.png
    └── role_attribution.png
```

---

## File Reference

### `main.py`
The browser UI entry point. Runs the full data pipeline on startup (fetches tournament data, classifies card roles, computes expected win rates, generates charts), then starts a Flask development server. Also defines `_make_reload_fn` — the callback wired into the `/refresh` endpoint that hot-reloads tournament data without restarting the server.

### `main_cli.py`
The terminal CLI entry point. Runs the same pipeline as `main.py` but launches an interactive terminal menu instead of a browser. No HTTP server involved.

### `wsgi.py`
Gunicorn entry point for VPS/production deployment. Calls `create_app()` from `main.py` and exposes the Flask `app` object. Run with:
```bash
venv/bin/gunicorn wsgi:app --workers 1 --bind 0.0.0.0:8765 --daemon
```
Single worker is required because the HTML cache is in-memory per process.

### `src/data_ingest.py`
Handles all external data fetching and caching.
- `fetch_card_catalog()` — downloads the full card list from GitHub and caches it to `data/cache/cards.json`
- `fetch_tournament_data()` — calls the Limitless TCG API for recent tournament standings and pairings; aggregates archetype win rates, meta shares, and the matchup matrix; caches to `data/cache/tournament.json`
- `load_card_catalog()` / `load_tournament_data()` — read from cache; fall back to mock data if cache is missing

### `src/models.py`
Core data models used throughout the pipeline.
- `Card` — represents a single card with id, name, HP, damage, type flags
- `Deck` — a named list of cards with an archetype label, built from tournament data
- `Collection` — a user's owned cards; computes deck completion % and missing card lists

### `src/matchup.py`
Computes the **expected win rate** for a deck against the current meta field using the formula:

```
E[WR] = Σ P(opponent plays deck d) × WR(your deck vs. d)
        d ∈ meta
```

Where `P(opponent plays deck d)` is the archetype's meta share and `WR` values come from the tournament matchup matrix.

### `src/card_roles.py`
Classifies every card into one of five roles based on tournament usage patterns, then fits a linear regression to measure how each role's fraction in a deck predicts its win rate.

**Role classification thresholds:**

| Role | Criteria |
|---|---|
| `win_condition` | Pokémon with 80+ max damage, appearing in ≤45% of meta decks |
| `engine` | Any card appearing in ≥50% of meta decks |
| `staple` | Any card appearing in 20–49% of meta decks |
| `tech` | Any card appearing in 5–19% of meta decks |
| `garnet` | Any card appearing in <5% of meta decks |

The regression R² is typically ~0.37 — about 37% of win-rate variance is explained by role composition alone.

### `src/visualizations.py`
Generates three matplotlib charts saved to `outputs/` and prints a terminal summary table.
- `plot_matchup_heatmap()` — color-coded head-to-head win rate matrix for all top archetypes
- `plot_wr_comparison()` — observed tournament win rate vs. regression-predicted win rate per archetype
- `plot_role_attribution()` — stacked bar showing each role's contribution to win rate per deck

### `src/web_collection.py`
The main browser UI — a ~3,100-line file containing the entire Flask application, HTML, CSS, and JavaScript as a single self-contained page rendered server-side.

**Key Python functions:**
- `_prepare_page_data()` — assembles all data (archetypes, matchup matrix, analysis results, card catalog, role map, regression) into a dict for the HTML template
- `_build_html()` — renders the full HTML page as a Python f-string with all data embedded as JSON constants
- `create_flask_app()` — creates and returns the Flask app with all routes; uses an in-memory HTML cache with `threading.Lock()` for thread-safe serving
- `_get_html()` — lazily builds the HTML cache on first request (stale-while-revalidate pattern)
- `_load_collection()` / `_load_custom_decks()` — SQLite helpers (retained for CLI compatibility; browser UI now uses localStorage)

**Flask routes:**
- `GET /` — serves the cached HTML page
- `GET /charts/<filename>` — serves generated chart PNGs
- `POST /refresh` — hot-reloads tournament data without restarting the server; uses a non-blocking lock to prevent concurrent refreshes (returns 429 if already running)

**Browser UI tabs (JavaScript):**

| Tab | What You Can Do |
|---|---|
| **Meta** | Browse top 15 archetypes ranked by meta share and win rate; hover a deck card to see its full card list; search by name |
| **Collection** | Track owned card copies; mark how many of each card you have; collection saves to browser localStorage |
| **Analysis** | Select your deck and a meta opponent; see Role DNA divergence, similarity-based win rate estimates, ownership breakdown, and cards to acquire |
| **Catalog** | Browse all ~3,200 cards with name search, set filter, and type filter |

**Custom decks:**
- Built inside the Collection tab via the NEW DECK modal
- Saved to `localStorage` under `pkmn_custom_decks`
- Loaded into `ARCHETYPES` at page load so they appear as selectable decks in the Analysis tab
- When selected in Analysis, matchup win rates are estimated via cosine similarity against known meta archetypes (top 3 most similar, similarity-weighted)
- Card roles assigned from `ROLE_MAP` (server-side classification exposed as a JS constant)

**Storage:**
- `localStorage['pkmn_collection']` — user's owned card counts (per browser, private)
- `localStorage['pkmn_custom_decks']` — user's custom deck definitions (per browser, private)
- `localStorage['darkMode']` — dark/light mode preference

### `src/cli/state.py`
`AppState` dataclass that holds all pipeline outputs (catalog, archetypes, matchup matrix, role map, regression, EWRs) and is passed between CLI commands.

### `src/cli/display.py`
Terminal output helpers: colored text, tabulate-based tables, deck card lists, pagination prompts. Used by all CLI commands to format output consistently.

### `src/cli/collection_io.py`
- CSV import — parses `data/my_collection.csv` with columns `card_id, count`
- Random collection generator — creates small/medium/large test collections
- Fuzzy card search — finds cards by approximate name match for manual entry

### `src/cli/commands.py`
Implements the four main CLI tabs:
- `MetaCommand` — displays archetype table, lets user drill into any deck's card list
- `CollectionCommand` — import, generate, add/remove cards, view owned cards
- `CatalogCommand` — search cards by name/type/set; view full card details
- `AnalysisCommand` — pick your deck and opponent; shows EWR, role attribution, and missing cards

### `src/cli/menu.py`
The top-level interactive menu loop. Presents a numbered list of available commands and dispatches to the appropriate `Command` subclass.

---

## How It Works

### Data Pipeline

```
Limitless TCG API  ──►  fetch_tournament_data()  ──►  data/cache/tournament.json
GitHub card DB     ──►  fetch_card_catalog()      ──►  data/cache/cards.json
                                │
                         main.py pipeline
                                │
              ┌─────────────────┼──────────────────┐
              ▼                 ▼                  ▼
      Expected Win Rate   Role Classification   Charts (PNG)
      matchup.py          card_roles.py         visualizations.py
                                │
                      Browser UI launched
                      web_collection.py
```

### Custom Deck Win Rate Estimation

For custom decks with no tournament history, matchup win rates are estimated by:
1. Computing cosine similarity between the custom deck's card vector and every known meta archetype
2. Taking the top 3 most similar archetypes
3. Producing a similarity-weighted average of their matchup win rates per opponent

This gives full nav pill estimates, tier rankings, and a weighted overall win rate — clearly labeled as estimated.

---

## Deployment (VPS)

The app runs on a Linux VPS behind Nginx using Gunicorn.

```bash
# SSH into the server
ssh root@YOUR_VPS_IP

# Pull latest changes
cd /root/pokemon-tcgp-analyzer-2
git pull origin main

# Restart Gunicorn
pkill -f gunicorn
venv/bin/gunicorn wsgi:app --workers 1 --bind 0.0.0.0:8765 --daemon

# Verify it's running
ps aux | grep gunicorn
```

Tournament data is refreshed daily via a cron job that calls `POST /refresh`.

---

## Running Tests

```bash
venv/bin/pytest tests/ -q
```

Run a single test file:
```bash
venv/bin/pytest tests/test_matchup.py -v
```

Run a single test:
```bash
venv/bin/pytest tests/test_matchup.py::test_known_deck_ewr -v
```

---

## Lint

```bash
flake8 src/ tests/ main.py main_cli.py --max-line-length=100
```

---

## Data Sources

- **Card Catalog** — [flibustier/pokemon-tcg-pocket-database](https://github.com/flibustier/pokemon-tcg-pocket-database) on GitHub
- **Tournament Data** — [Limitless TCG API](https://docs.limitlesstcg.com/developer.html)

---

## Team & Contributions

| Member | Deliverable |
|---|---|
| **Jofer Santiago** | Data Pipeline & API Integration — fetching and caching tournament data from Limitless TCG API and card catalog from GitHub |
| **Deborah Argayosa** | Core Analysis Engine — expected win rate calculation, card role classification, and regression model |
| **John Rudolph Navarro** | Browser UI — full web interface with Meta, Collection, Analysis, and Catalog tabs |
| **King Herald Monteroyo** | Python CLI — terminal version with all 4 tabs, CSV import, and random collection generator |
| **Stefanie Joy Rosete** | Testing & Documentation — test suite, README, and project documentation |

---

## Inspiration

- **[Shoppu by Mochi](https://shoppu.mochi.at/)** — UI design and aesthetic inspiration
