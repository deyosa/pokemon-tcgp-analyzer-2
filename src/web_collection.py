from __future__ import annotations
import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from flask import Flask, Response, abort, jsonify, send_from_directory
from src.models import Card, Collection
_CDN = "https://limitlesstcg.nyc3.cdn.digitaloceanspaces.com/pocket"
# Catalog set codes that differ from the CDN's set codes
_SET_REMAP = {"PROMO-A": "P-A", "PROMO-B": "P-B"}


def _card_image_url(card_id: str) -> str:
    """Return the Limitless CDN image URL for a card ID like 'A1-036' or 'PROMO-A-005'."""
    parts = card_id.rsplit("-", 1)
    if len(parts) != 2:
        return ""
    set_code, number = parts
    cdn_set = _SET_REMAP.get(set_code, set_code)
    return f"{_CDN}/{cdn_set}/{cdn_set}_{number}_EN.webp"


def _prepare_page_data(
    archetypes: list[dict],
    catalog: dict[str, Card],
    my_cards: dict,
    ewrs: list[float],
    attributions: list[dict],
    meta_decks: list,
    custom_decks: list[dict] | None = None,
    matchup_matrix: dict | None = None,
    role_map: dict | None = None,
    regression=None,
) -> dict:
    """Prepare all JSON-serialisable data the browser tabs need."""
    def _fallback_card(card_id: str, name: str | None = None) -> Card:
        return Card(id=card_id, name=name or card_id, card_type="Pokemon")

    def _normalize(text: str) -> str:
        return __import__("re").sub(r'[^a-z0-9]+', ' ', (text or '').lower()).strip()

    def _guess_missing_card_names(arch_name: str, known_names: list[str]) -> list[str]:
        arch_norm = _normalize(arch_name)
        remaining = arch_norm
        for name in known_names:
            if not name:
                continue
            name_norm = _normalize(name)
            if name_norm and name_norm in remaining:
                remaining = remaining.replace(name_norm, ' ')
            stripped = _normalize(__import__("re").sub(r'\s+(ex|v|vmax|vstar|gx)\s*$', '', name.lower()))
            if stripped and stripped in remaining:
                remaining = remaining.replace(stripped, ' ')
        remaining = _normalize(remaining)
        if not remaining:
            return []
        tokens = [t for t in remaining.split() if t not in {"and", "vs", "the", "a", "an", "of"}]
        if len(tokens) == 1:
            return [tokens[0].title()]
        if len(tokens) == 2 and (tokens[0] == "ex" or tokens[1] == "ex"):
            return [" ".join(tokens).title()]
        return []

    # ── COLLECTION tab ──────────────────────────────────────────────────────
    decks_data = []
    for arch in archetypes:
        known_names = [catalog[entry["id"]].name for entry in arch.get("cards", []) if entry["id"] in catalog]
        missing_names = _guess_missing_card_names(arch.get("name", ""), known_names)
        missing_iter = iter(missing_names)
        cards_data = []
        for entry in arch.get("cards", []):
            card = catalog.get(entry["id"])
            if card is None:
                fallback_name = entry.get("name") or next(missing_iter, None) or entry["id"]
                card = _fallback_card(entry["id"], fallback_name)
            cards_data.append({
                "id": entry["id"],
                "name": card.name,
                "type": card.card_type,
                "need": entry["count"],
                "have": my_cards.get(entry["id"], 0),
                "img": _card_image_url(entry["id"]),
                "role": role_map.get(entry["id"], "garnet") if role_map else "garnet",
            })
        if cards_data:
            decks_data.append({
                "id": arch["id"],
                "name": arch["name"],
                "meta_share": arch.get("meta_share", 0),
                "win_rate": arch.get("win_rate", 0.5),
                "cards": cards_data,
                "custom": False,
            })

    # Append user-created custom decks
    for cdeck in (custom_decks or []):
        cards_data = []
        for entry in cdeck.get("cards", []):
            card = catalog.get(entry["id"]) or _fallback_card(entry["id"])
            cards_data.append({
                "id": entry["id"],
                "name": card.name,
                "type": card.card_type,
                "need": entry["count"],
                "have": my_cards.get(entry["id"], 0),
                "img": _card_image_url(entry["id"]),
                "role": role_map.get(entry["id"], "garnet") if role_map else "garnet",
            })
        if cards_data:
            decks_data.append({
                "id": cdeck["id"],
                "name": cdeck["name"],
                "meta_share": 0,
                "win_rate": 0,
                "cards": cards_data,
                "custom": True,
            })

    # ── META tab ─────────────────────────────────────────────────────────────
    def _hero_imgs(arch: dict) -> list[str]:
        """Return up to 2 CDN image URLs for the representative Pokémon of an archetype.

        Priority:
        1. Pokémon whose base name (stripped of 'ex'/'v'/'vmax' suffix) appears
           in the archetype name — ordered by how early they appear in the name.
        2. Top-2 Pokémon by HP as fallback.
        """
        import re as _re
        arch_name_lower = _re.sub(r'[^a-z0-9]+', ' ', arch.get("name", "").lower()).strip()

        def _base(name: str) -> str:
            """Strip common card-suffix words so 'Altaria ex' → 'altaria'."""
            return _re.sub(r'\s+(ex|v|vmax|vstar|gx)\s*$', '', name.lower()).strip()

        def _normalize(text: str) -> str:
            return _re.sub(r'[^a-z0-9]+', ' ', text.lower()).strip()

        def _name_score(card_name: str) -> int:
            """Lower = better match. -1 means no match."""
            base = _normalize(_base(card_name))
            full = _normalize(card_name)
            match = None
            if base:
                match = _re.search(rf'\b{_re.escape(base)}\b', arch_name_lower)
            if match:
                return match.start()
            if full:
                match = _re.search(rf'\b{_re.escape(full)}\b', arch_name_lower)
            return match.start() if match else -1

        known_names = [catalog[entry["id"]].name for entry in arch.get("cards", []) if entry["id"] in catalog]
        missing_names = _guess_missing_card_names(arch.get("name", ""), known_names)
        missing_iter = iter(missing_names)

        named: list[tuple[int, int, str]] = []   # (position, hp, url)
        by_hp: list[tuple[int, str]] = []
        unknown_urls: list[str] = []

        for entry in arch.get("cards", []):
            url = _card_image_url(entry["id"])
            card = catalog.get(entry["id"])
            if card is None:
                fallback_name = next(missing_iter, entry["id"])
                score = _name_score(fallback_name)
                if score >= 0:
                    named.append((score, 0, url))
                else:
                    unknown_urls.append(url)
                continue
            if not card.is_pokemon:
                continue
            score = _name_score(card.name)
            if score >= 0:
                named.append((score, -(card.hp or 0), url))
            else:
                by_hp.append((card.hp or 0, url))

        # Sort named by position in deck name (earliest first)
        named.sort(key=lambda x: (x[0], x[1]))
        result = list(dict.fromkeys(u for _, _, u in named))[:2]

        # Prefer missing catalog card URLs before HP fallbacks.
        if len(result) < 2:
            for url in unknown_urls:
                if url not in result:
                    result.append(url)
                if len(result) == 2:
                    break

        # Fill remaining slots with highest-HP Pokémon
        if len(result) < 2:
            by_hp.sort(reverse=True)
            for _, url in by_hp:
                if url not in result:
                    result.append(url)
                if len(result) == 2:
                    break

        # Last resort: first card of any type
        if not result:
            for entry in arch.get("cards", []):
                result.append(_card_image_url(entry["id"]))
                break

        return result

    meta_data = sorted(
        [
            {
                "id": arch["id"],
                "name": arch["name"],
                "meta_share": round(arch.get("meta_share", 0) * 100, 1),
                "win_rate": round(arch.get("win_rate", 0.5) * 100, 1),
                "ewr": round(ewr * 100, 1),
                "hero_img": (_hero_imgs(arch) + [""])[0],
                "hero_imgs": _hero_imgs(arch),
            }
            for arch, ewr in zip(archetypes, ewrs)
        ],
        key=lambda x: x["meta_share"],
        reverse=True,
    )

    # ── ANALYSIS tab ─────────────────────────────────────────────────────────
    _ROLES = ["win_condition", "engine", "staple", "tech", "garnet"]
    collection = Collection(cards=my_cards)
    analysis_data = []
    for deck, ewr, attr in zip(meta_decks, ewrs, attributions):
      completion = collection.completion_percent(deck)
      missing = collection.missing_cards(deck)
      completion_by_name = collection.completion_percent_by_name(deck)
      missing_by_name = collection.missing_cards_by_name(deck)
      top_role = max(attr, key=lambda r: attr[r]) if attr else "N/A"  # noqa: B023
      analysis_data.append({
        "id": deck.archetype_id,
        "name": deck.archetype_label,
        "completion": completion,
      "completion_by_name": completion_by_name,
        "ewr": round(ewr * 100, 1),
        "top_role": top_role,
        "attribution": {r: round(attr.get(r, 0) * 100, 2) for r in _ROLES},
        "predicted_wr": round(
          (sum(attr.values()) + (regression.intercept if regression else 0)) * 100, 1
        ),
        "missing": [
          {
            "name": c.name,
            "count": n,
            "role": role_map.get(c.id, "garnet") if role_map else "garnet",
          }
          for c, n in missing
        ],
        "missing_by_name": [{"name": n, "count": c} for n, c in missing_by_name],
        "total_missing": len(missing),
      })
    analysis_data.sort(key=lambda r: r["completion"], reverse=True)

    # ── CATALOG tab ──────────────────────────────────────────────────────────
    catalog_list = sorted(
        [
            {
                "id": card.id,
                "name": card.name,
                "type": card.card_type,
                "set": card.set_id,
                "img": _card_image_url(card.id),
            }
            for card in catalog.values()
        ],
        key=lambda x: x["id"],
    )

    return {
        "decks": decks_data,
        "meta": meta_data,
        "analysis": analysis_data,
        "catalog": catalog_list,
        "matchup": matchup_matrix or {},
        "role_map": role_map or {},
        "regression": {
            "r2":        round(regression.r_squared, 3),
            "coef":      {r: round(regression.coef[r] * 100, 2) for r in _ROLES},
            "intercept": round(regression.intercept * 100, 1),
        } if regression else {},
    }


