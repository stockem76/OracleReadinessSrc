"""
settings.py
-----------
Persisted runtime configuration for the Oracle Readiness MCP server.

Settings are stored as JSON at $READINESS_DATA_DIR/settings.json so they
survive container restarts and are editable via the web UI at /.

Environment variables are used as defaults on first boot only; once
settings.json exists the persisted values take precedence (the UI can
override them at runtime without needing to restart the container).
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger("oracle_readiness_mcp.settings")

_DEFAULTS = {
    # Refresh schedule
    "refresh_hours":        float(os.environ.get("READINESS_REFRESH_HOURS", "6")),
    "autostart_refresh":    os.environ.get("READINESS_AUTOSTART_REFRESH", "1") != "0",
    "active_pillars":       ["erp", "scm", "hcm", "service"],
    # Which releases to highlight / download in detail (empty = show all)
    "target_releases":      [],

    # MCP server auth
    "mcp_token":            os.environ.get("READINESS_TOKEN", ""),

    # GitHub push
    "github_token":         os.environ.get("GITHUB_TOKEN", ""),
    "github_repo":          "",          # owner/repo
    "github_branch":        "main",
    "github_file_path":     "readiness/latest.md",
    "github_auto_push":     False,       # push after every scheduled refresh?

    # Oracle connectivity (informational — scraper uses hardcoded public URLs)
    "oracle_user_agent":    (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 oracle-readiness-mcp/2.0"
    ),
    "oracle_timeout_secs":  30,

    # UI display
    "ui_title":             "Oracle Readiness MCP",
}

# Fields that contain secrets — never echoed in full via the API
_SECRET_FIELDS = {"mcp_token", "github_token"}


class Settings:
    def __init__(self, data_dir: Path) -> None:
        self._path = data_dir / "settings.json"
        self._data: dict = dict(_DEFAULTS)
        self._load()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load(self) -> None:
        if self._path.exists():
            try:
                stored = json.loads(self._path.read_text())
                self._data.update(stored)
                logger.debug("Settings loaded from %s", self._path)
            except Exception:
                logger.exception("Failed to load settings from %s; using defaults", self._path)

    def save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, indent=2))
        tmp.replace(self._path)
        logger.info("Settings saved to %s", self._path)

    # ------------------------------------------------------------------
    # Accessors
    # ------------------------------------------------------------------

    def get(self, key: str, default=None):
        return self._data.get(key, default)

    def set(self, key: str, value) -> None:
        if key not in _DEFAULTS:
            raise KeyError(f"Unknown setting: {key!r}")
        self._data[key] = value

    def update(self, patch: dict) -> None:
        unknown = [k for k in patch if k not in _DEFAULTS]
        if unknown:
            raise KeyError(f"Unknown setting(s): {unknown}")
        self._data.update(patch)

    def as_dict(self, redact_secrets: bool = True) -> dict:
        out = dict(self._data)
        if redact_secrets:
            for f in _SECRET_FIELDS:
                if out.get(f):
                    out[f] = "••••••••"
        return out

    def as_full_dict(self) -> dict:
        """Full dict including secret values — only for internal use."""
        return dict(self._data)

    # ------------------------------------------------------------------
    # Typed property shortcuts used by server.py
    # ------------------------------------------------------------------

    @property
    def refresh_hours(self) -> float:
        return float(self._data.get("refresh_hours", 6))

    @property
    def autostart_refresh(self) -> bool:
        return bool(self._data.get("autostart_refresh", True))

    @property
    def active_pillars(self) -> list[str]:
        return list(self._data.get("active_pillars", ["erp", "scm", "hcm", "service"]))

    @property
    def mcp_token(self) -> str:
        return str(self._data.get("mcp_token", ""))

    @property
    def github_token(self) -> str:
        return str(self._data.get("github_token", ""))

    @property
    def github_repo(self) -> str:
        return str(self._data.get("github_repo", ""))

    @property
    def github_branch(self) -> str:
        return str(self._data.get("github_branch", "main"))

    @property
    def github_file_path(self) -> str:
        return str(self._data.get("github_file_path", "readiness/latest.md"))

    @property
    def github_auto_push(self) -> bool:
        return bool(self._data.get("github_auto_push", False))
