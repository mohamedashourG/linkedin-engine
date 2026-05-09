"""
Unit tests for app.services.client_config (RULE 20).

Pure file-system loader; no Mongo or network needed. Each test points the
loader at a tmp_path so we don't depend on the repo's real configs/.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services import client_config
from app.services.client_config import (
    ClientConfig,
    ClientConfigError,
    ClientConfigNotFound,
    DEFAULT_SLUG,
    load,
    merge_icp_rubric,
    reset_cache,
)


@pytest.fixture(autouse=True)
def _clear_cache():
    reset_cache()
    yield
    reset_cache()


def _write(slug_dir: Path, name: str, body: dict) -> None:
    slug_dir.mkdir(parents=True, exist_ok=True)
    (slug_dir / name).write_text(json.dumps(body), encoding="utf-8")


def test_load_returns_default_glnk_for_blank_slug(tmp_path: Path):
    _write(tmp_path / "glnk", "client.json", {"slug": "glnk", "display_name": "G LNK"})
    cfg = load(None, root=tmp_path)
    assert cfg.slug == "glnk"
    assert cfg.display_name == "G LNK"


def test_load_reads_all_known_files(tmp_path: Path):
    slug = tmp_path / "edge"
    _write(slug, "client.json", {"slug": "edge", "display_name": "Edge"})
    _write(slug, "icp_rubric.json", {"title": {"tiers": [{"matches": ["CHRO"], "score": 5}]}, "threshold": 6})
    _write(slug, "keyword_pools.json", {"tier_1_topical": ["a", "b"], "title_industry": ["VP HR hospital"]})
    _write(slug, "voice_profiles.json", {"shared_voice_notes": ["lead with the stat"]})
    _write(slug, "drafter_guardrails.json", {"extra_banned_phrases": ["staffing agency"]})

    cfg = load("edge", root=tmp_path)
    assert cfg.slug == "edge"
    assert cfg.icp_rubric["threshold"] == 6
    assert cfg.keyword_pools["title_industry"] == ["VP HR hospital"]
    assert cfg.drafter_guardrails["extra_banned_phrases"] == ["staffing agency"]


def test_load_missing_files_returns_empty_dicts(tmp_path: Path):
    """Each file is optional; the loader doesn't error when only client.json
    is present."""
    slug = tmp_path / "minimal"
    _write(slug, "client.json", {"slug": "minimal"})
    cfg = load("minimal", root=tmp_path)
    assert cfg.icp_rubric == {}
    assert cfg.keyword_pools == {}
    assert cfg.voice_profiles == {}
    assert cfg.drafter_guardrails == {}


def test_load_missing_slug_raises_not_found(tmp_path: Path):
    with pytest.raises(ClientConfigNotFound):
        load("nonexistent", root=tmp_path)


def test_load_invalid_slug_chars_raises(tmp_path: Path):
    with pytest.raises(ClientConfigError):
        load("../etc/passwd", root=tmp_path)


def test_load_invalid_json_raises(tmp_path: Path):
    slug = tmp_path / "broken"
    slug.mkdir()
    (slug / "client.json").write_text("{not valid json", encoding="utf-8")
    with pytest.raises(ClientConfigError) as ei:
        load("broken", root=tmp_path)
    assert "Invalid JSON" in str(ei.value)


def test_load_strips_underscore_comment_keys(tmp_path: Path):
    """Per-file `_comment` keys are documentation; downstream code shouldn't
    have to filter them out."""
    slug = tmp_path / "g"
    _write(slug, "client.json", {"_comment": "explanatory", "slug": "g"})
    cfg = load("g", root=tmp_path)
    assert "_comment" not in cfg.client
    assert cfg.client == {"slug": "g"}


def test_load_caches_by_mtime(tmp_path: Path, monkeypatch):
    slug = tmp_path / "c"
    _write(slug, "client.json", {"slug": "c", "display_name": "first"})
    cfg1 = load("c", root=tmp_path)
    assert cfg1.display_name == "first"

    # Second call without modifying the dir hits cache.
    cfg2 = load("c", root=tmp_path)
    assert cfg2 is cfg1

    # Modify the file: cache should invalidate.
    import time
    time.sleep(0.01)  # ensure mtime ticks on coarse-resolution filesystems
    _write(slug, "client.json", {"slug": "c", "display_name": "second"})
    cfg3 = load("c", root=tmp_path)
    assert cfg3 is not cfg1
    assert cfg3.display_name == "second"


def test_root_directory_resolves_to_repo_root():
    """The default root must point at <repo_root>/configs, not somewhere inside
    backend/. This guards against a refactor that moves the file."""
    from app.services.client_config import _configs_root
    root = _configs_root()
    assert root.name == "configs"
    # Real glnk dir should exist (we ship one).
    assert (root / "glnk").is_dir(), f"expected configs/glnk to exist at {root}"


def test_for_operator_uses_default_when_slug_absent(tmp_path: Path):
    _write(tmp_path / "glnk", "client.json", {"slug": "glnk"})
    cfg = client_config.load(None, root=tmp_path)
    assert cfg.slug == DEFAULT_SLUG


def test_client_name_aliases_strips_blanks(tmp_path: Path):
    slug = tmp_path / "g"
    _write(slug, "client.json", {"slug": "g", "client_name_aliases": ["G LNK", "  ", "GLNK", ""]})
    cfg = load("g", root=tmp_path)
    assert cfg.client_name_aliases == ["G LNK", "GLNK"]


# ---- merge_icp_rubric ----


def test_merge_user_axis_wins():
    user = {"title": {"tiers": [{"matches": ["custom"], "score": 5}]}}
    client = {
        "title": {"tiers": [{"matches": ["default"], "score": 5}]},
        "industry": {"tiers": [{"matches": ["pharma"], "score": 3}]},
        "threshold": 6,
    }
    merged = merge_icp_rubric(user, client)
    assert merged["title"]["tiers"][0]["matches"] == ["custom"]
    assert merged["industry"]["tiers"][0]["matches"] == ["pharma"]
    assert merged["threshold"] == 6


def test_merge_threshold_user_wins_else_client_else_six():
    assert merge_icp_rubric({"threshold": 9}, {"threshold": 5})["threshold"] == 9
    assert merge_icp_rubric({}, {"threshold": 5})["threshold"] == 5
    assert merge_icp_rubric({}, {})["threshold"] == 6


def test_merge_handles_none_user_rubric():
    client = {"industry": {"tiers": [{"matches": ["x"], "score": 3}]}, "threshold": 7}
    merged = merge_icp_rubric(None, client)
    assert merged["industry"]["tiers"][0]["matches"] == ["x"]
    assert merged["threshold"] == 7


def test_merge_omits_axes_with_no_data():
    """If neither side has an axis it shouldn't appear in the merged result —
    icp_scoring._format_rubric tolerates missing axes by emitting '(no tiers
    defined)' but we'd rather not pad the prompt."""
    merged = merge_icp_rubric({}, {"title": {"tiers": []}, "threshold": 6})
    assert "industry" not in merged
    assert "geography" not in merged
    assert "stage" not in merged


def test_default_glnk_config_loads_from_disk():
    """Smoke test that the shipped glnk config parses without raising."""
    cfg = load("glnk")
    assert cfg.slug == "glnk"
    assert cfg.icp_rubric.get("threshold") is not None
    assert "tier_1_topical" in cfg.keyword_pools
    assert "title_industry" in cfg.keyword_pools