def _build_html(page_data: dict, my_cards: dict) -> str:  # noqa: E501
    """Return a fully self-contained retro HTML page as a string."""
    built_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    decks_json = json.dumps(page_data["decks"])
    meta_json = json.dumps(page_data["meta"])
    analysis_json = json.dumps(page_data["analysis"])
    catalog_json = json.dumps(page_data.get("catalog", []))
    matchup_json = json.dumps(page_data.get("matchup", {}))
    role_map_json = json.dumps(page_data.get("role_map", {}))
    regression_json = json.dumps(page_data.get("regression", {}))

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>PKMN TCG POCKET // COLLECTION MANAGER</title>
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;600;700&family=Space+Mono:wght@400;700&family=JetBrains+Mono:wght@400;600;700&display=swap" rel="stylesheet">
<style>
  :root {{
    --bg:        #FBF3E4;
    --panel:     #F2E8D2;
    --card-bg:   #FFFFFF;
    --border:    #0A0A0A;
    --text:      #0A0A0A;
    --pink:      #E63462;
    --blue:      #1B5DEF;
    --green:     #2E7D32;
    --gold:      #F9C846;
    --red:       #CC2222;
    --dim:       #6B6660;
    --font:      'Space Grotesk', sans-serif;
    --pixel:     'Space Mono', monospace;
    --mono:      'JetBrains Mono', monospace;
    --shadow:     4px 4px 0 0 #0A0A0A;
    --shadow-lg:  8px 8px 0 0 #0A0A0A;
    --shadow-sm:  2px 2px 0 0 #0A0A0A;
    --shadow-hard:6px 6px 0 0 #0A0A0A;
    --bg-deep:   #F2E8D2;
  }}
  body.dark {{
    --bg: #1A1A1A; --panel: #242424; --card-bg: #2A2A2A;
    --border: #E0D6C0; --text: #E0D6C0; --dim: #8A8480; --bg-deep: #141414;
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; border-radius: 0 !important; }}
  body {{
    background: var(--bg);
    color: var(--text);
    font-family: var(--mono);
    font-size: 13px;
    min-height: 100vh;
    overflow-x: hidden;
  }}
  @keyframes slideIn {{from{{transform:translateY(-10px);opacity:0}}to{{transform:translateY(0);opacity:1}}}}
  @keyframes shoppu-marquee {{from{{transform:translateX(0)}}to{{transform:translateX(-50%)}}}}
  @keyframes shoppu-jump {{0%,100%{{transform:translateY(0)}}50%{{transform:translateY(-3px)}}}}
  @keyframes shoppu-wiggle {{0%,100%{{transform:rotate(-1deg)}}50%{{transform:rotate(1deg)}}}}
  #main-ui {{ display: flex; flex-direction: column; height: 100vh; }}

  /* ── Top nav ── */
  #top-nav {{
    position: relative; z-index: 200; flex-shrink: 0;
    display: flex; align-items: stretch;
    background: var(--bg); border-bottom: 4px solid var(--border);
    height: 72px;
  }}
  #nav-logo {{
    display: flex; align-items: center; gap: 14px;
    padding: 0 24px; border-right: 4px solid var(--border);
    min-width: 230px; flex-shrink: 0; text-decoration: none;
  }}
  #nav-logo svg {{ flex-shrink: 0; }}
  #nav-logo-name {{
    font-family: var(--font); font-size: 18px; color: var(--text);
    font-weight: 700; line-height: 1.2;
  }}
  #nav-logo-sub {{
    font-family: var(--pixel); font-size: 12px; color: var(--dim); margin-top: 4px;
  }}
  #nav-links {{ display: flex; flex: 1; align-items: stretch; }}
  .tab-btn {{
    background: transparent; border: none;
    border-right: 4px solid var(--border);
    padding: 0 22px; cursor: pointer;
    display: flex; flex-direction: column;
    align-items: center; justify-content: center;
    gap: 5px; transition: background .1s;
    min-width: 120px;
  }}
  .tab-btn:last-child {{ border-right: none; }}
  .tab-btn {{ font-family: var(--font); font-size: 15px; font-weight: 700; color: var(--text); letter-spacing: 0.5px; }}
  .tab-btn:hover {{ background: var(--border); color: #fff; }}
  .tab-btn.active {{ background: var(--border); color: #fff; }}
  #nav-right {{
    display: flex; align-items: center; gap: 12px;
    padding: 0 20px; border-left: 4px solid var(--border); flex-shrink: 0;
  }}
  #total-label {{ font-family: var(--font); font-size: 14px; color: var(--text); white-space: nowrap; }}
  #save-btn {{
    background: var(--border); border: 2px solid var(--border); color: var(--bg);
    font-family: var(--font); font-size: 12px; padding: 10px 20px;
    cursor: pointer; letter-spacing: 1px; white-space: nowrap;
    box-shadow: var(--shadow); transition: box-shadow .07s, transform .07s;
  }}
  #save-btn:hover {{ box-shadow: none; transform: translate(4px,4px); }}
  #kofi-btn {{
    background: #FF5E5B; border: 2px solid var(--border); color: #fff;
    font-family: var(--font); font-size: 13px; padding: 8px 12px;
    cursor: pointer; white-space: nowrap; line-height: 1;
    box-shadow: var(--shadow); transition: box-shadow .07s, transform .07s;
  }}
  #kofi-btn:hover {{ box-shadow: none; transform: translate(4px,4px); }}
  #dark-btn {{
    background: var(--bg); border: 2px solid var(--border); color: var(--text);
    font-family: var(--font); font-size: 15px; padding: 6px 10px; line-height: 1;
    cursor: pointer; box-shadow: var(--shadow); transition: box-shadow .07s, transform .07s;
  }}
  #dark-btn:hover {{ box-shadow: none; transform: translate(4px,4px); }}

  /* ── Support modal ── */
  #support-modal {{
    display: none; position: fixed; top: 70px; right: 20px; z-index: 500;
    background: var(--bg); border: 4px solid var(--border);
    box-shadow: 6px 6px 0 0 #0A0A0A; padding: 20px 20px 16px; min-width: 270px;
  }}
  #support-modal h3 {{
    font-family: var(--font); font-size: 12px; letter-spacing: 2px;
    color: var(--text); margin: 0 0 12px 0;
    border-bottom: 2px solid var(--border); padding-bottom: 8px;
  }}
  .support-link {{
    display: flex; align-items: center; gap: 10px; padding: 10px 12px; margin-bottom: 8px;
    border: 2px solid var(--border); text-decoration: none; color: var(--text);
    font-family: var(--font); font-size: 13px; letter-spacing: 1px;
    box-shadow: 3px 3px 0 0 #0A0A0A; transition: box-shadow .07s, transform .07s;
  }}
  .support-link:last-child {{ margin-bottom: 0; }}
  .support-link:hover {{ box-shadow: none; transform: translate(3px,3px); color: var(--text); }}
  .support-link .sl-icon {{ font-size: 22px; flex-shrink: 0; }}
  .support-link .sl-text {{ display: flex; flex-direction: column; gap: 2px; }}
  .support-link .sl-title {{ font-weight: 700; }}
  .support-link .sl-sub {{ font-family: var(--mono); font-size: 12px; color: var(--dim); }}
  #support-close {{
    position: absolute; top: 6px; right: 8px; background: none; border: none;
    color: var(--dim); font-size: 16px; cursor: pointer; padding: 4px; line-height: 1;
  }}
  #support-close:hover {{ color: var(--text); }}
  @media (max-width: 480px) {{
    #support-modal {{ right: 8px; left: 8px; min-width: unset; top: 60px; }}
  }}

  /* ── Marquee strip ── */
  #marquee-strip {{
    flex-shrink: 0; border-bottom: 4px solid var(--border);
    background: var(--border); color: var(--bg);
    font-family: var(--font); font-size: 16px;
    padding: 8px 0; overflow: hidden; white-space: nowrap;
  }}
  #marquee-inner {{
    display: inline-flex;
    animation: shoppu-marquee 40s linear infinite;
  }}
  .mq-item {{ padding: 0 40px; }}
  .mq-slash {{ color: var(--pink); margin-right: 6px; }}

  /* ── Support banner ── */
  #support-banner {{
    flex-shrink: 0; border-bottom: 2px solid var(--border);
    background: #FF5E5B; color: #fff;
    font-family: var(--font); font-size: 13px; letter-spacing: 1px;
    padding: 6px 20px; display: flex; align-items: center; justify-content: center; gap: 16px;
  }}
  #support-banner span {{ opacity: .9; }}
  .sb-link {{
    color: #fff; text-decoration: none; font-weight: 700;
    border: 2px solid rgba(255,255,255,.6); padding: 3px 10px;
    transition: background .1s;
  }}
  .sb-link:hover {{ background: rgba(255,255,255,.2); }}
  .sb-link.sb-dark {{
    background: #0A0A0A; border-color: #0A0A0A; color: #fff;
  }}
  .sb-link.sb-dark:hover {{ background: #222; }}
  @media (max-width: 480px) {{
    #support-banner {{ font-size: 12px; padding: 5px 12px; gap: 10px; flex-wrap: wrap; justify-content: center; }}
  }}

  /* ── Status toast ── */
  #status-msg {{
    position: fixed; bottom: 24px; left: 50%; transform: translateX(-50%);
    font-family: var(--font); font-size: 13px; color: var(--bg);
    background: var(--border); border: 4px solid var(--border);
    box-shadow: var(--shadow); padding: 12px 24px; z-index: 5000;
    opacity: 0; transition: opacity .3s; pointer-events: none; white-space: nowrap;
  }}
  #status-msg.visible {{ opacity: 1; pointer-events: auto; }}
  #status-msg.ok  {{ background: var(--green); border-color: var(--green); }}
  #status-msg.err {{ background: var(--red);   border-color: var(--red); }}

  /* ── Content ── */
  #content {{ flex: 1; overflow: hidden; min-height: 0; }}
  .tab-pane {{
    display: none; height: 100%; overflow-y: auto;
    padding: 40px 48px 60px;
  }}
  .tab-pane.active {{ display: block; }}
  .tab-pane::-webkit-scrollbar {{ width: 6px; }}
  .tab-pane::-webkit-scrollbar-thumb {{ background: #ccc; }}

  /* ── Page header (per-tab large title) ── */
  .page-header {{
    padding: 0 0 24px; margin-bottom: 36px;
    border-bottom: 4px solid var(--border);
  }}
  .page-header h1 {{
    font-family: var(--font); font-size: 56px; font-weight: 700;
    letter-spacing: -1px; line-height: 1; color: var(--text);
  }}
  .page-header-jp {{
    font-family: var(--pixel); font-size: 13px; color: var(--pink); margin-top: 10px;
  }}
  .page-subtitle {{
    font-family: var(--mono); font-size: 12px; color: var(--dim);
    margin-top: 12px; line-height: 1.6;
  }}
  .page-subtitle .ps-updated {{
    display: inline-block; margin-top: 6px; font-size: 12px;
    background: var(--panel); border: 1px solid var(--border);
    padding: 2px 8px; color: var(--dim);
  }}
  .page-subtitle .ps-hint {{
    display: inline-block; margin-top: 4px; font-size: 12px;
    color: var(--dim); font-style: italic;
  }}

  /* ── Section label ── */
  .section-label {{
    display: flex; align-items: baseline; gap: 14px;
    margin: 40px 0 20px; border-bottom: 2px solid var(--border); padding-bottom: 12px;
  }}
  .section-label h2 {{ font-family: var(--font); font-size: 26px; font-weight: 700; }}
  .section-label span {{ font-family: var(--pixel); font-size: 12px; color: var(--dim); }}
  .wr-hi {{ color: var(--green); font-weight: bold; }}
  .wr-lo {{ color: var(--red); }}

  /* ── META tab — arch-card grid ── */
  #meta-search-wrap {{ margin-bottom: 20px; }}
  #meta-search-input {{
    font-family: var(--mono); font-size: 12px; padding: 10px 16px;
    border: 4px solid var(--border); background: var(--card-bg); color: var(--text);
    width: 100%; max-width: 360px; outline: none; box-shadow: var(--shadow);
  }}
  #meta-search-input:focus {{ box-shadow: var(--shadow-lg); }}
  .meta-grid {{ display: grid; grid-template-columns: repeat(5, 1fr); gap: 20px; }}
  .arch-card {{
    background: var(--card-bg); border: 4px solid var(--border);
    cursor: default; box-shadow: var(--shadow); overflow: hidden; position: relative;
    transition: box-shadow .1s, transform .1s; animation: slideIn .3s ease;
  }}
  .arch-card:hover {{ box-shadow: var(--shadow-lg); transform: translate(-4px,-4px); }}
  .arch-hover-overlay {{
    position: absolute; inset: 0; background: rgba(10,10,10,0.92); display: flex; flex-direction: column;
    align-items: center; justify-content: flex-start; gap: 12px; padding: 16px; text-align: center;
    opacity: 0; visibility: hidden; transition: opacity 0.15s ease, visibility 0.15s ease; z-index: 100;
    overflow-y: auto; max-height: 100%;
  }}
  .arch-card:hover .arch-hover-overlay {{ opacity: 1; visibility: visible; }}
  .arch-hover-title {{
    font-family: var(--font); font-size: 14px; font-weight: 700; color: #fff;
    word-break: break-word; line-height: 1.3; flex-shrink: 0;
  }}
  .arch-hover-cards {{
    display: grid; grid-template-columns: repeat(auto-fit, minmax(70px, 1fr)); gap: 8px; width: 100%;
    flex: 1; overflow-y: auto; padding: 8px 0;
  }}
  .arch-hover-cards::-webkit-scrollbar {{ width: 4px; }}
  .arch-hover-cards::-webkit-scrollbar-thumb {{ background: rgba(255,255,255,0.3); border-radius: 2px; }}
  .meta-card-item {{
    display: flex; flex-direction: column; align-items: center; gap: 4px; font-size: 13px; color: #fff;
  }}
  .meta-card-thumb {{
    width: 60px; height: 80px; background: var(--panel); border: 2px solid rgba(255,255,255,0.2);
    object-fit: contain; display: flex; align-items: center; justify-content: center;
  }}
  img.meta-card-thumb {{ cursor: zoom-in; }}
  .meta-card-name {{
    font-family: var(--mono); font-size: 12px; color: #ccc; line-height: 1.2; word-break: break-word;
    max-width: 70px; overflow: hidden; text-overflow: ellipsis; display: -webkit-box; -webkit-line-clamp: 2;
  }}
  .meta-card-count {{
    font-family: var(--pixel); font-size: 12px; color: var(--gold); font-weight: 700;
  }}
  .arch-img-area {{
    height: 220px; background: var(--panel); border-bottom: 4px solid var(--border);
    display: flex; align-items: center; justify-content: center;
    position: relative; overflow: hidden; gap: 4px;
  }}
  .arch-img-area img {{
    height: 100%; width: 50%; object-fit: contain; display: block; flex-shrink: 0;
  }}
  .arch-img-area.single img {{
    width: 100%;
  }}
  .arch-sticker {{
    display: inline-block;
    background: var(--gold); border: 2px solid var(--border);
    font-family: var(--pixel); font-size: 12px; color: #0A0A0A;
    padding: 4px 8px; box-shadow: 2px 2px 0 0 var(--border);
    margin-bottom: 8px;
  }}
  .arch-body {{ padding: 16px; }}
  .arch-name {{
    font-family: var(--font); font-size: 13px; color: var(--text);
    margin-bottom: 10px; font-weight: 700; white-space: nowrap;
    overflow: hidden; text-overflow: ellipsis;
  }}
  .arch-stats {{ display: flex; align-items: flex-end; justify-content: space-between; }}
  .arch-share {{ font-family: var(--font); font-size: 28px; color: var(--pink); font-weight: 700; line-height: 1; }}
  .arch-wr {{ font-family: var(--pixel); font-size: 12px; color: var(--dim); text-align: right; line-height: 2.2; }}
  .arch-card-hi {{ border-left: 8px solid var(--green) !important; }}
  .arch-card-lo {{ border-left: 8px solid var(--red) !important; }}
  .arch-owned-row {{ display: flex; align-items: center; justify-content: space-between; margin-top: 8px; padding-top: 8px; border-top: 2px solid var(--border); }}
  .arch-owned-pct {{ font-family: var(--mono); font-size: 12px; color: var(--dim); }}
  .arch-buildable-yes {{ font-family: var(--pixel); font-size: 12px; color: var(--green); }}
  .arch-buildable-no {{ font-family: var(--pixel); font-size: 12px; color: var(--red); }}

  /* ── COLLECTION tab ── */
  #collection-pane {{
    padding: 0 !important;
    display: none;
    grid-template-columns: 420px 1fr;
    height: 100%;
    overflow: hidden;
  }}
  #collection-pane.active {{ display: grid !important; }}
  #deck-list {{
    border-right: 4px solid var(--border);
    overflow-y: auto; padding: 14px 12px 40px;
    background: var(--panel);
  }}
  #deck-list-hint {{
    font-family: var(--mono); font-size: 12px; color: var(--dim);
    padding: 4px 4px 10px; line-height: 1.6; letter-spacing: 0.5px;
  }}
  #deck-list::-webkit-scrollbar {{ width: 6px; }}
  #deck-list::-webkit-scrollbar-thumb {{ background: var(--dim); }}
  .deck-item {{
    cursor: pointer; padding: 14px; margin-bottom: 10px;
    border: 4px solid var(--border); background: var(--card-bg);
    box-shadow: var(--shadow);
    transition: box-shadow .1s, transform .1s; animation: slideIn .3s ease;
  }}
  .deck-item:hover {{ background: rgba(230,52,98,.06); box-shadow: var(--shadow-lg); transform: translate(-4px,-4px); }}
  .deck-item.active {{ background: var(--border); box-shadow: none; transform: translate(4px,4px); }}
  .deck-item .dname {{
    font-family: var(--font); font-size: 12px; color: var(--text);
    margin-bottom: 8px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }}
  .deck-item.active .dname {{ color: #fff; }}
  .xp-bar-wrap {{ background: var(--panel); height: 10px; border: 2px solid var(--border); margin: 6px 0; padding: 2px; }}
  .xp-bar {{ height: 100%; background: var(--green); transition: width .4s ease; }}
  .xp-bar.low {{ background: var(--red); }}
  .xp-bar.mid {{ background: var(--gold); }}
  .deck-meta {{ display: flex; justify-content: space-between; font-size: 13px; color: var(--dim); margin-top: 6px; font-family: var(--mono); }}
  .deck-item.active .deck-meta {{ color: rgba(255,255,255,.65); }}
  #card-area {{ overflow-y: auto; padding: 28px 32px; background: var(--bg); }}
  #card-area::-webkit-scrollbar {{ width: 6px; }}
  #card-area::-webkit-scrollbar-thumb {{ background: var(--dim); }}
  #deck-title-row {{
    display: flex; align-items: center; justify-content: center;
    gap: 10px; margin-bottom: 24px; padding-bottom: 16px;
    border-bottom: 4px solid var(--border);
  }}
  #deck-title {{ font-family: var(--font); font-size: 16px; color: var(--text); flex: 1; text-align: center; font-weight: 700; }}
  #clear-deck-btn {{
    background: transparent; border: 2px solid var(--red); color: var(--red);
    font-family: var(--font); font-size: 12px; padding: 8px 12px;
    cursor: pointer; white-space: nowrap; flex-shrink: 0;
    box-shadow: var(--shadow-sm); transition: box-shadow .07s, transform .07s;
  }}
  #clear-deck-btn:hover {{ background: var(--red); color: #fff; box-shadow: none; transform: translate(4px,4px); }}
  #clear-deck-btn:disabled {{ opacity: .3; cursor: not-allowed; box-shadow: none; transform: none; }}
  #share-deck-btn {{
    background: transparent; border: 2px solid var(--blue); color: var(--blue);
    font-family: var(--font); font-size: 12px; padding: 8px 12px;
    cursor: pointer; white-space: nowrap; flex-shrink: 0;
    box-shadow: var(--shadow-sm); transition: box-shadow .07s, transform .07s;
  }}
  #share-deck-btn:hover {{ background: var(--blue); color: #fff; box-shadow: none; transform: translate(4px,4px); }}
  #share-deck-btn:disabled {{ opacity: .3; cursor: not-allowed; box-shadow: none; transform: none; }}
  #card-grid {{ display: grid; grid-template-columns: repeat(5, 1fr); gap: 16px; }}
  #collection-empty {{
    display: flex; flex-direction: column; align-items: center; justify-content: center;
    height: 100%; gap: 16px; color: var(--dim);
  }}
  #collection-empty-icon {{ font-family: var(--font); font-size: 48px; animation: shoppu-wiggle 1s ease-in-out infinite; }}
  #collection-empty-text {{ font-family: var(--mono); font-size: 12px; text-align: center; line-height: 2; letter-spacing: 1px; }}

  /* ── Card tile (Collection + Catalog shared) ── */
  .card {{
    border: 4px solid var(--border); background: var(--card-bg);
    padding: 10px 8px; text-align: center; position: relative;
    box-shadow: var(--shadow); transition: box-shadow .1s, transform .1s;
  }}
  .card:hover {{ box-shadow: var(--shadow-lg); transform: translate(-4px,-4px); }}
  .card.owned  {{ border-color: var(--green); }}
  .card.partial {{ border-color: var(--gold); }}
  .card.missing {{ opacity: .55; }}
  .card-type-badge {{
    font-family: var(--font); font-size: 12px; padding: 3px 8px;
    margin-bottom: 6px; display: inline-block; letter-spacing: 0.5px;
    border: 1px solid rgba(0,0,0,.2); color: var(--text);
  }}
  .type-Pokemon {{ background: #FFD9E6; }}
  .type-Trainer {{ background: #FFE5C2; }}
  .type-Energy  {{ background: #FFF5C2; }}
  .card-name {{ font-family: var(--font); font-size: 12px; color: var(--text); margin-bottom: 8px; line-height: 1.6; min-height: 24px; word-break: break-word; }}
  .need-label {{ font-family: var(--font); font-size: 12px; color: var(--dim); margin-bottom: 6px; }}
  .counter {{ display: flex; align-items: center; justify-content: center; gap: 6px; margin-top: 6px; }}
  .btn-counter {{
    background: var(--border); border: 2px solid var(--border); color: var(--bg);
    font-family: var(--font); font-size: 14px; width: 30px; height: 30px;
    cursor: pointer; line-height: 1; padding: 0;
    box-shadow: var(--shadow-sm); transition: box-shadow .07s, transform .07s;
  }}
  .btn-counter:hover {{ box-shadow: none; transform: translate(4px,4px); }}
  .count-display {{ font-family: var(--font); font-size: 14px; color: var(--text); min-width: 24px; text-align: center; }}
  .card-img {{ width: 84px; height: 116px; object-fit: contain; display: block; margin: 4px auto; border: 1px solid #ddd; background: var(--panel); }}
  .card-img-fallback {{
    width: 84px; height: 116px; display: flex; align-items: center;
    justify-content: center; font-size: 36px; border: 1px solid #ddd;
    margin: 4px auto; background: var(--panel);
  }}
  /* Catalog owned badge */
  .owned-badge {{
    position: absolute; top: 5px; right: 5px;
    background: var(--green); color: #fff;
    font-family: var(--pixel); font-size: 6px; padding: 3px 5px;
    border: 1px solid var(--border); letter-spacing: 0;
  }}

  /* ── ANALYSIS tab (Shoppu Fighter layout) ── */
  #analysis-pane {{ padding: 0 !important; }}
  .an-header {{
    padding: 56px 32px 24px; border-bottom: 2px solid var(--border);
  }}
  .an-header-eyebrow {{
    display: flex; align-items: center; gap: 12px; margin-bottom: 14px;
    font-family: var(--pixel); font-size: 12px;
  }}
  .an-header-eyebrow .an-route {{ color: var(--text); }}
  .an-header-eyebrow .an-rule {{ flex: 1; height: 2px; background: var(--border); }}
  .an-header-eyebrow .an-badge {{ color: var(--text); }}
  .an-headline {{
    font-family: var(--font); font-size: 56px; font-weight: 700; line-height: 1;
    color: var(--text); margin-bottom: 10px;
  }}
  .an-headline .pink {{ color: var(--pink); }}
  .an-subtitle {{
    font-family: var(--pixel); font-size: 13px; color: var(--dim);
  }}
  .an-desc {{
    font-family: var(--mono); font-size: 13px; color: var(--dim);
    margin-top: 10px; line-height: 1.7; max-width: 560px;
  }}

  /* Scoreboard hero */
  .an-scoreboard {{
    display: grid; grid-template-columns: 1fr 320px 1fr;
    gap: 36px; padding: 40px 32px;
  }}
  .an-score-card-wrap {{ display: flex; flex-direction: column; gap: 10px; }}
  .an-score-card-wrap.right {{ align-items: flex-end; }}
  .an-sc-above {{
    font-family: var(--pixel); font-size: 12px; color: var(--dim);
  }}
  .an-sc-body {{
    width: 100%; height: 440px; background: var(--bg-deep);
    border: 4px solid var(--border); box-shadow: var(--shadow-hard);
    position: relative; cursor: pointer; overflow: hidden;
    transition: transform .1s;
    display: flex; align-items: center; justify-content: center;
  }}
  .an-sc-body:hover {{ transform: translate(-2px,-2px); }}
  /* 3-card row inside the score card */
  .an-sc-hand {{
    display: flex; align-items: center; justify-content: center;
    height: 100%; width: 100%; padding: 20px;
    gap: 10px; overflow: hidden;
  }}
  .an-sc-hand-card {{
    flex: 0 0 auto; width: 160px; height: 100%;
    max-height: 400px;
    border: 2px solid var(--border); box-shadow: 2px 2px 0 0 #0A0A0A;
    overflow: hidden; background: var(--panel); position: relative;
  }}
  .an-sc-hand-card img {{ width: 100%; height: 100%; object-fit: contain; display: block; }}
  .an-sc-hand-card.hc-left  {{ z-index: 1; }}
  .an-sc-hand-card.hc-mid   {{ z-index: 3; }}
  .an-sc-hand-card.hc-right {{ z-index: 1; }}
  .an-sc-blank {{
    width: 100%; height: 100%; display: flex; align-items: center; justify-content: center;
    font-size: 28px; color: var(--dim); background: var(--bg-deep);
  }}
  .an-sc-tag {{
    position: absolute; bottom: 8px; left: 8px;
    background: white; border: 2px solid var(--border);
    font-family: var(--pixel); font-size: 12px; padding: 4px 8px;
  }}
  .an-sc-tag.right {{ left: auto; right: 8px; }}
  .an-sc-name {{
    font-family: var(--font); font-size: 32px; line-height: 1.05;
  }}
  .an-sc-meta {{
    font-family: var(--pixel); font-size: 13px; color: var(--pink);
  }}
  .an-sc-dna {{
    height: 14px; border: 1px solid var(--border);
    display: flex; overflow: hidden; width: 100%;
  }}
  .an-sc-dna-seg {{ height: 100%; }}
  .an-sc-empty {{
    display: flex; align-items: center; justify-content: center;
    border: 4px dashed var(--dim) !important;
    box-shadow: none !important;
  }}
  .an-sc-prompt {{
    font-family: var(--pixel); font-size: 12px; color: var(--dim);
    text-align: center; line-height: 2.5; letter-spacing: 1px;
  }}

  /* Verdict core */
  .an-verdict-core {{
    display: flex; flex-direction: column; align-items: center;
    justify-content: center; gap: 12px; padding: 20px 0;
  }}
  .an-verdict-sticker {{
    font-family: var(--pixel); font-size: 13px; letter-spacing: 2px;
    padding: 8px 14px; border: 2px solid var(--border);
    box-shadow: 3px 3px 0 0 #0A0A0A; transform: rotate(-3deg);
  }}
  .an-verdict-label {{
    font-family: var(--pixel); font-size: 12px; color: var(--dim);
  }}
  .an-verdict-wr {{
    font-family: var(--pixel); font-size: 64px; letter-spacing: 3px; line-height: 1;
  }}
  .an-verdict-r2 {{
    font-family: var(--pixel); font-size: 13px; color: var(--dim);
  }}
  .an-verdict-dots {{ display: flex; gap: 6px; }}
  .an-verdict-dots span {{
    width: 8px; height: 8px; background: var(--dim); display: inline-block;
    animation: shoppu-jump .6s ease-in-out infinite;
  }}

  /* Insight strip */
  .an-insight {{
    background: #0A0A0A; padding: 20px 32px;
    display: flex; align-items: center; gap: 28px; flex-wrap: wrap;
  }}
  .an-insight-lead {{
    font-family: var(--pixel); font-size: 12px; color: var(--gold); flex-shrink: 0;
  }}
  .an-insight-items {{
    display: flex; align-items: center; gap: 16px; flex-wrap: wrap;
  }}
  .an-insight-item {{
    font-family: var(--mono); font-size: 13px; color: #FBF3E4;
  }}
  .an-insight-item strong {{ color: var(--pink); font-weight: 700; }}
  .an-insight-div {{ color: var(--dim); }}

  /* Matchup navigator */
  .an-nav {{
    background: var(--panel); padding: 20px 32px;
    border-bottom: 2px solid var(--border);
    display: flex; align-items: center; gap: 24px;
  }}
  .an-nav-label {{
    border-right: 2px solid var(--border);
    padding-right: 24px; flex-shrink: 0;
  }}
  .an-nav-label .big {{ font-family: var(--font); font-size: 14px; font-weight: 700; }}
  .an-nav-label .small {{ font-family: var(--pixel); font-size: 12px; color: var(--dim); margin-top: 2px; }}
  .an-nav-pills {{
    flex: 1; display: flex; gap: 8px; overflow-x: auto;
    scrollbar-width: thin;
  }}
  .an-nav-pill {{
    display: flex; align-items: center; gap: 6px;
    padding: 8px 12px; border: 2px solid var(--border);
    box-shadow: 3px 3px 0 0 #0A0A0A; background: white; cursor: pointer;
    flex-shrink: 0; white-space: nowrap;
    transition: transform .07s, box-shadow .07s;
  }}
  .an-nav-pill:hover {{ transform: translate(-1px,-1px); box-shadow: 4px 4px 0 0 #0A0A0A; }}
  .an-nav-pill.active {{
    background: #0A0A0A; color: white;
    box-shadow: 1px 1px 0 0 #0A0A0A; transform: translate(2px,2px);
  }}
  .an-nav-pill img {{
    width: 24px; height: 24px; object-fit: cover; border: 1px solid var(--border);
    flex-shrink: 0;
  }}
  .an-nav-pill .pill-name {{ font-family: var(--font); font-size: 13px; }}
  .an-nav-pill .pill-wr {{ font-family: var(--pixel); font-size: 12px; }}
  .an-nav-right {{
    border-left: 2px solid var(--border);
    padding-left: 24px; flex-shrink: 0;
    font-family: var(--pixel); font-size: 13px;
    display: flex; flex-direction: column; gap: 4px;
  }}

  /* Workspace */
  .an-workspace {{
    display: flex; flex-direction: column;
    gap: 24px; padding: 40px 32px 0;
  }}
  .an-panel {{
    background: var(--card-bg); border: 4px solid var(--border);
    box-shadow: 4px 4px 0 0 #0A0A0A; padding: 24px;
  }}
  .an-panel-eyebrow {{
    font-family: var(--pixel); font-size: 12px; color: var(--pink); margin-bottom: 6px;
  }}
  .an-panel-title {{
    font-family: var(--font); font-size: 28px; font-weight: 700;
  }}
  .an-panel-subtitle {{
    font-family: var(--pixel); font-size: 12px; color: var(--dim);
    border-bottom: 2px solid var(--border); padding-bottom: 10px; margin-bottom: 18px;
    display: block; margin-top: 4px;
  }}

  /* Role chip rows */
  .an-role-row {{
    display: grid; grid-template-columns: 120px 1fr 80px 80px;
    padding: 10px 14px; border: 2px solid var(--border);
    box-shadow: 3px 3px 0 0 #0A0A0A; background: white;
    margin-bottom: 8px; cursor: pointer;
    transition: border-color .1s;
  }}
  .an-role-row:hover {{ border-color: var(--pink); }}
  .an-role-row.active {{ border-color: var(--pink); box-shadow: 3px 3px 0 0 var(--pink); background: #FFE4EE; }}
  .an-role-badge {{
    font-family: var(--pixel); font-size: 12px; padding: 4px 8px;
    border: 1px solid rgba(0,0,0,.2); color: #fff; display: inline-block;
    align-self: center;
  }}
  .an-role-badge.role-tech {{ color: var(--text); }}
  .an-role-desc {{
    font-family: var(--mono); font-size: 12px; color: var(--dim);
    margin-top: 5px; line-height: 1.4;
  }}
  .an-comp-bar-wrap {{
    background: var(--panel); height: 8px; border: 1px solid var(--border);
    align-self: center;
  }}
  .an-comp-bar-fill {{ height: 100%; transition: width .4s ease; }}
  .an-comp-bar-label {{
    font-family: var(--pixel); font-size: 12px; color: var(--dim); margin-top: 4px;
  }}
  .an-role-attr {{
    font-family: var(--pixel); font-size: 13px; text-align: right; align-self: center;
  }}
  .an-role-attr-label {{
    font-family: var(--pixel); font-size: 12px; color: var(--dim); text-align: right; align-self: center; line-height: 1.6;
  }}

  /* Card list */
  .an-card-list-header {{
    display: flex; align-items: center; gap: 10px;
    border-top: 2px solid var(--border); padding-top: 18px; margin-top: 18px;
    margin-bottom: 12px;
  }}
  .an-card-list-title {{ font-family: var(--pixel); font-size: 12px; flex: 1; }}
  .an-filter-badge {{
    font-family: var(--pixel); font-size: 12px; color: var(--pink);
    border: 1px solid var(--pink); padding: 2px 6px;
  }}
  .an-clear-btn {{
    font-family: var(--pixel); font-size: 12px; color: var(--dim);
    background: none; border: 1px solid var(--border); cursor: pointer; padding: 4px 8px;
  }}
  .an-acq-item {{
    display: flex; align-items: center; gap: 14px;
    padding: 10px; border: 2px solid var(--border); background: white;
    margin-bottom: 6px;
  }}
  .an-acq-thumb {{
    width: 40px; height: 56px; background: var(--bg-deep);
    border: 1px solid var(--border); flex-shrink: 0; object-fit: cover;
  }}
  .an-acq-name {{ font-family: var(--font); font-size: 16px; flex: 1; }}
  .an-acq-count {{ font-family: var(--pixel); font-size: 14px; color: var(--pink); }}
  .an-acq-empty {{
    border: 2px dashed var(--border); padding: 20px;
    font-family: var(--pixel); font-size: 12px; color: var(--dim);
    text-align: center; line-height: 2.5;
  }}

  /* Why panel — two-column internal layout */
  .an-why-body {{
    display: grid; grid-template-columns: 1fr 1.8fr;
    gap: 16px; margin-top: 4px;
  }}
  .an-callout {{
    background: white; border: 2px solid var(--border);
    padding: 16px; font-family: var(--mono); font-size: 14px;
    display: flex; flex-direction: column; gap: 10px;
  }}
  .an-callout-title {{
    font-family: var(--pixel); font-size: 12px; color: var(--dim);
    border-bottom: 1px solid var(--border); padding-bottom: 8px;
  }}
  .an-callout .arrow {{ color: var(--pink); }}
  .an-diverg {{
    background: white; border: 2px solid var(--border); padding: 16px;
  }}
  .an-diverg-header {{
    display: grid; grid-template-columns: 90px 1fr 1fr 64px;
    gap: 8px; margin-bottom: 10px;
  }}
  .an-diverg-header span {{
    font-family: var(--pixel); font-size: 12px; color: var(--dim);
  }}
  .an-diverg-row {{
    display: grid; grid-template-columns: 90px 1fr 1fr 64px;
    gap: 8px; align-items: center; margin-bottom: 8px;
  }}
  .an-diverg-bar-col {{
    display: flex; align-items: center; gap: 6px;
  }}
  .an-diverg-bar-wrap {{
    flex: 1; height: 20px; background: var(--bg-deep);
    border: 1px solid var(--border); overflow: hidden; position: relative;
  }}
  .an-diverg-bar-fill {{
    height: 100%; min-width: 2px; transition: width .4s ease;
  }}
  .an-diverg-pct {{
    font-family: var(--pixel); font-size: 12px; color: var(--dim);
    white-space: nowrap; min-width: 28px; text-align: right;
  }}
  .an-diverg-delta {{
    font-family: var(--pixel); font-size: 12px; text-align: right;
  }}
  /* Cards to acquire grid */
  .an-acq-grid {{
    display: grid; grid-template-columns: 1fr 1fr; gap: 8px;
  }}

  /* Field band */
  .an-field {{
    padding: 48px 32px 96px; border-top: 2px solid var(--border);
    margin-top: 40px;
  }}
  .an-field-eyebrow {{ font-family: var(--pixel); font-size: 13px; color: var(--pink); }}
  .an-field-title {{ font-family: var(--font); font-size: 28px; font-weight: 700; margin: 6px 0 4px; }}
  .an-field-subtitle {{ font-family: var(--pixel); font-size: 12px; color: var(--dim); margin-bottom: 24px; display: block; }}
  .an-tier-row {{
    display: flex; gap: 18px; padding: 18px;
    border: 4px solid var(--border); box-shadow: 4px 4px 0 0 #0A0A0A;
    background: var(--card-bg); margin-bottom: 16px;
    align-items: flex-start;
  }}
  .an-tier-badge {{
    width: 64px; height: 64px; display: flex; flex-direction: column;
    align-items: center; justify-content: center; flex-shrink: 0;
    border: 2px solid white;
  }}
  .an-tier-badge .tier-letter {{ font-family: var(--font); font-size: 32px; font-weight: 700; line-height: 1; }}
  .an-tier-badge .tier-sub {{ font-family: var(--pixel); font-size: 12px; }}
  .an-tier-decks {{ display: flex; flex-wrap: wrap; gap: 12px; flex: 1; }}
  .an-tier-deck {{
    width: 110px; background: var(--bg-deep); border: 2px solid var(--border);
    cursor: pointer; overflow: hidden;
    transition: border-color .1s, box-shadow .1s;
  }}
  .an-tier-deck:hover {{ border-color: var(--pink); }}
  .an-tier-deck.active {{ border: 3px solid var(--pink); box-shadow: 3px 3px 0 0 var(--pink); }}
  .an-tier-deck .td-img {{
    width: 100%; height: 64px; object-fit: cover; display: block;
  }}
  .an-tier-deck .td-body {{ padding: 6px 8px; }}
  .an-tier-deck .td-name {{ font-family: var(--font); font-size: 13px; margin-bottom: 4px; line-height: 1.2; }}
  .an-tier-deck .td-wr {{ font-family: var(--pixel); font-size: 12px; }}
  .an-tier-deck .td-meta {{ font-family: var(--pixel); font-size: 12px; color: var(--dim); }}

  /* Deck picker overlay */
  #an-picker-overlay {{
    display: none; position: fixed; inset: 0;
    background: rgba(10,10,10,.55); z-index: 1000;
    align-items: center; justify-content: center;
  }}
  #an-picker-overlay.open {{ display: flex; }}
  #an-picker-modal {{
    max-width: 880px; width: 96vw;
    background: var(--card-bg); border: 4px solid var(--border);
    box-shadow: 8px 8px 0 0 #0A0A0A; padding: 32px; max-height: 90vh; overflow-y: auto;
  }}
  .an-picker-header {{
    display: flex; align-items: center; justify-content: space-between;
    margin-bottom: 20px; padding-bottom: 16px; border-bottom: 2px solid var(--border);
  }}
  .an-picker-header h2 {{ font-family: var(--font); font-size: 24px; font-weight: 700; }}
  .an-picker-sub {{ font-family: var(--pixel); font-size: 12px; color: var(--dim); margin-top: 4px; }}
  .an-picker-close {{
    background: var(--border); border: 2px solid var(--border); color: white;
    font-family: var(--pixel); font-size: 12px; padding: 8px 14px; cursor: pointer;
  }}
  .an-picker-grid {{
    display: grid; grid-template-columns: repeat(5,1fr); gap: 14px;
  }}
  .an-picker-card {{
    background: var(--bg-deep); border: 4px solid var(--border);
    box-shadow: var(--shadow); padding: 14px;
    display: flex; flex-direction: column; align-items: center; gap: 8px;
    cursor: pointer; transition: border-color .1s;
  }}
  .an-picker-card:hover {{ border-color: var(--pink); }}
  .an-picker-card.selected {{ border-color: var(--pink); box-shadow: 4px 4px 0 0 var(--pink); }}
  /* mini 3-card fan inside each picker card */
  .an-picker-hand {{
    width: 100%; height: 90px;
    display: flex; align-items: flex-end; justify-content: center;
    overflow: hidden; position: relative;
  }}
  .an-picker-hcard {{
    width: 52px; height: 73px; flex-shrink: 0;
    border: 1px solid var(--border); overflow: hidden; background: var(--panel);
  }}
  .an-picker-hcard img {{ width: 100%; height: 100%; object-fit: cover; display: block; }}
  .an-picker-hcard.hc-left  {{ transform: rotate(-6deg) translateY(8px); margin-right: -8px; z-index: 1; }}
  .an-picker-hcard.hc-mid   {{ z-index: 3; }}
  .an-picker-hcard.hc-right {{ transform: rotate(6deg) translateY(8px); margin-left: -8px; z-index: 1; }}
  .an-picker-name {{ font-family: var(--font); font-size: 13px; text-align: center; line-height: 1.3; }}
  .an-picker-meta {{ font-family: var(--pixel); font-size: 12px; color: var(--pink); }}

  /* ── CATALOG tab ── */
  #catalog-filters {{
    display: flex; flex-wrap: wrap; gap: 10px; align-items: center;
    margin-bottom: 20px; padding-bottom: 20px; border-bottom: 4px solid var(--border);
  }}
  #cat-search {{
    background: var(--card-bg); border: 2px solid var(--border); color: var(--text);
    font-family: var(--mono); font-size: 14px; padding: 10px 14px;
    flex: 1; min-width: 160px; outline: none;
    box-shadow: var(--shadow);
  }}
  #cat-search:focus {{ border-color: var(--pink); outline: 2px solid var(--pink); }}
  #cat-search::placeholder {{ color: var(--dim); }}
  #cat-set {{
    background: var(--card-bg); border: 2px solid var(--border); color: var(--text);
    font-family: var(--mono); font-size: 13px; padding: 10px 10px; cursor: pointer; outline: none;
    box-shadow: var(--shadow);
  }}
  /* ShoppuChip filter buttons */
  .cat-type-btn {{
    background: var(--card-bg); border: 2px solid var(--border); color: var(--text);
    font-family: var(--font); font-size: 14px; padding: 10px 18px; cursor: pointer;
    box-shadow: 4px 4px 0 0 var(--border);
    transition: box-shadow .07s, transform .07s;
  }}
  .cat-type-btn:hover {{ box-shadow: 2px 2px 0 0 var(--border); transform: translate(2px,2px); }}
  .cat-type-btn.active {{ background: var(--border); color: var(--bg); box-shadow: 2px 2px 0 0 var(--border); transform: translate(2px,2px); }}
  #cat-count {{ font-family: var(--font); font-size: 12px; color: var(--dim); margin-bottom: 16px; }}
  #catalog-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(148px, 1fr)); gap: 16px; }}
  #cat-load-more {{
    display: block; margin: 28px auto 0;
    background: var(--border); border: 4px solid var(--border); color: var(--bg);
    font-family: var(--font); font-size: 13px; padding: 14px 32px; cursor: pointer;
    letter-spacing: 1px; box-shadow: var(--shadow);
    transition: box-shadow .07s, transform .07s;
  }}
  #cat-load-more:hover {{ box-shadow: none; transform: translate(4px,4px); }}
  #cat-load-more:disabled {{ display: none; }}

  /* ── NEW DECK button ── */
  #new-deck-btn {{
    width: 100%; margin-bottom: 4px; display: block;
    background: transparent; border: 2px dashed var(--border); color: var(--dim);
    font-family: var(--font); font-size: 12px; padding: 14px 0; cursor: pointer;
    letter-spacing: 1px; text-align: center;
    transition: border-color .12s, color .12s, background .12s;
  }}
  #new-deck-btn:hover {{ border-color: var(--pink); color: var(--pink); background: rgba(230,52,98,.04); border-style: solid; }}

  /* ── New Deck modal ── */
  #nd-overlay {{
    display: none; position: fixed; inset: 0;
    background: rgba(0,0,0,.65); z-index: 8000;
    align-items: center; justify-content: center;
  }}
  #nd-overlay.open {{ display: flex; }}
  #nd-modal {{
    background: var(--bg); border: 4px solid var(--border);
    box-shadow: var(--shadow-lg);
    width: min(780px, 96vw); max-height: 88vh;
    display: flex; flex-direction: column; overflow: hidden;
  }}
  #nd-header {{
    display: flex; align-items: center; justify-content: space-between;
    padding: 16px 20px; border-bottom: 4px solid var(--border);
    font-family: var(--font); font-size: 14px; color: var(--bg);
    background: var(--border);
  }}
  #nd-close {{
    background: none; border: 2px solid var(--bg); color: var(--bg); font-family: var(--font);
    font-size: 12px; cursor: pointer; padding: 6px 12px;
    transition: background .12s, color .12s;
  }}
  #nd-close:hover {{ background: var(--bg); color: var(--border); }}
  #nd-name-row {{
    padding: 12px 18px; border-bottom: 2px solid var(--border);
    display: flex; align-items: center; gap: 10px; background: var(--panel);
  }}
  #nd-name-row label {{ font-family: var(--font); font-size: 12px; color: var(--dim); white-space: nowrap; }}
  #nd-name {{
    flex: 1; background: var(--card-bg); border: 2px solid var(--border);
    color: var(--text); font-family: var(--mono); font-size: 13px; padding: 8px 10px; outline: none;
    box-shadow: var(--shadow-sm);
  }}
  #nd-name:focus {{ border-color: var(--pink); outline: 2px solid var(--pink); }}
  #nd-body {{ display: grid; grid-template-columns: 1fr 1fr; flex: 1; overflow: hidden; min-height: 0; }}
  #nd-search-panel {{
    border-right: 2px solid var(--border);
    display: flex; flex-direction: column; overflow: hidden; background: var(--bg);
  }}
  #nd-search-wrap {{ padding: 10px 12px; border-bottom: 2px solid var(--border); display: flex; gap: 6px; }}
  #nd-search {{
    flex: 1; background: var(--card-bg); border: 2px solid var(--border);
    color: var(--text); font-family: var(--mono); font-size: 13px; padding: 7px 10px; outline: none;
    box-shadow: var(--shadow-sm);
  }}
  #nd-search:focus {{ border-color: var(--pink); outline: 2px solid var(--pink); }}
  #nd-results {{ overflow-y: auto; flex: 1; padding: 6px; }}
  .nd-result {{
    display: flex; align-items: center; gap: 8px;
    padding: 8px; border: 2px solid transparent; cursor: pointer; transition: background .1s;
  }}
  .nd-result:hover {{ background: var(--panel); border-color: var(--border); }}
  .nd-result-img {{ width: 30px; height: 42px; object-fit: contain; flex-shrink: 0; border: 1px solid #ccc; }}
  .nd-result-info {{ flex: 1; min-width: 0; }}
  .nd-result-name {{ font-family: var(--mono); font-size: 12px; color: var(--text); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
  .nd-result-sub  {{ font-family: var(--mono); font-size: 13px; color: var(--dim); margin-top: 2px; }}
  .nd-add-btn {{
    background: var(--border); border: 2px solid var(--border); color: var(--bg);
    font-family: var(--font); font-size: 12px; padding: 6px 10px; cursor: pointer;
    flex-shrink: 0; box-shadow: var(--shadow-sm);
    transition: box-shadow .07s, transform .07s;
  }}
  .nd-add-btn:hover {{ box-shadow: none; transform: translate(4px,4px); }}
  #nd-draft-panel {{ display: flex; flex-direction: column; overflow: hidden; background: var(--bg); }}
  #nd-draft-header {{
    padding: 10px 12px; border-bottom: 2px solid var(--border);
    font-family: var(--font); font-size: 12px; color: var(--dim); display: flex; justify-content: space-between;
    background: var(--panel);
  }}
  #nd-draft-count {{ color: var(--text); }}
  #nd-draft-list {{ overflow-y: auto; flex: 1; padding: 6px; }}
  .nd-draft-item {{ display: flex; align-items: center; gap: 6px; padding: 6px; border-bottom: 2px solid var(--panel); }}
  .nd-draft-name {{ flex: 1; font-family: var(--mono); font-size: 12px; color: var(--text); min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
  .nd-draft-cnt  {{ font-family: var(--font); font-size: 12px; color: var(--text); min-width: 16px; text-align: center; }}
  .nd-draft-btn  {{
    background: var(--border); border: 2px solid var(--border); color: var(--bg);
    font-family: var(--font); font-size: 12px; width: 24px; height: 24px;
    cursor: pointer; padding: 0; line-height: 1;
    box-shadow: var(--shadow-sm); transition: box-shadow .07s, transform .07s; flex-shrink: 0;
  }}
  .nd-draft-btn:hover {{ box-shadow: none; transform: translate(4px,4px); }}
  #nd-footer {{
    padding: 14px 20px; border-top: 4px solid var(--border);
    display: flex; gap: 10px; justify-content: flex-end; align-items: center;
    background: var(--panel);
  }}
  #nd-err {{ font-family: var(--font); font-size: 12px; color: var(--red); flex: 1; }}
  #nd-save-btn {{
    background: var(--pink); border: 2px solid var(--border); color: #fff;
    font-family: var(--font); font-size: 12px; padding: 12px 24px; cursor: pointer;
    letter-spacing: 1px; box-shadow: var(--shadow);
    transition: box-shadow .07s, transform .07s;
  }}
  #nd-save-btn:hover {{ box-shadow: none; transform: translate(4px,4px); }}

  /* ── Role CSS classes ── */
  .role-win_condition {{ background: var(--pink); }}
  .role-engine        {{ background: var(--blue); }}
  .role-staple        {{ background: var(--green); }}
  .role-tech          {{ background: var(--gold); color: var(--text); }}
  .role-garnet        {{ background: #FF6B35; }}

  /* ── Boot ── */
  #boot {{
    position: fixed; inset: 0; background: var(--bg);
    display: flex; flex-direction: column; align-items: center; justify-content: center;
    z-index: 9000; gap: 16px;
  }}
  #boot-title {{
    font-family: var(--font); font-size: 56px; font-weight: 700;
    color: var(--text); text-align: center; line-height: 1;
    letter-spacing: -1px;
  }}
  #boot-sub {{
    font-family: var(--pixel); font-size: 12px; color: var(--pink);
    text-align: center; margin-top: -4px;
  }}
  .boot-line {{ font-family: var(--pixel); font-size: 12px; color: var(--dim); margin-top: 4px; }}
  #boot-bar-wrap {{
    width: 320px; height: 24px; border: 4px solid var(--border);
    background: var(--panel); padding: 4px; margin-top: 4px;
  }}
  #boot-bar {{ height: 100%; width: 0%; background: var(--border); transition: width .05s linear; }}

  /* Tooltip for aggregate toggle */
  .agg-tooltip {{
    position: relative; display: inline-block; margin-left: 6px; cursor: help; color: var(--dim);
    font-weight: 600; width: 18px; height: 18px; line-height: 18px; text-align: center; border-radius: 3px;
    background: rgba(0,0,0,0.04);
  }}
  .agg-tooltip .agg-tooltip-text {{
    visibility: hidden; opacity: 0; width: 260px; background-color: #111; color: #fff; text-align: left;
    border-radius: 6px; padding: 8px; position: absolute; z-index: 1000; bottom: 125%; left: 50%; transform: translateX(-50%);
    transition: opacity 0.15s ease, visibility 0.15s ease; font-family: var(--mono); font-size: 12px; line-height: 1.2;
  }}
  .agg-tooltip .agg-tooltip-text::after {{
    content: ""; position: absolute; top: 100%; left: 50%; transform: translateX(-50%); border-width: 6px; border-style: solid;
    border-color: #111 transparent transparent transparent;
  }}
  .agg-tooltip:hover .agg-tooltip-text {{ visibility: visible; opacity: 1; }}

  #card-zoom-overlay {{
    display: none; position: fixed; inset: 0; z-index: 9999;
    background: rgba(0,0,0,0.85); align-items: center; justify-content: center;
    cursor: zoom-out;
  }}
  #card-zoom-overlay.active {{ display: flex; }}
  #card-zoom-overlay img {{
    max-width: min(420px, 90vw); max-height: 90vh; object-fit: contain;
    border-radius: 12px; box-shadow: 0 8px 48px rgba(0,0,0,0.8);
    pointer-events: none;
  }}
  .card-img {{ cursor: zoom-in; }}

  /* ── Mobile responsive ── */
  @media (max-width: 768px) {{
    /* Nav — logo row on top, tabs fixed to bottom of screen */
    #top-nav {{ height: 52px; flex-wrap: nowrap; }}
    #nav-logo {{ min-width: unset; padding: 0 12px; border-right: none; gap: 8px; flex: 1; }}
    #nav-logo-name {{ font-size: 14px; }}
    #nav-logo-sub {{ display: none; }}
    #nav-links {{
      position: fixed; bottom: 0; left: 0; right: 0; z-index: 300;
      background: var(--bg); border-top: 4px solid var(--border);
      height: 60px; overflow: visible; width: 100%;
    }}
    .tab-btn {{
      flex: 1; min-width: unset; padding: 0;
      height: 60px; border-right: 2px solid var(--border);
    }}
    .tab-btn:last-child {{ border-right: none; }}
    .tab-btn .nav-en {{ font-size: 13px; }}
    .tab-btn .nav-jp {{ font-size: 6px; }}
    #nav-right {{ padding: 0 10px; gap: 8px; border-left: 2px solid var(--border); flex-shrink: 0; }}
    #total-label {{ display: none; }}
    #save-btn {{ font-size: 12px; padding: 8px 12px; }}
    /* Push scrollable content above the fixed bottom tab bar */
    .tab-pane {{ padding-bottom: 80px !important; }}
    #card-area {{ padding-bottom: 80px; }}

    /* Marquee */
    #marquee-strip {{ font-size: 13px; }}

    /* Tab content */
    .tab-pane {{ padding: 20px 14px 40px; }}

    /* Page headers */
    .page-header h1 {{ font-size: 32px; }}
    .page-header-jp {{ font-size: 12px; }}

    /* Meta grid — 2 columns */
    .meta-grid {{ grid-template-columns: repeat(2, 1fr); gap: 12px; }}
    .arch-img-area {{ height: 160px; }}

    /* Collection — stack vertically */
    #collection-pane {{ grid-template-columns: 1fr; grid-template-rows: 260px 1fr; }}
    #deck-list {{ border-right: none; border-bottom: 4px solid var(--border); overflow-y: auto; padding: 10px 8px 16px; }}
    #card-area {{ padding: 16px 12px 80px; }}
    #card-grid {{ grid-template-columns: repeat(2, 1fr); gap: 10px; }}

    /* Analysis header */
    .an-header {{ padding: 24px 16px 16px; }}
    .an-headline {{ font-size: 28px; }}
    .an-subtitle {{ font-size: 12px; }}

    /* Analysis scoreboard — stack vertically */
    .an-scoreboard {{ grid-template-columns: 1fr; gap: 16px; padding: 20px 16px; }}
    .an-sc-body {{ height: 200px; }}
    .an-score-card-wrap.right {{ align-items: flex-start; }}
    .an-verdict-wr {{ font-size: 40px; }}

    /* Analysis nav */
    .an-nav {{ padding: 12px 16px; flex-wrap: wrap; gap: 10px; }}
    .an-nav-label {{ border-right: none; padding-right: 0; border-bottom: 2px solid var(--border); padding-bottom: 8px; width: 100%; }}

    /* Analysis workspace */
    .an-workspace {{ padding: 16px 14px 0; gap: 16px; }}
    .an-panel {{ padding: 16px; }}
    .an-why-body {{ grid-template-columns: 1fr; }}
    .an-role-row {{ grid-template-columns: 80px 1fr 56px 56px; padding: 8px 10px; }}
    .an-diverg-header {{ grid-template-columns: 70px 1fr 1fr 48px; }}
    .an-diverg-row {{ grid-template-columns: 70px 1fr 1fr 48px; }}
    .an-acq-grid {{ grid-template-columns: 1fr; }}
    .an-field {{ padding: 24px 14px 48px; }}
    .an-tier-deck {{ width: 90px; }}

    /* Analysis picker */
    .an-picker-grid {{ grid-template-columns: repeat(2, 1fr); gap: 10px; }}
    #an-picker-modal {{ padding: 16px; max-height: 85vh; }}

    /* New Deck modal */
    #nd-modal {{ width: 98vw; max-height: 94vh; }}
    #nd-body {{ grid-template-columns: 1fr; grid-template-rows: 1fr 1fr; }}
    #nd-search-panel {{ border-right: none; border-bottom: 2px solid var(--border); }}

    /* Catalog */
    #catalog-grid {{ grid-template-columns: repeat(auto-fill, minmax(120px, 1fr)); gap: 10px; }}
    #catalog-filters {{ gap: 8px; }}

    /* Section labels */
    .section-label h2 {{ font-size: 20px; }}
  }}

  @media (max-width: 480px) {{
    .meta-grid {{ grid-template-columns: repeat(2, 1fr); gap: 8px; }}
    #card-grid {{ grid-template-columns: repeat(2, 1fr); gap: 8px; }}
    .tab-btn {{ min-width: 68px; padding: 0 6px; }}
    .tab-btn .nav-en {{ font-size: 13px; }}
    .page-header h1 {{ font-size: 26px; }}
    .an-headline {{ font-size: 22px; }}
    .an-scoreboard {{ padding: 14px 12px; }}
    .an-sc-body {{ height: 160px; }}
    .an-verdict-wr {{ font-size: 32px; }}
    .an-picker-grid {{ grid-template-columns: repeat(2, 1fr); }}
    #catalog-grid {{ grid-template-columns: repeat(auto-fill, minmax(100px, 1fr)); }}
    .tab-pane {{ padding: 14px 10px 32px; }}
    #card-area {{ padding: 12px 8px; }}
  }}

