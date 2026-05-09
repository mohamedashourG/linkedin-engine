"""
Per-client config loader (RULE 20).

Reads `configs/<slug>/` JSON files and overlays them onto operator-level
defaults at run time. Slugs come from `users.client_slug` (defaults to
"glnk" when absent for backward compatibility).

Cache key: (slug, mtime of any file in the slug dir). Files are tiny so we
re-read on mtime change rather than installing a watcher.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

DEFAULT_SLUG = "glnk"

_FILES = (
    "client.json",
    "icp_rubric.json",
    "keyword_pools.json",
    "voice_profiles.json",
    "drafter_guardrails.json",
)


class ClientConfigError(RuntimeError):
    pass


class ClientConfigNotFound(ClientConfigError):
    pass


@dataclass(frozen=True)
class ClientConfig:
    slug: str
    client: dict[str, Any] = field(default_factory=dict)
    icp_rubric: dict[str, Any] = field(default_factory=dict)
    keyword_pools: dict[str, Any] = field(default_factory=dict)
    voice_profiles: dict[str, Any] = field(default_factory=dict)
    drafter_guardrails: dict[str, Any] = field(default_factory=dict)

    @property
    def display_name(self) -> str:
        return str(self.client.get("display_name") or self.slug)

    @property
    def client_name_aliases(self) -> list[str]:
        raw = self.client.get("client_name_aliases") or []
        return [str(s).strip() for s in raw if str(s).strip()]


_cache: dict[str, tuple[float, ClientConfig]] = {}
_cache_lock = threading.Lock()


def _configs_root() -> Path:
    """Repo's configs/ directory.

    Resolution priority:
      1. CLIENT_CONFIGS_DIR env var (absolute path).
      2. <repo_root>/configs (one level up from backend/).
    """
    env_path = os.getenv("CLIENT_CONFIGS_DIR")
    if env_path:
        return Path(env_path).resolve()
    # backend/app/services/client_config.py → repo root is parents[3]
    return Path(__file__).resolve().parents[3] / "configs"


def _slug_dir_mtime(slug_dir: Path) -> float:
    """Latest mtime across the slug directory (creation/edit/delete of any
    config file invalidates the cache)."""
    latest = slug_dir.stat().st_mtime
    for name in _FILES:
        path = slug_dir / name
        if path.exists():
            latest = max(latest, path.stat().st_mtime)
    return latest


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as fp:
            data = json.load(fp)
    except json.JSONDecodeError as err:
        raise ClientConfigError(
            f"Invalid JSON in {path}: {err.msg} at line {err.lineno} col {err.colno}"
        ) from err
    if not isinstance(data, dict):
        raise ClientConfigError(f"{path} must be a JSON object, got {type(data).__name__}")
    # Drop comment keys so they never show up in downstream consumers.
    return {k: v for k, v in data.items() if not k.startswith("_")}


def load(slug: str | None = None, *, root: Path | None = None) -> ClientConfig:
    """Load (or return cached) ClientConfig for a slug.

    Pass `slug=None` (or empty) to get the default config (`glnk`)."""
    resolved = (slug or DEFAULT_SLUG).strip().lower()
    if not resolved.isidentifier() and not all(
        ch.isalnum() or ch in "-_" for ch in resolved
    ):
        raise ClientConfigError(f"Invalid client slug: {slug!r}")

    base = root or _configs_root()
    slug_dir = base / resolved
    if not slug_dir.is_dir():
        raise ClientConfigNotFound(
            f"No client config directory at {slug_dir}. "
            f"Create configs/{resolved}/ with client.json + icp_rubric.json + keyword_pools.json."
        )

    mtime = _slug_dir_mtime(slug_dir)
    with _cache_lock:
        cached = _cache.get(resolved)
        if cached and cached[0] == mtime:
            return cached[1]

    config = ClientConfig(
        slug=resolved,
        client=_read_json(slug_dir / "client.json"),
        icp_rubric=_read_json(slug_dir / "icp_rubric.json"),
        keyword_pools=_read_json(slug_dir / "keyword_pools.json"),
        voice_profiles=_read_json(slug_dir / "voice_profiles.json"),
        drafter_guardrails=_read_json(slug_dir / "drafter_guardrails.json"),
    )

    with _cache_lock:
        _cache[resolved] = (mtime, config)

    log.info(
        "client_config: loaded slug=%s (icp=%s kw=%s voice=%s guard=%s)",
        resolved,
        bool(config.icp_rubric),
        bool(config.keyword_pools),
        bool(config.voice_profiles),
        bool(config.drafter_guardrails),
    )
    return config


def for_operator(operator: dict[str, Any]) -> ClientConfig:
    """Resolve the client config for an operator document. Treats a missing
    or empty `client_slug` as `glnk` (back-compat with pre-RULE-20 users)."""
    return load(operator.get("client_slug") or DEFAULT_SLUG)


def merge_icp_rubric(
    user_rubric: dict[str, Any] | None, client_rubric: dict[str, Any]
) -> dict[str, Any]:
    """Per-axis overlay: user rubric wins on any axis it defines, client
    rubric provides defaults for axes the user didn't customize. Threshold
    follows the same rule (user > client > 6).

    Rationale: per-user rubrics are typed by hand during onboarding; the
    client rubric is the curated baseline that ships with the codebase.
    Falling back per-axis means a thin user rubric still gets the rest of
    the client baseline for free (no need to copy/paste tier-1 industry
    lists into every operator)."""
    user = user_rubric or {}
    out: dict[str, Any] = {}
    for axis in ("title", "industry", "geography", "stage"):
        if user.get(axis):
            out[axis] = user[axis]
        elif client_rubric.get(axis):
            out[axis] = client_rubric[axis]
    if "threshold" in user:
        out["threshold"] = user["threshold"]
    elif "threshold" in client_rubric:
        out["threshold"] = client_rubric["threshold"]
    else:
        out["threshold"] = 6
    return out


def reset_cache() -> None:
    """Test helper. Production code should not call this."""
    with _cache_lock:
        _cache.clear()
