"""Laster config.yaml og .env."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent


def load_env(path: Path | None = None) -> None:
    """Enkel .env-leser (KEY=VALUE). Overskriver ikke eksisterende miljøvariabler."""
    path = path or ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    """Standardverdier fra config.example.yaml, overstyrt av config.yaml."""
    defaults = yaml.safe_load((ROOT / "config.example.yaml").read_text())
    path = Path(path) if path else ROOT / "config.yaml"
    user = yaml.safe_load(path.read_text()) if path.exists() else {}
    cfg = _deep_merge(defaults, user or {})
    validate(cfg)
    return cfg


def validate(cfg: dict) -> None:
    b = cfg["buckets"]
    total = sum(b.values())
    if abs(total - 1.0) > 1e-3:
        raise ValueError(f"buckets må summere til 1.0 (nå {total})")
    if cfg["mode"] not in ("paper", "live"):
        raise ValueError("mode må være 'paper' eller 'live'")
    if cfg["risk"]["max_position_usd"] <= 0:
        raise ValueError("risk.max_position_usd må være > 0")