</style>
<script data-goatcounter="https://deyosa.goatcounter.com/count" async src="//gc.zgo.at/count.js"></script>
<script async src="https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js?client=ca-pub-1375181015653535" crossorigin="anonymous"></script>
</head>
<body>

<!-- Boot screen -->
<div id="boot">
  <svg width="80" height="80" viewBox="0 0 80 80" fill="none" xmlns="http://www.w3.org/2000/svg">
    <circle cx="40" cy="40" r="36" fill="white" stroke="#0A0A0A" stroke-width="4"/>
    <path d="M4 40 h72" stroke="#0A0A0A" stroke-width="4"/>
    <path d="M4 40 A36 36 0 0 1 76 40" fill="#E63462"/>
    <circle cx="40" cy="40" r="12" fill="white" stroke="#0A0A0A" stroke-width="4"/>
    <circle cx="40" cy="40" r="5" fill="#0A0A0A"/>
  </svg>
  <div id="boot-title">PKMN.POCKET</div>
  <div id="boot-sub">META ANALYZER</div>
  <div class="boot-line">Loading card database...</div>
  <div id="boot-bar-wrap"><div id="boot-bar"></div></div>
</div>

<!-- Main UI -->
<div id="main-ui" style="display:none;">
  <nav id="top-nav">
    <div id="nav-logo">
      <svg width="40" height="40" viewBox="0 0 40 40" fill="none" xmlns="http://www.w3.org/2000/svg">
        <circle cx="20" cy="20" r="18" fill="white" stroke="#0A0A0A" stroke-width="3"/>
        <path d="M2 20 h36" stroke="#0A0A0A" stroke-width="3"/>
        <path d="M2 20 A18 18 0 0 1 38 20" fill="#E63462"/>
        <circle cx="20" cy="20" r="6" fill="white" stroke="#0A0A0A" stroke-width="3"/>
        <circle cx="20" cy="20" r="2.5" fill="#0A0A0A"/>
      </svg>
      <div>
        <div id="nav-logo-name">PKMN.POCKET</div>
        <div id="nav-logo-sub">META ANALYZER</div>
      </div>
    </div>
    <div id="nav-links">
      <button class="tab-btn active" onclick="showTab('meta')">Meta</button>
      <button class="tab-btn" onclick="showTab('collection')">Collection</button>
      <button class="tab-btn" onclick="showTab('catalog')">Catalog</button>
      <button class="tab-btn" onclick="showTab('analysis')">Analysis</button>
    </div>
    <div id="nav-right">
      <span id="total-label">♥ <span id="total-count">0</span></span>
      <button id="dark-btn" onclick="toggleDark()" title="Toggle dark mode">🌙</button>
      <button id="kofi-btn" onclick="toggleSupport()" title="Support the site">☕</button>
      <button id="save-btn" onclick="saveCollection()">SAVE</button>
    </div>
  </nav>


  <!-- Support banner -->
  <div id="support-banner">
    <span>☕ Enjoying this free tool?</span>
    <a class="sb-link" href="https://ko-fi.com/deyosa" target="_blank" rel="noopener">DONATE ON KO-FI</a>
    <a class="sb-link sb-dark" href="https://www.amazon.com/s?k=pokemon+tcg+pocket+booster+pack&tag=pocketmeta00-20" target="_blank" rel="noopener sponsored">🛒 BUY POKEMON CARDS</a>
  </div>

  <div id="content">
    <!-- META -->
    <div class="tab-pane active" id="meta-pane">
      <div class="page-header">
        <h1>META</h1>
        <div class="page-subtitle">
          Top archetypes ranked by expected win rate · Data from Limitless TCG
          <br><span class="ps-updated" id="meta-updated" data-built="{built_at}">⏱ Updated ...</span>
          <br><span class="ps-hint">💡 Hover over a deck card to see its full card list</span>
        </div>
      </div>
      <div id="meta-search-wrap">
        <input id="meta-search-input" type="text" placeholder="SEARCH DECKS..." oninput="filterMeta()">
      </div>
      <div class="meta-grid" id="meta-grid"></div>
    </div>

    <!-- COLLECTION -->
    <div class="tab-pane" id="collection-pane">
      <div id="deck-list">
        <div id="deck-list-hint">Track owned cards · Build decks · Press SAVE to persist</div>
        <button id="new-deck-btn" onclick="openNewDeck()">➕ NEW DECK</button>
      </div>
      <div id="card-area">
        <div id="collection-empty">
          <div id="collection-empty-icon">◄</div>
          <div id="collection-empty-text">Select a deck from the list<br>to view its cards</div>
        </div>
        <div id="deck-title-row" style="display:none">
          <div id="deck-title">◄ SELECT A DECK ►</div>
          <button id="clear-deck-btn" onclick="clearDeck()" disabled>🗑 CLEAR</button>
          <button id="share-deck-btn" onclick="shareDeck()" disabled>📋 COPY</button>
        </div>
        <div id="card-grid" style="display:none"></div>
      </div>
    </div>

    <!-- CATALOG -->
    <div class="tab-pane" id="catalog-pane">
      <div class="page-header">
        <h1>CATALOG</h1>
        <div class="page-subtitle">Browse all Pokemon TCG Pocket cards · Mark owned copies to update your collection</div>
      </div>
      <div id="catalog-filters">
        <input id="cat-search" type="text" placeholder="SEARCH BY NAME..."
               oninput="filterCatalog()">
        <select id="cat-set" onchange="filterCatalog()">
          <option value="">ALL SETS</option>
        </select>
        <button class="cat-type-btn active" onclick="setCatType('')">ALL</button>
        <button class="cat-type-btn" onclick="setCatType('Pokemon')">POKEMON</button>
        <button class="cat-type-btn" onclick="setCatType('Trainer')">TRAINER</button>
        <button class="cat-type-btn" onclick="setCatType('Energy')">ENERGY</button>
      </div>
      <div id="cat-count"></div>
      <div id="catalog-grid"></div>
      <button id="cat-load-more" onclick="loadMoreCatalog()">▼ LOAD MORE</button>
    </div>

    <!-- ANALYSIS (Shoppu Fighter layout) -->
    <div class="tab-pane" id="analysis-pane">
      <div id="an-root"></div>
    </div>
  </div>
