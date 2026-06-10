import json
import pytest
from unittest.mock import patch, MagicMock
from src.data_ingest import fetch_card_catalog, load_card_catalog, fetch_tournament_data

SAMPLE_CARDS = [
    {"set": "A1", "number": 1, "name": "Bulbasaur", "type": "Pokemon", "hp": 70},
    {"set": "A1", "number": 2, "name": "Ivysaur", "type": "Pokemon", "hp": 90},
]
SAMPLE_EXTRAS = [{"set": "A1", "number": 1, "attacks": [{"name": "Vine Whip", "damage": 40}]}]


def test_fetch_merges_extras(tmp_path, monkeypatch):
    monkeypatch.setattr("src.data_ingest.CACHE_DIR", tmp_path)
    mock_get = MagicMock()
    mock_get.side_effect = [
        MagicMock(json=lambda: SAMPLE_CARDS, raise_for_status=lambda: None),
        MagicMock(json=lambda: SAMPLE_EXTRAS, raise_for_status=lambda: None),
    ]
    with patch("src.data_ingest.requests.get", mock_get):
        cards = fetch_card_catalog()
    bulbasaur = next(c for c in cards if c["id"] == "A1-001")
    assert "attacks" in bulbasaur
    assert bulbasaur["attacks"][0]["name"] == "Vine Whip"
    assert bulbasaur["hp"] == 70  # original field preserved
    ivysaur = next(c for c in cards if c["id"] == "A1-002")
    assert "attacks" not in ivysaur  # unmatched card not mutated


def test_load_uses_cache(tmp_path, monkeypatch):
    monkeypatch.setattr("src.data_ingest.CACHE_DIR", tmp_path)
    (tmp_path / "cards.json").write_text(json.dumps(SAMPLE_CARDS))
    with patch("src.data_ingest.requests.get") as mock_get:
        cards = load_card_catalog()
    mock_get.assert_not_called()
    assert len(cards) == 2


def test_load_raises_when_missing(tmp_path, monkeypatch):
    monkeypatch.setattr("src.data_ingest.CACHE_DIR", tmp_path)
    with pytest.raises(FileNotFoundError):
        load_card_catalog()


def test_fetch_uses_cache_when_exists(tmp_path, monkeypatch):
    monkeypatch.setattr("src.data_ingest.CACHE_DIR", tmp_path)
    (tmp_path / "cards.json").write_text(json.dumps(SAMPLE_CARDS))
    with patch("src.data_ingest.requests.get") as mock_get:
        cards = fetch_card_catalog()
    mock_get.assert_not_called()
    assert len(cards) == 2


def test_fetched_cards_have_required_fields(tmp_path, monkeypatch):
    monkeypatch.setattr("src.data_ingest.CACHE_DIR", tmp_path)
    mock_get = MagicMock()
    mock_get.side_effect = [
        MagicMock(json=lambda: SAMPLE_CARDS, raise_for_status=lambda: None),
        MagicMock(json=lambda: [], raise_for_status=lambda: None),
    ]
    with patch("src.data_ingest.requests.get", mock_get):
        cards = fetch_card_catalog()
    for card in cards:
        assert {"id", "name", "type"} <= set(card)


def test_fetch_tournament_data_uses_cache(tmp_path, monkeypatch):
    monkeypatch.setattr("src.data_ingest.CACHE_DIR", tmp_path)
    monkeypatch.setattr("src.data_ingest._TOURNAMENT_CACHE", tmp_path / "tournament.json")
    cached = {"archetypes": [{"id": "test", "win_rate": 0.5}], "matchup_matrix": {}}
    (tmp_path / "tournament.json").write_text(json.dumps(cached))
    with patch("src.data_ingest.requests.get") as mock_get:
        result = fetch_tournament_data()
    mock_get.assert_not_called()
    assert result["archetypes"][0]["id"] == "test"


def test_aggregate_decklist_empty():
    from src.data_ingest import _aggregate_decklist
    assert _aggregate_decklist([]) == []


def test_aggregate_decklist_basic():
    from src.data_ingest import _aggregate_decklist
    decklists = [
        {"pokemon": [{"count": 2, "set": "A1", "number": "1", "name": "X"}],
         "trainer": [{"count": 2, "set": "P-A", "number": "5", "name": "Y"}]},
        {"pokemon": [{"count": 2, "set": "A1", "number": "1", "name": "X"}],
         "trainer": [{"count": 1, "set": "P-A", "number": "5", "name": "Y"}]},
    ]
    result = _aggregate_decklist(decklists)
    ids = [c["id"] for c in result]
    assert "A1-001" in ids
    assert "PROMO-A-005" in ids  # P-A is normalized to PROMO-A to match catalog
    assert all(c["count"] > 0 for c in result)
    name_by_id = {c["id"]: c["name"] for c in result}
    assert name_by_id["A1-001"] == "X"
    assert name_by_id["PROMO-A-005"] == "Y"