</div>

<!-- New Deck Modal -->
<div id="nd-overlay">
  <div id="nd-modal">
    <div id="nd-header">
      ➕ CREATE NEW DECK
      <button id="nd-close" onclick="closeNewDeck()">✕ CANCEL</button>
    </div>
    <div id="nd-name-row">
      <label>DECK NAME:</label>
      <input id="nd-name" type="text" placeholder="e.g. MY CHARIZARD DECK" maxlength="40">
    </div>
    <div id="nd-body">
      <div id="nd-search-panel">
        <div id="nd-search-wrap">
          <input id="nd-search" type="text" placeholder="SEARCH CARDS..."
                 oninput="ndSearch()">
        </div>
        <div id="nd-results"></div>
      </div>
      <div id="nd-draft-panel">
        <div id="nd-draft-header">
          DECK PREVIEW
          <span id="nd-draft-count">0 / 20 CARDS</span>
        </div>
        <div id="nd-draft-list"></div>
      </div>
    </div>
    <div id="nd-footer">
      <span id="nd-err"></span>
      <button id="nd-save-btn" onclick="saveDraft()">💾 SAVE DECK</button>
    </div>
  </div>
</div>

<!-- Analysis Deck Picker Overlay -->
<div id="an-picker-overlay" onclick="anClosePicker(event)">
  <div id="an-picker-modal">
    <div class="an-picker-header">
      <div>
        <h2>PICK YOUR FIGHTER</h2>
        <div class="an-picker-sub" id="an-picker-sub">YOUR DECK</div>
      </div>
      <button class="an-picker-close" onclick="anClosePicker(null)">✕ CLOSE</button>
    </div>
    <div class="an-picker-grid" id="an-picker-grid"></div>
  </div>
</div>

<div id="status-msg"></div>

<script>
const ARCHETYPES    = {decks_json};
const META_DATA     = {meta_json};
let   ANALYSIS_DATA = {analysis_json};
const CATALOG_DATA  = {catalog_json};
let   MATCHUP_DATA  = {matchup_json};
const ROLE_MAP      = {role_map_json};
let   REGRESSION    = {regression_json};
let   collection    = JSON.parse(localStorage.getItem('pkmn_collection') || '{{}}');
let   activeDeckIdx = -1;
let   activeTab     = 'meta';
let   aggregateByName = (localStorage.getItem('aggByName') === '1');

// Load custom decks from localStorage and merge into ARCHETYPES
(function() {{
  const saved = JSON.parse(localStorage.getItem('pkmn_custom_decks') || '[]');
  saved.forEach(cdeck => {{
    ARCHETYPES.push({{
      id: cdeck.id, name: cdeck.name,
      meta_share: 0, win_rate: 0, custom: true,
      cards: cdeck.cards.map(c => {{
        const cat = CATALOG_DATA.find(x => x.id === c.id);
        return {{
          id: c.id,
          name: cat ? cat.name : c.name || c.id,
          type: cat ? cat.type : 'Pokemon',
          role: ROLE_MAP[c.id] || 'garnet',
          need: c.count, have: collection[c.id] || 0,
          img: cat ? cat.img : '',
        }};
      }}),
    }});
  }});
}})();

// Catalog state
let catFiltered = [];
let catPage     = 0;
let catType     = '';
const CAT_PAGE_SIZE = 60;

const TYPE_SPRITE = {{ "Pokemon":"🎮","Trainer":"🃏","Energy":"⚡" }};
const TYPE_COLOR  = {{ "Pokemon":"type-Pokemon","Trainer":"type-Trainer","Energy":"type-Energy" }};

function toggleDark() {{
  const isDark = document.body.classList.toggle('dark');
  document.getElementById('dark-btn').textContent = isDark ? '☀' : '🌙';
  localStorage.setItem('darkMode', isDark ? '1' : '0');
}}
if (localStorage.getItem('darkMode') === '1') {{
  document.body.classList.add('dark');
  const _darkBtn = document.getElementById('dark-btn');
  if (_darkBtn) _darkBtn.textContent = '☀';
}}

// ── Tab system ──────────────────────────────────────────────────────────────
function initUpdatedLabel() {{
  const el = document.getElementById('meta-updated');
  if (!el) return;
  const built = new Date(el.dataset.built);
  const mins = Math.round((Date.now() - built) / 60000);
  if (mins < 60)       el.textContent = `⏱ Updated ${{mins}}m ago`;
  else if (mins < 1440) el.textContent = `⏱ Updated ${{Math.floor(mins/60)}}h ago`;
  else                  el.textContent = `⏱ Updated ${{Math.floor(mins/1440)}}d ago`;
}}

function showTab(name) {{
  document.querySelectorAll('.tab-pane').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  document.getElementById(name + '-pane').classList.add('active');
  event.currentTarget.classList.add('active');
  activeTab = name;
  if (name === 'meta')       renderMeta();
  if (name === 'collection') renderDeckList();
  if (name === 'catalog')    initCatalog();
  if (name === 'analysis')   renderAnalysis();
  setStatus('', '');
}}

function anToggleAggregate() {{
  aggregateByName = document.getElementById('agg-toggle').checked;
  localStorage.setItem('aggByName', aggregateByName ? '1' : '0');
  renderAnalysis();
}}

// ── META tab ────────────────────────────────────────────────────────────────
function renderMeta() {{
  const grid = document.getElementById('meta-grid');
  grid.innerHTML = '';
  META_DATA.forEach((arch, i) => {{
    const ewrCls  = arch.ewr >= 52 ? 'wr-hi' : arch.ewr < 48 ? 'wr-lo' : '';
    const imgs = arch.hero_imgs && arch.hero_imgs.length ? arch.hero_imgs : (arch.hero_img ? [arch.hero_img] : []);
    const imgHtml = imgs.length
      ? imgs.map(u => `<img src="${{u}}" alt="${{arch.name}}" onerror="this.style.display='none'">`).join('')
      : `<span style="font-size:56px">🃏</span>`;
    
    // Find the full deck in ARCHETYPES to get all cards
    const fullDeck = ARCHETYPES.find(d => d.id === arch.id);
    const cardGridHtml = fullDeck && fullDeck.cards && fullDeck.cards.length
      ? fullDeck.cards.map(c => `
          <div class="meta-card-item">
            ${{c.img ? `<img src="${{c.img}}" alt="${{c.name}}" class="meta-card-thumb" onclick="openCardZoom(this.src,this.alt)" onerror="this.style.display='none'">` : `<div class="meta-card-thumb" style="background:var(--panel)"></div>`}}
            <div class="meta-card-name">${{c.name}}</div>
            <div class="meta-card-count">×${{c.need}}</div>
          </div>`).join('')
      : '';
    
    let ownedPct = 0;
    if (fullDeck && fullDeck.cards && fullDeck.cards.length) {{
      let have = 0, need = 0;
      for (const c of fullDeck.cards) {{
        need += c.need;
        have += Math.min(collection[c.id] || 0, c.need);
      }}
      ownedPct = need === 0 ? 100 : Math.round(100 * have / need);
    }}
    const buildable = ownedPct >= 100;
    const ownedHtml = `<div class="arch-owned-row">
  <span class="arch-owned-pct">${{ownedPct}}% OWNED</span>
  <span class="${{buildable ? 'arch-buildable-yes' : 'arch-buildable-no'}}">${{buildable ? '✓ BUILD' : '✗ BUILD'}}</span>
</div>`;
    const areaCls = imgs.length === 1 ? 'arch-img-area single' : 'arch-img-area';
    const div = document.createElement('div');
    const cardCls = 'arch-card';
    div.className = cardCls;
    div.innerHTML = `
      <div class="${{areaCls}}">
        ${{imgHtml}}
      </div>
      <div class="arch-body">
        <div class="arch-sticker">#${{i + 1}}</div>
        <div class="arch-name">${{arch.name.toUpperCase()}}</div>
        <div class="arch-stats">
          <div class="arch-share">${{arch.meta_share}}%</div>
          <div class="arch-wr">
            WIN ${{arch.win_rate}}%<br>
            E[WR] <span class="${{ewrCls}}">${{arch.ewr}}%</span>
          </div>
        </div>
        ${{ownedHtml}}
      </div>
      <div class="arch-hover-overlay">
        <div class="arch-hover-title">${{arch.name}}</div>
        <div class="arch-hover-cards">${{cardGridHtml}}</div>
      </div>`;
    grid.appendChild(div);
  }});
  filterMeta();
}}

function normalizeSearch(s) {{
  return s.toLowerCase().normalize('NFD').replace(/[̀-ͯ]/g, '');
}}

function filterMeta() {{
  const q = normalizeSearch(document.getElementById('meta-search-input').value || '');
  const grid = document.getElementById('meta-grid');
  grid.querySelectorAll('.arch-card').forEach(card => {{
    const name = card.querySelector('.arch-name');
    if (!name) return;
    card.style.display = (!q || normalizeSearch(name.textContent).includes(q)) ? '' : 'none';
  }});
}}

// ── COLLECTION tab ──────────────────────────────────────────────────────────
function deckCompletion(deck) {{
  let have = 0, need = 0;
  for (const c of deck.cards) {{
    need += c.need;
    have += Math.min(collection[c.id] || 0, c.need);
  }}
  return need === 0 ? 100 : Math.round(100 * have / need);
}}

function renderDeckList() {{
  const el = document.getElementById('deck-list');
  const newBtn = document.getElementById('new-deck-btn');
  const hint = document.getElementById('deck-list-hint');
  el.innerHTML = '';
  if (hint) el.appendChild(hint);
  ARCHETYPES.forEach((deck, i) => {{
    const pct = deckCompletion(deck);
    const barClass = pct >= 80 ? '' : pct >= 40 ? 'mid' : 'low';
    const div = document.createElement('div');
    div.className = 'deck-item' + (i === activeDeckIdx ? ' active' : '');
    const metaInfo = deck.custom
      ? `<span>${{pct}}% BUILT</span><span style="color:var(--pink)">CUSTOM</span>`
      : `<span>${{pct}}% BUILT</span><span>WR ${{(deck.win_rate*100).toFixed(1)}}%</span><span>META ${{(deck.meta_share*100).toFixed(1)}}%</span>`;
    const delBtn = deck.custom
      ? `<span class="deck-del" onclick="event.stopPropagation();deleteCustomDeck('${{deck.id}}')"
           title="Delete deck" style="cursor:pointer;color:var(--dim);float:right;padding-left:6px">🗑</span>`
      : '';
    div.innerHTML = `
      <div class="dname">${{delBtn}}${{deck.name.toUpperCase()}}</div>
      <div class="xp-bar-wrap"><div class="xp-bar ${{barClass}}" style="width:${{pct}}%"></div></div>
      <div class="deck-meta">${{metaInfo}}</div>`;
    div.onclick = () => selectDeck(i);
    el.appendChild(div);
  }});
  el.appendChild(newBtn);
}}

function selectDeck(idx) {{
  activeDeckIdx = idx;
  renderDeckList();
  renderCards(ARCHETYPES[idx]);
  document.getElementById('clear-deck-btn').disabled = false;
  document.getElementById('collection-empty').style.display = 'none';
  document.getElementById('deck-title-row').style.display = '';
  document.getElementById('card-grid').style.display = '';
  document.getElementById('share-deck-btn').disabled = false;
}}

function clearDeck() {{
  if (activeDeckIdx < 0) return;
  const deck = ARCHETYPES[activeDeckIdx];
  deck.cards.forEach(c => {{ collection[c.id] = 0; }});
  renderCards(deck);
  renderDeckList();
  updateTotal();
  setStatus('DECK CLEARED — PRESS 💾 SAVE TO PERSIST', '');
}}

function shareDeck() {{
  if (activeDeckIdx < 0) return;
  const deck = ARCHETYPES[activeDeckIdx];
  const lines = [`${{deck.name.toUpperCase()}} — Pokemon TCG Pocket Deck`, ''];
  deck.cards.forEach(c => lines.push(`${{c.need}}x ${{c.name}}`));
  lines.push('', 'Built with pocket-meta.online');
  navigator.clipboard.writeText(lines.join('\\n')).then(() => {{
    const btn = document.getElementById('share-deck-btn');
    btn.textContent = '✓ COPIED!';
    setTimeout(() => {{ btn.textContent = '📋 COPY'; }}, 2000);
  }});
}}

function renderCards(deck) {{
  document.getElementById('deck-title').textContent = deck.name.toUpperCase();
  const grid = document.getElementById('card-grid');
  grid.innerHTML = '';
  deck.cards.forEach(c => {{
    const owned  = collection[c.id] || 0;
    const cls    = owned >= c.need ? 'owned' : owned > 0 ? 'partial' : 'missing';
    const sprite = TYPE_SPRITE[c.type] || '🃏';
    const typeCls = TYPE_COLOR[c.type] || 'type-Trainer';
    const div = document.createElement('div');
    div.className = `card ${{cls}}`;
    div.id = `card-${{c.id.replace(/[^a-z0-9]/gi,'_')}}`;
    const imgHtml = c.img
      ? `<img class="card-img" src="${{c.img}}"
             onerror="this.style.display='none';this.nextElementSibling.style.display='flex';"
             onclick="openCardZoom(this.src,this.alt)"
             alt="${{c.name}}">
         <div class="card-img-fallback" style="display:none">${{sprite}}</div>`
      : `<div class="card-img-fallback">${{sprite}}</div>`;
    div.innerHTML = `
      <div class="card-type-badge ${{typeCls}}">${{c.type.toUpperCase()}}</div>
      ${{imgHtml}}
      <div class="card-name">${{c.name.toUpperCase()}}</div>
      <div class="need-label">NEED: ${{c.need}}</div>
      <div class="counter">
        <button class="btn-counter" onclick="adjust('${{c.id}}',${{c.need}},-1)">−</button>
        <span class="count-display" id="cnt-${{c.id.replace(/[^a-z0-9]/gi,'_')}}">${{owned}}</span>
        <button class="btn-counter" onclick="adjust('${{c.id}}',${{c.need}},+1)">+</button>
      </div>`;
    grid.appendChild(div);
  }});
}}

function adjust(cardId, need, delta) {{
  const next = Math.max(0, Math.min((collection[cardId] || 0) + delta, 4));
  collection[cardId] = next;
  const safe = cardId.replace(/[^a-z0-9]/gi, '_');
  const el = document.getElementById('cnt-' + safe);
  if (el) el.textContent = next;
  const cardEl = document.getElementById('card-' + safe);
  if (cardEl) cardEl.className = 'card ' + (next >= need ? 'owned' : next > 0 ? 'partial' : 'missing');
  // Sync to catalog tab if it's showing this card
  const catCnt = document.getElementById('cat-cnt-' + safe);
  if (catCnt) catCnt.textContent = next;
  const catBadge = document.getElementById('cat-badge-' + safe);
  if (catBadge) catBadge.style.display = next >= 1 ? '' : 'none';
  const catCard = document.getElementById('cat-card-' + safe);
  if (catCard) catCard.className = 'card cat-card ' + (next >= 1 ? 'owned' : 'missing');
  if (activeDeckIdx >= 0) renderDeckList();
  updateTotal();
  setStatus('UNSAVED CHANGES — PRESS 💾 SAVE', '');
}}

// ── ANALYSIS tab (Shoppu Fighter layout) ────────────────────────────────────
const ROLE_ORDER = {{ win_condition:0, engine:1, staple:2, tech:3, garnet:4 }};
const ROLE_LABEL = {{ win_condition:'WIN CON', engine:'ENGINE', staple:'STAPLE', tech:'TECH', garnet:'GARNET' }};
const ROLE_DESC  = {{
  win_condition: 'Primary attacker · 80+ dmg · archetype-specific',
  engine:        'Core consistency · draw, search, acceleration · in 50%+ of decks',
  staple:        'Widely used support · in 20–49% of decks',
  tech:          'Situational pick · counters specific matchups · in 5–19% of decks',
  garnet:        'Niche / rarely played · in <5% of meta decks',
}};
const ROLE_COLOR = {{
  win_condition: 'var(--pink)',
  engine:        'var(--blue)',
  staple:        'var(--green)',
  tech:          'var(--gold)',
  garnet:        '#FF6B35',
}};
const AN_ROLES = ['win_condition','engine','staple','tech','garnet'];

let anYourId     = '';
let anOppId      = '';
let anPicker     = null;
let anRoleFilter = null;
// anYourId / anOppId start empty — user must tap to select

function roleFractions(cards) {{
  const counts = {{ win_condition:0, engine:0, staple:0, tech:0, garnet:0 }};
  if (!cards || !cards.length) return counts;
  let total = 0;
  for (const c of cards) {{
    const r = c.role || 'garnet';
    counts[r] = (counts[r] || 0) + (c.need || 1);
    total += (c.need || 1);
  }}
  if (!total) return counts;
  for (const r in counts) counts[r] = counts[r] / total * 100;
  return counts;
}}

function deckCosineSimilarity(cardsA, cardsB) {{
  const vA = {{}}, vB = {{}};
  for (const c of (cardsA || [])) vA[c.id] = (vA[c.id] || 0) + (c.need || c.count || 1);
  for (const c of (cardsB || [])) vB[c.id] = (vB[c.id] || 0) + (c.need || c.count || 1);
  const ids = new Set([...Object.keys(vA), ...Object.keys(vB)]);
  let dot = 0, magA = 0, magB = 0;
  for (const id of ids) {{
    const a = vA[id] || 0, b = vB[id] || 0;
    dot += a * b; magA += a * a; magB += b * b;
  }}
  return magA && magB ? dot / (Math.sqrt(magA) * Math.sqrt(magB)) : 0;
}}

function anVerdictInfo(wr) {{
  if (wr === undefined || wr === null) return {{ label:'— NO DATA', color:'var(--dim)', bgColor:'#ccc' }};
  if (wr >= 0.55) return {{ label:'⬆ FAVORABLE',   color:'var(--green)', bgColor:'#E8F5E9' }};
  if (wr <= 0.45) return {{ label:'⬇ UNFAVORABLE', color:'var(--pink)',  bgColor:'#FFE4EE' }};
  return {{ label:'➔ EVEN MATCH', color:'var(--gold)', bgColor:'#FFF9E3' }};
}}

function anTop3Cards(deck) {{
  // Return up to 3 unique Pokémon cards from the deck.
  // Priority: 1) name position in deck name, 2) role priority (win_condition first)
  const deckNameLower = (deck.name || deck.id || '').toLowerCase();
  const normalizedDeckName = deckNameLower.replace(/[^a-z0-9]+/g, ' ');
  const seen = {{}};
  const unique = [];
  for (const c of (deck.cards || [])) {{
    if (!seen[c.id] && c.type === 'Pokemon') {{ seen[c.id] = true; unique.push(c); }}
  }}
  const titleIndex = (card) => {{
    if (!normalizedDeckName || !card.name) return Number.MAX_SAFE_INTEGER;
    const normalized = card.name.toLowerCase().replace(/ ex$/i, '').replace(/ v$/i, '').replace(/[^a-z0-9]+/g, ' ').trim();
    if (!normalized) return Number.MAX_SAFE_INTEGER;
    const regex = new RegExp(`\\b${{normalized.replace(/[.*+?^${{}}()|[\\]\\\\]/g, '\\\\\\\\$&')}}\\b`);
    const match = normalizedDeckName.match(regex);
    return match ? match.index : Number.MAX_SAFE_INTEGER;
  }};
  unique.sort((a, b) => {{
    const aPos = titleIndex(a);
    const bPos = titleIndex(b);
    if (aPos !== bPos) return aPos - bPos;
    return (ROLE_ORDER[a.role] ?? 4) - (ROLE_ORDER[b.role] ?? 4);
  }});
  return unique.slice(0, 3);
}}

function anHandHtml(deck) {{
  const cards = anTop3Cards(deck);
  if (!cards.length) return '<div class="an-sc-blank">🃏</div>';
  return `<div class="an-sc-hand">${{
    cards.map(c => `
      <div class="an-sc-hand-card">
        ${{c.img
          ? `<img src="${{c.img}}" alt="${{c.name}}" onerror="this.parentNode.innerHTML='<div class=\\'an-sc-blank\\'>🃏</div>'">`
          : `<div class="an-sc-blank">🃏</div>`
        }}
      </div>`).join('')
  }}</div>`;
}}

function anDnaSvg(fracs) {{
  return AN_ROLES.map(r => {{
    const pct = fracs[r] || 0;
    if (pct < 0.5) return '';
    const col = ROLE_COLOR[r];
    return `<div class="an-sc-dna-seg" style="width:${{pct.toFixed(1)}}%;background:${{col}}" title="${{ROLE_LABEL[r]}}: ${{pct.toFixed(0)}}%"></div>`;
  }}).join('');
}}

function renderAnalysis() {{
  anRenderRoot();
}}

function anRenderRoot() {{
  const root = document.getElementById('an-root');
  if (!root) return;

  const yourDeck = anYourId ? ARCHETYPES.find(d => d.id === anYourId) : null;
  const oppDeck  = anOppId  ? META_DATA.find(m => m.id === anOppId)  : null;
  const bothSelected = !!(yourDeck && oppDeck);

  // Always compute fracs (safe with empty arrays)
  const yourFracs = roleFractions(yourDeck ? yourDeck.cards : []);
  const oppArch   = oppDeck ? ARCHETYPES.find(d => d.id === oppDeck.id) : null;
  const oppFracs  = roleFractions(oppArch ? oppArch.cards : []);

  // Show selected deck(s) immediately — full analysis only needs both
  if (!bothSelected) {{
    const vi0 = {{ label: '? PENDING', color: 'var(--dim)', bgColor: 'var(--panel)' }};
    const yourSide0 = yourDeck
      ? `<div class="an-sc-body" onclick="anOpenPicker('your')">${{anHandHtml(yourDeck)}}<div class="an-sc-tag">◀ TAP</div></div>
         <div class="an-sc-name">${{yourDeck.name}}</div>
         <div class="an-sc-meta">META ${{(yourDeck.meta_share*100).toFixed(0)}}%</div>
         <div class="an-sc-dna">${{anDnaSvg(yourFracs)}}</div>`
      : `<div class="an-sc-body an-sc-empty" onclick="anOpenPicker('your')"><div class="an-sc-prompt">◀ TAP TO SELECT</div></div>
         <div class="an-sc-name" style="color:var(--dim)">—</div>`;
    const oppSide0 = (oppDeck && oppArch)
      ? `<div class="an-sc-body" onclick="anOpenPicker('meta')">${{anHandHtml(oppArch)}}<div class="an-sc-tag right">TAP ▶</div></div>
         <div class="an-sc-name">${{oppDeck.name}}</div>
         <div class="an-sc-meta">META ${{oppDeck.meta_share.toFixed(0)}}%</div>
         <div class="an-sc-dna">${{anDnaSvg(oppFracs)}}</div>`
      : `<div class="an-sc-body an-sc-empty" onclick="anOpenPicker('meta')"><div class="an-sc-prompt">TAP TO SELECT ▶</div></div>
         <div class="an-sc-name" style="color:var(--dim)">—</div>`;
    root.innerHTML = `
      <div class="an-header">
        <div class="an-header-eyebrow">
          <span class="an-route">// POCKET META</span>
          <div class="an-rule"></div>
          <span class="an-badge">◆ ANALYSIS</span>
        </div>
        <div class="an-headline">Pick your fighter.<span class="pink"> We do the math.</span></div>
        <div class="an-desc">Select your deck and an opponent archetype to calculate your expected win rate and matchup breakdown · Based on Limitless TCG tournament data</div>
      </div>
      <div class="an-scoreboard">
        <div class="an-score-card-wrap">
          <div class="an-sc-above">YOUR DECK</div>
          ${{yourSide0}}
        </div>
        <div class="an-verdict-core">
          <div class="an-verdict-sticker" style="background:${{vi0.bgColor}};color:${{vi0.color}}">${{vi0.label}}</div>
          <div class="an-verdict-label">➜ WIN RATE ←</div>
          <div class="an-verdict-wr" style="color:${{vi0.color}}">—</div>
          <div class="an-verdict-r2">SELECT BOTH DECKS</div>
          <div class="an-verdict-dots">
            <span style="animation-delay:0s"></span>
            <span style="animation-delay:.15s"></span>
            <span style="animation-delay:.30s"></span>
          </div>
        </div>
        <div class="an-score-card-wrap right">
          <div class="an-sc-above">META DECK</div>
          ${{oppSide0}}
        </div>
      </div>`;
    return;
  }}

  // ── Similarity-based matchup estimation for custom decks ─────────────
  let mostSimilarMeta = null;
  let myMatchups;
  if (yourDeck.custom) {{
    const metaArchetypes = ARCHETYPES.filter(a => !a.custom);
    const sims = metaArchetypes.map(a => ({{
      id: a.id, name: a.name, sim: deckCosineSimilarity(yourDeck.cards, a.cards)
    }})).sort((a, b) => b.sim - a.sim);
    mostSimilarMeta = sims[0] || null;
    const topN = sims.slice(0, 3).filter(s => s.sim > 0.05);
    const estimated = {{}};
    META_DATA.forEach(m => {{
      let wSum = 0, wTot = 0;
      for (const {{id, sim}} of topN) {{
        const wr = (MATCHUP_DATA[id] || {{}})[m.id];
        if (wr !== undefined) {{ wSum += wr * sim; wTot += sim; }}
      }}
      if (wTot > 0) estimated[m.id] = wSum / wTot;
    }});
    myMatchups = estimated;
  }} else {{
    myMatchups = MATCHUP_DATA[yourDeck.id] || {{}};
  }}

  const rawWr = myMatchups[oppDeck.id];

  // ── Insight strip data ────────────────────────────────────────────────
  const ad = ANALYSIS_DATA.find(a => a.id === yourDeck.id || a.name === yourDeck.name) || {{}};
  const attr = Object.assign({{}}, (ad && ad.attribution) || {{}});
  if (yourDeck.custom && REGRESSION && REGRESSION.coef) {{
    AN_ROLES.forEach(r => {{ if (attr[r] === undefined) attr[r] = (REGRESSION.coef[r] || 0) * (yourFracs[r] || 0) / 100; }});
  }}
  const worstRole = AN_ROLES.reduce((best, r) => (attr[r]||0) < (attr[best]||0) ? r : best, AN_ROLES[0]);
  const worstVal  = Math.abs(attr[worstRole] || 0).toFixed(1);
  const totalMissing = (ad && ad.total_missing) || 0;
  let predWr = (ad && ad.predicted_wr !== undefined) ? ad.predicted_wr : null;
  if (predWr === null && yourDeck.custom && REGRESSION && REGRESSION.coef) {{
    let _wr = (REGRESSION.intercept || 0);
    AN_ROLES.forEach(r => {{ _wr += (REGRESSION.coef[r] || 0) * (yourFracs[r] || 0) / 100; }});
    predWr = parseFloat((_wr * 100).toFixed(1));
  }}
  predWr = predWr !== null ? predWr : '—';

  const vi      = anVerdictInfo(rawWr);
  const wrStr   = rawWr !== undefined ? (rawWr * 100).toFixed(1) + '%' : 'N/A';
  const wrLabel = yourDeck.custom ? 'EST. WIN RATE' : '➜ WIN RATE ←';
  let bestOpp = null, bestOppWr = -1;
  META_DATA.forEach(m => {{
    const w = myMatchups[m.id];
    if (w !== undefined && w > bestOppWr) {{ bestOppWr = w; bestOpp = m; }}
  }});
  const bestOppStr = bestOpp ? `${{bestOpp.name.slice(0,20)}} (${{(bestOppWr*100).toFixed(0)}}%)` : '—';

  // ── Nav stats ─────────────────────────────────────────────────────────
  let favorable = 0, totalShare = 0, weightedWr = 0, matchupCount = 0;
  META_DATA.forEach(m => {{
    const w = myMatchups[m.id];
    if (w === undefined) return;
    if (w >= 0.52) favorable++;
    totalShare  += m.meta_share;
    weightedWr  += m.meta_share * w;
    matchupCount++;
  }});
  const wwrStr = totalShare > 0 ? (weightedWr / totalShare * 100).toFixed(1) + '%' : '—';
  const favStr = `${{favorable}}/${{matchupCount}}`;

  // ── Nav pills ─────────────────────────────────────────────────────────
  const pillsHtml = META_DATA.map(m => {{
    const w = myMatchups[m.id];
    const vi2 = anVerdictInfo(w);
    const pillWrStr = w !== undefined ? (w*100).toFixed(0) + '%' : '—';
    const isActive = m.id === oppDeck.id;
    return `<div class="an-nav-pill${{isActive ? ' active' : ''}}" onclick="anSetOpp('${{m.id}}')">
      ${{m.hero_img ? `<img src="${{m.hero_img}}" onerror="this.style.display='none'" alt="${{m.name}}">` : ''}}
      <span class="pill-name">${{m.name.slice(0,12)}}</span>
      <span class="pill-wr" style="color:${{isActive ? 'white' : vi2.color}}">${{pillWrStr}}</span>
    </div>`;
  }}).join('');

  // ── Close the gap panel ───────────────────────────────────────────────
  const neededByRole = {{ win_condition:0, engine:0, staple:0, tech:0, garnet:0 }};
  const ownedByRole  = {{ win_condition:0, engine:0, staple:0, tech:0, garnet:0 }};
  const seenR = {{}};
  for (const c of yourDeck.cards) {{
    if (seenR[c.id]) continue; seenR[c.id] = true;
    const r = c.role || 'garnet';
    neededByRole[r] += c.need;
    ownedByRole[r]  += Math.min(collection[c.id] || 0, c.need);
  }}

  const roleRowsHtml = AN_ROLES.filter(r => neededByRole[r] > 0).map(r => {{
    const pct = Math.round(ownedByRole[r] / neededByRole[r] * 100);
    const col = ROLE_COLOR[r];
    const attrVal = attr[r] || 0;
    const attrSign = attrVal >= 0 ? '+' : '';
    const attrColor = attrVal >= 0 ? 'var(--green)' : 'var(--red)';
    const isActive = anRoleFilter === r;
    const missingInRole = neededByRole[r] - ownedByRole[r];
    return `<div class="an-role-row${{isActive ? ' active' : ''}}" onclick="anToggleRole('${{r}}')">
      <div>
        <span class="an-role-badge role-${{r}}" style="background:${{col}}">${{ROLE_LABEL[r]}}</span>
        <div class="an-role-desc">${{ROLE_DESC[r]}}</div>
      </div>
      <div>
        <div class="an-comp-bar-wrap">
          <div class="an-comp-bar-fill" style="width:${{pct}}%;background:${{col}}"></div>
        </div>
        <div class="an-comp-bar-label">OWNED ${{pct}}%${{missingInRole > 0 ? ' · MISSING ' + missingInRole : ''}}</div>
      </div>
      <div class="an-role-attr" style="color:${{attrColor}}">${{attrSign}}${{attrVal.toFixed(1)}}%</div>
      <div class="an-role-attr-label">MODEL /<br>CONTRIB.</div>
    </div>`;
  }}).join('');

  // All unique cards in the deck with owned/need counts
  const allCards = [];
  if (aggregateByName) {{
    const nameMap = {{}};
    for (const c of yourDeck.cards) {{
      const base = c.name.replace(/\s+(ex|v|vmax|vstar|gx)\s*$/i, '').trim() || c.name;
      if (!nameMap[base]) {{
        nameMap[base] = {{ name: base, need: 0, owned: 0, role: c.role || 'garnet', id: c.id, img: c.img || '' }};
      }}
      const prev = nameMap[base];
      prev.need += c.need;
      prev.owned = Math.min(prev.owned + Math.min(collection[c.id] || 0, c.need), prev.need);
    }}
    allCards.push(...Object.values(nameMap));
  }} else {{
    const seen2 = {{}};
    for (const c of yourDeck.cards) {{
      if (seen2[c.id]) continue; seen2[c.id] = true;
      const owned = Math.min(collection[c.id] || 0, c.need);
      allCards.push({{ name: c.name, need: c.need, owned, role: c.role || 'garnet', id: c.id, img: c.img || '' }});
    }}
  }}
  allCards.sort((a, b) => (ROLE_ORDER[a.role] ?? 4) - (ROLE_ORDER[b.role] ?? 4));
  const allMissing = allCards.filter(c => c.owned < c.need);
  const totalMissingCount = allMissing.length;
  const visibleCards = anRoleFilter ? allCards.filter(c => c.role === anRoleFilter) : allCards;

  const cardListHtml = visibleCards.length
    ? `<div class="an-acq-grid">${{visibleCards.map(c => {{
        const isFull = c.owned >= c.need;
        const countColor = isFull ? 'var(--green)' : 'var(--red)';
        return `<div class="an-acq-item">
          ${{c.img ? `<img class="an-acq-thumb" src="${{c.img}}" onerror="this.style.display='none'" alt="${{c.name}}">` : `<div class="an-acq-thumb"></div>`}}
          <div style="flex:1;min-width:0">
            <span class="an-role-badge role-${{c.role}}" style="background:${{ROLE_COLOR[c.role]}}">${{ROLE_LABEL[c.role]}}</span>
            <div class="an-acq-name">${{c.name}}</div>
          </div>
          <span class="an-acq-count" style="color:${{countColor}}">${{c.owned}}/${{c.need}}</span>
        </div>`;
      }}).join('')}}</div>`
    : `<div class="an-acq-empty">${{anRoleFilter ? `NO ${{ROLE_LABEL[anRoleFilter]}} CARDS IN THIS DECK` : 'NO CARD DATA'}}</div>`;

  const filterBadge = anRoleFilter
    ? `<span class="an-filter-badge">${{ROLE_LABEL[anRoleFilter]}}</span>` : '';

  // ── Why panel divergence ──────────────────────────────────────────────
  const activeFracs = [yourFracs, oppFracs];
  const maxFrac = Math.max(...AN_ROLES.map(r => Math.max(yourFracs[r]||0, oppFracs[r]||0)));
  const biggestDeltaRole = AN_ROLES.reduce((best, r) => {{
    const delta = Math.abs((yourFracs[r]||0) - (oppFracs[r]||0));
    const bestDelta = Math.abs((yourFracs[best]||0) - (oppFracs[best]||0));
    return delta > bestDelta ? r : best;
  }}, AN_ROLES[0]);
  const biggestDelta = (yourFracs[biggestDeltaRole]||0) - (oppFracs[biggestDeltaRole]||0);
  const calloutHtml = Math.abs(biggestDelta) >= 5
    ? `<span class="arrow">↳</span> Your deck runs <strong>${{Math.abs(biggestDelta).toFixed(0)}}% ${{biggestDelta > 0 ? 'more' : 'less'}}</strong> ${{biggestDeltaRole}} than theirs.`
    : '';
  const divergRowsHtml = AN_ROLES.filter(r => (yourFracs[r]||0) + (oppFracs[r]||0) > 0.5).map(r => {{
    const yf = yourFracs[r] || 0;
    const mf = oppFracs[r] || 0;
    const delta = yf - mf;
    const deltaStr = (delta >= 0 ? '+' : '') + delta.toFixed(0) + '%';
    const deltaCol = delta >= 0 ? 'var(--green)' : 'var(--red)';
    const col = ROLE_COLOR[r];
    return `<div class="an-diverg-row">
      <div><span class="an-role-badge role-${{r}}" style="background:${{col}}">${{ROLE_LABEL[r]}}</span></div>
      <div class="an-diverg-bar-col">
        <div class="an-diverg-bar-wrap">
          <div class="an-diverg-bar-fill" style="width:${{yf.toFixed(0)}}%;background:${{col}}"></div>
        </div>
        <span class="an-diverg-pct">${{yf.toFixed(0)}}%</span>
      </div>
      <div class="an-diverg-bar-col">
        <div class="an-diverg-bar-wrap">
          <div class="an-diverg-bar-fill" style="width:${{mf.toFixed(0)}}%;background:${{col}};opacity:.75"></div>
        </div>
        <span class="an-diverg-pct">${{mf.toFixed(0)}}%</span>
      </div>
      <div class="an-diverg-delta" style="color:${{deltaCol}}">${{deltaStr}}</div>
    </div>`;
  }}).join('');

  // ── Field band ────────────────────────────────────────────────────────
  const allMatchupsSorted = META_DATA.map(m => {{
    const w = myMatchups[m.id];
    return {{ m, w }};
  }}).filter(x => x.w !== undefined).sort((a,b) => b.w - a.w);

  const tierDefs = [
    {{ id:'S', label:'S', minWr:0.60, color:'var(--green)' }},
    {{ id:'A', label:'A', minWr:0.50, color:'var(--blue)' }},
    {{ id:'B', label:'B', minWr:0.40, color:'var(--gold)' }},
    {{ id:'C', label:'C', minWr:0,    color:'var(--pink)' }},
  ];
  const tierHtml = tierDefs.map(tier => {{
    const nextMinWr = tier.id === 'S' ? 1 : tierDefs[tierDefs.indexOf(tier)-1].minWr;
    const decksInTier = allMatchupsSorted.filter(x => {{
      if (tier.id === 'S') return x.w >= 0.60;
      if (tier.id === 'A') return x.w >= 0.50 && x.w < 0.60;
      if (tier.id === 'B') return x.w >= 0.40 && x.w < 0.50;
      return x.w < 0.40;
    }});
    if (!decksInTier.length) return '';
    const deckCardsHtml = decksInTier.map(x => {{
      const isActive = x.m.id === oppDeck.id;
      const wrPct = (x.w * 100).toFixed(0) + '%';
      return `<div class="an-tier-deck${{isActive ? ' active' : ''}}" onclick="anSetOpp('${{x.m.id}}')">
        ${{x.m.hero_img ? `<img class="td-img" src="${{x.m.hero_img}}" onerror="this.style.display='none'" alt="${{x.m.name}}">` : `<div class="td-img" style="background:var(--bg-deep)"></div>`}}
        <div class="td-body">
          <div class="td-name">${{x.m.name.slice(0,14)}}</div>
          <div class="td-wr" style="color:${{tier.color}}">${{wrPct}}</div>
          <div class="td-meta">META ${{x.m.meta_share.toFixed(0)}}%</div>
        </div>
      </div>`;
    }}).join('');
    return `<div class="an-tier-row">
      <div class="an-tier-badge" style="background:${{tier.color}}">
        <span class="tier-letter">${{tier.label}}</span>
        <span class="tier-sub">TIER</span>
      </div>
      <div class="an-tier-decks">${{deckCardsHtml}}</div>
    </div>`;
  }}).join('');

  // ── Assemble root HTML ────────────────────────────────────────────────
  root.innerHTML = `
    <div class="an-header">
      <div class="an-header-eyebrow">
        <span class="an-route">// ROUTE 04</span>
        <div class="an-rule"></div>
        <span class="an-badge">◆ ANALYSIS</span>
      </div>
      <div class="an-headline">Pick your fighter.<span class="pink"> We do the math.</span></div>
    </div>

    <div class="an-scoreboard">
      <div class="an-score-card-wrap">
        <div class="an-sc-above">YOUR DECK</div>
        <div class="an-sc-body" onclick="anOpenPicker('your')">
          ${{anHandHtml(yourDeck)}}
          <div class="an-sc-tag">◀ TAP</div>
        </div>
        <div class="an-sc-name">${{yourDeck.name}}</div>
        <div class="an-sc-meta">${{yourDeck.custom ? 'CUSTOM DECK' : `META ${{(yourDeck.meta_share*100).toFixed(0)}}%`}}</div>
        <div class="an-sc-dna">${{anDnaSvg(yourFracs)}}</div>
      </div>

      <div class="an-verdict-core">
        <div class="an-verdict-sticker" style="background:${{vi.bgColor}};color:${{vi.color}}">${{vi.label}}</div>
        <div class="an-verdict-label">${{wrLabel}}</div>
        <div class="an-verdict-wr" style="color:${{vi.color}}">${{wrStr}}</div>
        <div class="an-verdict-r2">R² = ${{REGRESSION && REGRESSION.r2 !== undefined ? REGRESSION.r2 : '—'}} · MODEL FIT</div>
        <div class="an-verdict-dots">
          <span style="animation-delay:0s"></span>
          <span style="animation-delay:.15s"></span>
          <span style="animation-delay:.30s"></span>
        </div>
      </div>

      <div class="an-score-card-wrap right">
        <div class="an-sc-above">META DECK</div>
        <div class="an-sc-body" onclick="anOpenPicker('meta')">
          ${{anHandHtml(oppArch || {{}})}}
          <div class="an-sc-tag right">TAP ▶</div>
        </div>
        <div class="an-sc-name">${{oppDeck.name}}</div>
        <div class="an-sc-meta">META ${{oppDeck.meta_share.toFixed(0)}}%</div>
        <div class="an-sc-dna">${{anDnaSvg(oppFracs)}}</div>
      </div>
    </div>

    <div class="an-insight">
      <div class="an-insight-lead">◆ READ ME</div>
      <div class="an-insight-items">
        <div class="an-insight-item">Your <strong>${{ROLE_LABEL[worstRole]}}</strong> is dragging this matchup by ${{worstVal}}%.</div>
        <span class="an-insight-div">│</span>
        ${{yourDeck.custom
          ? `<div class="an-insight-item"><strong>Closest meta deck:</strong> ${{mostSimilarMeta ? mostSimilarMeta.name + ' (' + (mostSimilarMeta.sim*100).toFixed(0) + '% similar)' : '—'}} · WRs estimated by similarity</div>`
          : `<div class="an-insight-item"><strong>Acquire ${{totalMissing}} card${{totalMissing!==1?'s':''}}</strong> → predicted WR ${{predWr}}%.</div>`
        }}
        <span class="an-insight-div">│</span>
        <div class="an-insight-item"><strong>Easiest matchup:</strong> ${{bestOppStr}}</div>
      </div>
    </div>

    <div class="an-nav">
      <div class="an-nav-label">
        <div class="big">MATCHUP</div>
        <div class="small">NAVIGATOR</div>
      </div>
      <div class="an-nav-pills">${{pillsHtml}}</div>
      <div class="an-nav-right">
        <div style="color:var(--green)">FAVORABLE ${{favStr}}</div>
        <div>WEIGHTED WR ${{wwrStr}}</div>
        <label style="margin-left:12px;display:flex;align-items:center;font-size:12px;color:var(--dim)">
          <input id="agg-toggle" type="checkbox" onchange="anToggleAggregate()" style="margin-right:6px" ${{aggregateByName ? 'checked' : ''}}>Aggregate by name
          <span class="agg-tooltip" aria-hidden="true">?
            <span class="agg-tooltip-text">When enabled, analysis aggregates cards by base Pokémon name (strips suffixes like ex/v/vmax) and groups by Pokémon name instead of exact card IDs. Useful when you want an aggregated collection completion view across variants.</span>
          </span>
        </label>
      </div>
    </div>

    <div class="an-workspace">
      <div class="an-panel">
        <div class="an-panel-eyebrow">ROLE DNA — COMPOSITION DIVERGENCE</div>
        <div class="an-panel-title">WHY</div>
        <div class="an-why-body">
          <div class="an-callout">
            <div class="an-callout-title">THE BIG SWING</div>
            ${{calloutHtml || '<span style="color:var(--dim);font-size:12px">No significant divergence.</span>'}}
          </div>
          <div class="an-diverg">
            <div class="an-diverg-header">
              <span></span>
              <span>YOUR DECK</span>
              <span>META DECK</span>
              <span style="text-align:right">Δ</span>
            </div>
            ${{divergRowsHtml}}
          </div>
        </div>
      </div>

      <div class="an-panel">
        <div class="an-panel-eyebrow">HOW TO WIN THIS MATCHUP</div>
        <div class="an-panel-title">CLOSE THE GAP</div>
        ${{roleRowsHtml}}
        <div class="an-card-list-header">
          <div class="an-card-list-title">DECK LIST (${{allCards.length}})${{totalMissingCount ? ` · <span style="color:var(--red)">${{totalMissingCount}} MISSING</span>` : ' · <span style="color:var(--green)">✓ COMPLETE</span>'}}</div>
          ${{filterBadge}}
          ${{anRoleFilter ? `<button class="an-clear-btn" onclick="anToggleRole(null)">✕ CLEAR</button>` : ''}}
        </div>
        ${{cardListHtml}}
      </div>
    </div>

    <div class="an-field">
      <div class="an-field-eyebrow">FIELD VIEW</div>
      <div class="an-field-title">FULL MATCHUP SWEEP</div>
      <span class="an-field-subtitle">SORTED BY WIN RATE INTO TIERS</span>
      ${{tierHtml || '<div style="font-family:var(--pixel);font-size:9px;color:var(--dim)">NO MATCHUP DATA</div>'}}
    </div>`;
}}

function anSetOpp(id) {{
  anOppId = id;
  anRenderRoot();
  document.getElementById('analysis-pane').scrollTop = 0;
}}

function anToggleRole(r) {{
  anRoleFilter = anRoleFilter === r ? null : r;
  anRenderRoot();
}}

function anOpenPicker(side) {{
  anPicker = side;
  const overlay = document.getElementById('an-picker-overlay');
  const grid    = document.getElementById('an-picker-grid');
  const sub     = document.getElementById('an-picker-sub');
  sub.textContent = side === 'your' ? 'YOUR DECK' : 'META DECK';
  const decks = side === 'your' ? ARCHETYPES : META_DATA;
  const selectedId = side === 'your' ? anYourId : anOppId;
  grid.innerHTML = decks.map(d => {{
    // For meta side, look up the ARCHETYPES entry to get card images
    const deckWithCards = side === 'your' ? d : (ARCHETYPES.find(a => a.id === d.id) || {{}});
    const metaStr = side === 'meta' ? `META ${{d.meta_share.toFixed(0)}}%` : (d.meta_share > 0 ? `META ${{(d.meta_share*100).toFixed(0)}}%` : '');
    const isSelected = d.id === selectedId;
    // Build mini 3-card fan
    const top3 = anTop3Cards(deckWithCards);
    const cls = ['hc-left','hc-mid','hc-right'];
    const positions = top3.length === 1 ? ['hc-mid'] : top3.length === 2 ? ['hc-left','hc-right'] : cls;
    const fanHtml = top3.length
      ? `<div class="an-picker-hand">${{top3.map((c,i) =>
          `<div class="an-picker-hcard ${{positions[i]}}">${{c.img ? `<img src="${{c.img}}" alt="${{c.name}}" onerror="this.style.display='none'">` : ''}}</div>`
        ).join('')}}</div>`
      : `<div class="an-picker-hand" style="background:var(--bg-deep)"></div>`;
    return `<div class="an-picker-card${{isSelected ? ' selected' : ''}}" onclick="anPickerSelect('${{d.id}}')">
      ${{fanHtml}}
      <div class="an-picker-name">${{d.name}}</div>
      ${{metaStr ? `<div class="an-picker-meta">${{metaStr}}</div>` : ''}}
    </div>`;
  }}).join('');
  overlay.classList.add('open');
}}

function anPickerSelect(id) {{
  if (anPicker === 'your') anYourId = id;
  else                     anOppId  = id;
  anRoleFilter = null;
  anClosePicker(null);
  anRenderRoot();
}}

function anClosePicker(e) {{
  if (e && e.target !== document.getElementById('an-picker-overlay')) return;
  document.getElementById('an-picker-overlay').classList.remove('open');
  anPicker = null;
}}

// ── NEW DECK modal ────────────────────────────────────────────────────────────
let draftDeck = {{ name: '', cards: [] }}; // cards: {{id,name,type,img,count}}

function openNewDeck() {{
  draftDeck = {{ name: '', cards: [] }};
  document.getElementById('nd-name').value = '';
  document.getElementById('nd-search').value = '';
  document.getElementById('nd-err').textContent = '';
  document.getElementById('nd-overlay').classList.add('open');
  ndSearch();
  renderDraft();
}}

function closeNewDeck() {{
  document.getElementById('nd-overlay').classList.remove('open');
}}

function ndSearch() {{
  const q = normalizeSearch(document.getElementById('nd-search').value);
  const results = CATALOG_DATA
    .filter(c => !q || normalizeSearch(c.name).includes(q))
    .slice(0, 40);
  const el = document.getElementById('nd-results');
  el.innerHTML = '';
  if (!results.length) {{
    el.innerHTML = '<div style="padding:12px;font-size:7px;color:var(--dim)">NO CARDS FOUND</div>';
    return;
  }}
  results.forEach(c => {{
    const div = document.createElement('div');
    div.className = 'nd-result';
    div.innerHTML = `
      ${{c.img
        ? `<img class="nd-result-img" src="${{c.img}}"
               onerror="this.style.display='none'" alt="${{c.name}}">`
        : `<div class="nd-result-img" style="display:flex;align-items:center;justify-content:center;font-size:16px">
             ${{TYPE_SPRITE[c.type]||'🃏'}}</div>`}}
      <div class="nd-result-info">
        <div class="nd-result-name">${{c.name.toUpperCase()}}</div>
        <div class="nd-result-sub">${{c.type.toUpperCase()}} · ${{c.set}}</div>
      </div>
    <button class="nd-add-btn" onclick="ndAddCard('${{c.id}}','${{c.name.replace(/'/g,"\\\\'")}}','${{c.type}}','${{c.img || ''}}')">ADD</button>`;
    el.appendChild(div);
  }});
}}

function ndAddCard(id, name, type, img) {{
  const existing = draftDeck.cards.find(c => c.id === id);
  const totalCopies = draftDeck.cards.reduce((s,c) => s + c.count, 0);
  if (existing) {{
    if (existing.count >= 2) {{ setNdErr('MAX 2 COPIES PER CARD'); return; }}
    existing.count++;
  }} else {{
    if (totalCopies >= 20) {{ setNdErr('DECK IS FULL (20 CARDS)'); return; }}
    draftDeck.cards.push({{ id, name, type, img, count: 1 }});
  }}
  setNdErr('');
  renderDraft();
}}

function ndAdjust(id, delta) {{
  const card = draftDeck.cards.find(c => c.id === id);
  if (!card) return;
  const totalOther = draftDeck.cards.filter(c=>c.id!==id).reduce((s,c)=>s+c.count,0);
  if (delta > 0 && card.count >= 2) {{ setNdErr('MAX 2 COPIES PER CARD'); return; }}
  if (delta > 0 && totalOther + card.count >= 20) {{ setNdErr('DECK IS FULL (20 CARDS)'); return; }}
  card.count += delta;
  if (card.count <= 0) draftDeck.cards = draftDeck.cards.filter(c => c.id !== id);
  setNdErr('');
  renderDraft();
}}

function renderDraft() {{
  const total = draftDeck.cards.reduce((s,c) => s+c.count, 0);
  document.getElementById('nd-draft-count').textContent = `${{total}} / 20 CARDS`;
  document.getElementById('nd-draft-count').style.color = total >= 20 ? 'var(--green)' : 'var(--text)';
  const el = document.getElementById('nd-draft-list');
  el.innerHTML = '';
  if (!draftDeck.cards.length) {{
    el.innerHTML = '<div style="padding:12px;font-size:7px;color:var(--dim)">ADD CARDS FROM THE LEFT</div>';
    return;
  }}
  draftDeck.cards.forEach(c => {{
    const div = document.createElement('div');
    div.className = 'nd-draft-item';
    div.innerHTML = `
      <div class="nd-draft-name">${{c.name.toUpperCase()}}</div>
      <button class="nd-draft-btn" onclick="ndAdjust('${{c.id}}',-1)">−</button>
      <span class="nd-draft-cnt">${{c.count}}</span>
      <button class="nd-draft-btn" onclick="ndAdjust('${{c.id}}',+1)">+</button>`;
    el.appendChild(div);
  }});
}}

function setNdErr(msg) {{
  document.getElementById('nd-err').textContent = msg;
}}

function _persistCustomDecks() {{
  const customDecks = ARCHETYPES.filter(d => d.custom).map(d => ({{
    id: d.id, name: d.name,
    cards: d.cards.map(c => ({{ id: c.id, count: c.need }})),
  }}));
  localStorage.setItem('pkmn_custom_decks', JSON.stringify(customDecks));
}}

function saveDraft() {{
  const name = document.getElementById('nd-name').value.trim();
  if (!name) {{ setNdErr('ENTER A DECK NAME'); return; }}
  if (!draftDeck.cards.length) {{ setNdErr('ADD AT LEAST ONE CARD'); return; }}
  const total = draftDeck.cards.reduce((s,c) => s+c.count, 0);
  if (total > 20) {{ setNdErr('TOO MANY CARDS (MAX 20)'); return; }}

  ARCHETYPES.push({{
    id: 'custom-' + Date.now(), name,
    meta_share: 0, win_rate: 0, custom: true,
    cards: draftDeck.cards.map(c => ({{
      id: c.id, name: c.name, type: c.type,
      need: c.count, have: collection[c.id] || 0, img: c.img,
    }})),
  }});
  _persistCustomDecks();

  // Auto-sync collection up to deck needs
  let collectionChanged = false;
  for (const c of draftDeck.cards) {{
    if ((collection[c.id] || 0) < c.count) {{
      collection[c.id] = c.count;
      collectionChanged = true;
    }}
  }}
  if (collectionChanged) localStorage.setItem('pkmn_collection', JSON.stringify(collection));

  closeNewDeck();
  renderDeckList();
  setStatus(collectionChanged ? '✔ DECK SAVED — COLLECTION UPDATED AUTOMATICALLY!' : '✔ DECK SAVED!', 'ok');
}}

function deleteCustomDeck(deckId) {{
  const idx = ARCHETYPES.findIndex(d => d.id === deckId);
  if (idx < 0) return;
  ARCHETYPES.splice(idx, 1);
  if (activeDeckIdx === idx) {{
    activeDeckIdx = -1;
    document.getElementById('collection-empty').style.display = '';
    document.getElementById('deck-title-row').style.display = 'none';
    document.getElementById('card-grid').style.display = 'none';
    document.getElementById('clear-deck-btn').disabled = true;
  }} else if (activeDeckIdx > idx) {{
    activeDeckIdx--;
  }}
  _persistCustomDecks();
  renderDeckList();
  setStatus('DECK DELETED', '');
}}

// ── CATALOG tab ──────────────────────────────────────────────────────────────
let catInitialised = false;

function initCatalog() {{
  if (!catInitialised) {{
    // Populate set dropdown from unique set values
    const sets = [...new Set(CATALOG_DATA.map(c => c.set).filter(Boolean))].sort();
    const sel = document.getElementById('cat-set');
    sets.forEach(s => {{
      const opt = document.createElement('option');
      opt.value = s; opt.textContent = s;
      sel.appendChild(opt);
    }});
    catInitialised = true;
  }}
  filterCatalog();
}}

function setCatType(t) {{
  catType = t;
  document.querySelectorAll('.cat-type-btn').forEach(b => {{
    b.classList.toggle('active',
      (t === '' && b.textContent === 'ALL') ||
      b.textContent === t.toUpperCase()
    );
  }});
  filterCatalog();
}}

function filterCatalog() {{
  const query = normalizeSearch(document.getElementById('cat-search').value || '');
  const set   = document.getElementById('cat-set').value;
  catFiltered = CATALOG_DATA.filter(c =>
    (!query  || normalizeSearch(c.name).includes(query)) &&
    (!set    || c.set === set) &&
    (!catType || c.type === catType)
  );
  catPage = 0;
  renderCatalogPage(true);
}}

function renderCatalogPage(reset) {{
  const grid = document.getElementById('catalog-grid');
  if (reset) grid.innerHTML = '';

  const start = catPage * CAT_PAGE_SIZE;
  const slice = catFiltered.slice(start, start + CAT_PAGE_SIZE);

  slice.forEach(c => {{
    const owned   = collection[c.id] || 0;
    const cls     = owned >= 1 ? 'owned' : 'missing';
    const sprite  = TYPE_SPRITE[c.type] || '🃏';
    const typeCls = TYPE_COLOR[c.type]  || 'type-Trainer';
    const safe    = c.id.replace(/[^a-z0-9]/gi, '_');
    const div = document.createElement('div');
    div.className = `card cat-card ${{cls}}`;
    div.title = owned === 0 ? 'You own 0 copies' : `You own ${{owned}} cop${{owned === 1 ? 'y' : 'ies'}}`;
    div.id = `cat-card-${{safe}}`;
    const imgHtml = c.img
      ? `<img class="card-img" src="${{c.img}}"
             onerror="this.style.display='none';this.nextElementSibling.style.display='flex';"
             onclick="openCardZoom(this.src,this.alt)"
             alt="${{c.name}}">
         <div class="card-img-fallback" style="display:none">${{sprite}}</div>`
      : `<div class="card-img-fallback">${{sprite}}</div>`;
    const ownedBadge = owned >= 1
      ? `<div class="owned-badge" id="cat-badge-${{safe}}">✓ OWNED</div>`
      : `<div class="owned-badge" id="cat-badge-${{safe}}" style="display:none">✓ OWNED</div>`;
    div.innerHTML = `
      ${{ownedBadge}}
      <div class="card-type-badge ${{typeCls}}">${{c.type.toUpperCase()}}</div>
      ${{imgHtml}}
      <div class="card-name">${{c.name.toUpperCase()}}</div>
      <div class="need-label">${{c.set}} · ${{c.id.split('-')[1] || ''}}</div>
      <div class="counter">
        <button class="btn-counter" onclick="adjustCat('${{c.id}}',-1)">−</button>
        <span class="count-display" id="cat-cnt-${{safe}}">${{owned}}</span>
        <button class="btn-counter" onclick="adjustCat('${{c.id}}',+1)">+</button>
      </div>`;
    grid.appendChild(div);
  }});

  const shown = Math.min((catPage + 1) * CAT_PAGE_SIZE, catFiltered.length);
  document.getElementById('cat-count').textContent =
    `SHOWING ${{shown}} OF ${{catFiltered.length}} CARDS`;
  const loadMore = document.getElementById('cat-load-more');
  loadMore.disabled = shown >= catFiltered.length;
  catPage++;
}}

function loadMoreCatalog() {{
  renderCatalogPage(false);
}}

function adjustCat(cardId, delta) {{
  const next = Math.max(0, Math.min((collection[cardId] || 0) + delta, 4));
  collection[cardId] = next;
  const safe = cardId.replace(/[^a-z0-9]/gi, '_');

  // Update catalog tab display
  const catCnt = document.getElementById('cat-cnt-' + safe);
  if (catCnt) catCnt.textContent = next;
  const catBadge = document.getElementById('cat-badge-' + safe);
  if (catBadge) catBadge.style.display = next >= 1 ? '' : 'none';
  const catCard = document.getElementById('cat-card-' + safe);
  if (catCard) catCard.className = 'card cat-card ' + (next >= 1 ? 'owned' : 'missing');

  // Sync collection tab if it has this card displayed
  const deckCnt = document.getElementById('cnt-' + safe);
  if (deckCnt) deckCnt.textContent = next;

  if (activeDeckIdx >= 0) renderDeckList();
  updateTotal();
  setStatus('UNSAVED CHANGES — PRESS 💾 SAVE', '');
}}

// ── Save ─────────────────────────────────────────────────────────────────────
function saveCollection() {{
  const btn = document.getElementById('save-btn');
  btn.textContent = 'SAVING...';
  try {{
    localStorage.setItem('pkmn_collection', JSON.stringify(collection));
    setStatus('✔ COLLECTION SAVED!', 'ok');
    btn.textContent = '✔ SAVED!';
    setTimeout(() => {{ btn.textContent = 'SAVE'; }}, 2000);
    if (activeTab === 'analysis') renderAnalysis();
  }} catch(e) {{
    setStatus('✘ SAVE FAILED', 'err');
    btn.textContent = 'SAVE';
  }}
}}

function setStatus(msg, cls) {{
  const el = document.getElementById('status-msg');
  el.textContent = msg;
  el.className = cls || '';
  clearTimeout(el._t);
  if (msg) {{
    el.classList.add('visible');
    el._t = setTimeout(() => el.classList.remove('visible'), 3500);
  }}
}}

function updateTotal() {{
  document.getElementById('total-count').textContent =
    Object.values(collection).reduce((a,b) => a+b, 0);
}}

// ── Boot ──────────────────────────────────────────────────────────────────────
function bootSequence() {{
  const bar = document.getElementById('boot-bar');
  let pct = 0;
  const iv = setInterval(() => {{
    pct += Math.random() * 8 + 2;
    bar.style.width = Math.min(pct, 100) + '%';
    if (pct >= 100) {{
      clearInterval(iv);
      setTimeout(() => {{
        document.getElementById('boot').style.display = 'none';
        document.getElementById('main-ui').style.display = 'flex';
        renderMeta();
        initUpdatedLabel();
        setStatus('SELECT A TAB TO EXPLORE', '');
      }}, 300);
    }}
  }}, 40);
}}

updateTotal();
bootSequence();

function openCardZoom(src, name) {{
  const overlay = document.getElementById('card-zoom-overlay');
  const img = document.getElementById('card-zoom-img');
  img.src = src;
  img.alt = name || '';
  overlay.classList.add('active');
}}
function closeCardZoom() {{
  document.getElementById('card-zoom-overlay').classList.remove('active');
}}
document.addEventListener('keydown', e => {{ if (e.key === 'Escape') closeCardZoom(); }});

// ── Support modal ────────────────────────────────────────────────────────────
function toggleSupport() {{
  const modal = document.getElementById('support-modal');
  modal.style.display = modal.style.display === 'block' ? 'none' : 'block';
}}
document.addEventListener('click', function(e) {{
  const modal = document.getElementById('support-modal');
  const btn = document.getElementById('kofi-btn');
  if (modal && modal.style.display === 'block' && !modal.contains(e.target) && e.target !== btn) {{
    modal.style.display = 'none';
  }}
}});
</script>

<!-- Support modal -->
<div id="support-modal">
  <button id="support-close" onclick="toggleSupport()">✕</button>
  <h3>SUPPORT THE SITE</h3>
  <a class="support-link" href="https://ko-fi.com/deyosa" target="_blank" rel="noopener">
    <span class="sl-icon">☕</span>
    <span class="sl-text">
      <span class="sl-title">BUY ME A COFFEE</span>
      <span class="sl-sub">One-time donation · Ko-fi</span>
    </span>
  </a>
  <a class="support-link" href="https://www.amazon.com/s?k=pokemon+tcg+pocket+booster+pack&tag=pocketmeta00-20" target="_blank" rel="noopener sponsored">
    <span class="sl-icon">🛒</span>
    <span class="sl-text">
      <span class="sl-title">SHOP POKEMON CARDS</span>
      <span class="sl-sub">Amazon · affiliate link</span>
    </span>
  </a>
</div>

<div id="card-zoom-overlay" onclick="closeCardZoom()">
  <img id="card-zoom-img" src="" alt="">
</div>

<!-- Disclaimer -->
<style>
  #disclaimer {{
    position: fixed; bottom: 0; left: 0; right: 0; z-index: 199;
    background: var(--bg); border-top: 2px solid var(--border);
    font-family: var(--mono); font-size: 12px; color: var(--dim);
    text-align: center; padding: 5px 16px; line-height: 1.4;
    pointer-events: none;
  }}
  /* On desktop push tab content above the disclaimer bar */
  .tab-pane {{ padding-bottom: 48px; }}
  #card-area {{ padding-bottom: 48px; }}
  @media (max-width: 768px) {{
    /* On mobile sit above the fixed tab bar (60px) */
    #disclaimer {{ bottom: 60px; font-size: 12px; padding: 4px 10px; }}
    .tab-pane {{ padding-bottom: 140px !important; }}
    #card-area {{ padding-bottom: 140px; }}
  }}
</style>
<div id="disclaimer">
  Not affiliated with, endorsed, or approved by Nintendo, The Pokémon Company, or Creatures Inc.
  Pokémon and all related names are trademarks of Nintendo/Creatures Inc./GAME FREAK inc.
</div>

</body>
</html>"""  # noqa: E501


# ── SQLite persistence ─────────────────────────────────────────────────────────

def _get_db_path(base_dir: Path) -> Path:
    return base_dir / "collection.db"


def _init_db(db_path: Path) -> None:
    with sqlite3.connect(db_path, check_same_thread=False) as con:
        con.executescript("""
            CREATE TABLE IF NOT EXISTS collection (
                card_id TEXT PRIMARY KEY,
                count   INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS custom_decks (
                deck_id TEXT PRIMARY KEY,
                data    TEXT NOT NULL
            );
            PRAGMA journal_mode = WAL;
        """)


def _load_collection(db_path: Path) -> dict:
    with sqlite3.connect(db_path, check_same_thread=False) as con:
        rows = con.execute("SELECT card_id, count FROM collection").fetchall()
    return {row[0]: row[1] for row in rows}


def _save_collection(db_path: Path, cards: dict) -> None:
    with sqlite3.connect(db_path, check_same_thread=False) as con:
        con.execute("DELETE FROM collection")
        con.executemany(
            "INSERT INTO collection (card_id, count) VALUES (?, ?)",
            [(k, v) for k, v in cards.items() if isinstance(v, int) and v > 0],
        )


def _load_custom_decks(db_path: Path) -> list:
    with sqlite3.connect(db_path, check_same_thread=False) as con:
        rows = con.execute(
            "SELECT data FROM custom_decks ORDER BY rowid"
        ).fetchall()
    return [json.loads(row[0]) for row in rows]


def _save_custom_decks(db_path: Path, decks: list) -> None:
    with sqlite3.connect(db_path, check_same_thread=False) as con:
        con.execute("DELETE FROM custom_decks")
        con.executemany(
            "INSERT INTO custom_decks (deck_id, data) VALUES (?, ?)",
            [(d["id"], json.dumps(d)) for d in decks if "id" in d],
        )


def _maybe_migrate_json(db_path: Path) -> None:
    """One-time migration from legacy JSON files into SQLite on first run."""
    parent = db_path.parent
    coll_json = parent / "my_collection.json"
    decks_json = parent / "my_decks.json"

    with sqlite3.connect(db_path, check_same_thread=False) as con:
        has_cards = con.execute("SELECT COUNT(*) FROM collection").fetchone()[0] > 0
        has_decks = con.execute("SELECT COUNT(*) FROM custom_decks").fetchone()[0] > 0

    if not has_cards and coll_json.exists():
        try:
            with open(coll_json) as f:
                cards = json.load(f)
            if isinstance(cards, dict):
                _save_collection(db_path, cards)
                print(f"  Migrated {len(cards)} cards from {coll_json.name}")
        except Exception:
            pass

    if not has_decks and decks_json.exists():
        try:
            with open(decks_json) as f:
                decks = json.load(f)
            if isinstance(decks, list):
                _save_custom_decks(db_path, decks)
                print(f"  Migrated {len(decks)} custom decks from {decks_json.name}")
        except Exception:
            pass


# ── Flask application factory ──────────────────────────────────────────────────

def create_flask_app(
    archetypes: list[dict],
    catalog: dict[str, Card],
    db_path: Path,
    ewrs: list[float] | None = None,
    attributions: list[dict] | None = None,
    meta_decks: list | None = None,
    outputs_dir: Path | None = None,
    reload_fn=None,
    matchup_matrix: dict | None = None,
    role_map: dict | None = None,
    regression=None,
) -> Flask:
    """Build and return the Flask WSGI application."""
    ewrs = ewrs or []
    attributions = attributions or []
    meta_decks = meta_decks or []
    outputs_dir = (outputs_dir or Path("outputs")).resolve()

    _init_db(db_path)
    _maybe_migrate_json(db_path)

    # Mutable pipeline state — updated atomically by /refresh
    _state: dict = {
        "archetypes":     archetypes,
        "ewrs":           ewrs,
        "attributions":   attributions,
        "meta_decks":     meta_decks,
        "matchup_matrix": matchup_matrix,
        "role_map":       role_map,
        "regression":     regression,
        "html":           None,   # None = needs rebuild
    }
    _lock = threading.Lock()
    _refresh_lock = threading.Lock()  # prevents concurrent refreshes overlapping

    def _get_html() -> str:
        if _state["html"] is None:
            with _lock:
                if _state["html"] is None:
                    page_data = _prepare_page_data(
                        _state["archetypes"], catalog, {},
                        _state["ewrs"], _state["attributions"], _state["meta_decks"],
                        custom_decks=[],
                        matchup_matrix=_state["matchup_matrix"],
                        role_map=_state["role_map"],
                        regression=_state["regression"],
                    )
                    _state["html"] = _build_html(page_data, {})
        return _state["html"]

    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024  # 10 MB POST limit

    @app.get("/")
    def index():
        return Response(_get_html(), mimetype="text/html; charset=utf-8")

    @app.get("/ads.txt")
    def ads_txt():
        _ads_path = Path(__file__).parent.parent / "ads.txt"
        if _ads_path.exists():
            return Response(_ads_path.read_text(), mimetype="text/plain")
        abort(404)

    @app.get("/charts/<path:filename>")
    def charts(filename):
        fpath = (outputs_dir / filename).resolve()
        if not str(fpath).startswith(str(outputs_dir) + os.sep):
            abort(403)
        if not fpath.exists() or fpath.suffix not in (".png", ".jpg", ".svg"):
            abort(404)
        return send_from_directory(str(outputs_dir), filename)

    @app.post("/refresh")
    def refresh():
        if reload_fn is None:
            return jsonify({"error": "reload not configured"}), 501
        if not _refresh_lock.acquire(blocking=False):
            return jsonify({"error": "refresh already in progress"}), 429
        try:
            result = reload_fn()
            if isinstance(result, tuple):
                page_data, state_updates = result
            else:
                page_data = result
                state_updates = {}

            # Build new HTML before touching _state — visitors keep getting old page
            new_html = _build_html(page_data, {})

            # Atomically swap in new state + pre-built HTML
            with _lock:
                _state.update(state_updates)
                _state["html"] = new_html

            return jsonify(page_data)
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500
        finally:
            _refresh_lock.release()

    return app
