"""Plugin discovery and loading.

A plugin is a directory containing ``plugin.py`` that defines ``PLUGIN``.
Loading is deliberately dumb: plugins are installed by an admin and trusted
exactly as much as any other Python dependency. There is no sandbox, and
pretending otherwise would be the dishonest kind of security.
"""
from __future__ import annotations

import importlib.util
import sys
import traceback
from pathlib import Path

from .sdk import Plugin


class LoadError(Exception):
    pass


def discover(paths: list[Path]) -> dict[str, Path]:
    """id -> directory, later paths winning."""
    found: dict[str, Path] = {}
    for root in paths:
        if not root.is_dir():
            continue
        for d in sorted(root.iterdir()):
            if d.name.startswith((".", "_")) or not d.is_dir():
                continue
            if (d / "plugin.py").exists():
                found[d.name] = d
    return found


def load_one(dirpath: Path) -> Plugin:
    name = f"quorum_plugins.{dirpath.name}"
    spec = importlib.util.spec_from_file_location(name, dirpath / "plugin.py")
    if spec is None or spec.loader is None:
        raise LoadError(f"{dirpath}: not importable")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    plugin = getattr(mod, "PLUGIN", None)
    if not isinstance(plugin, Plugin):
        raise LoadError(f"{dirpath}/plugin.py: no module-level PLUGIN = Plugin(...)")
    if plugin.id != dirpath.name:
        raise LoadError(f"{dirpath}: plugin id {plugin.id!r} != directory name")
    if plugin.web and not (dirpath / "web" / plugin.web).exists():
        raise LoadError(f"{dirpath}: declares web module {plugin.web!r} which is missing")
    plugin.dir = dirpath
    plugin.module_name = name          # so a plugin can reach a sibling's helpers
    return plugin


def load_all(paths: list[Path], enabled: list[str] | None, settings: dict) -> tuple[dict[str, Plugin], list[str]]:
    plugins: dict[str, Plugin] = {}
    problems: list[str] = []
    for pid, d in discover(paths).items():
        if enabled is not None and pid not in enabled:
            continue
        try:
            p = load_one(d)
            p.settings = dict(settings.get(pid, {}))
            plugins[pid] = p
        except Exception as e:                       # one bad plugin must not take the server down
            problems.append(f"{pid}: {e}")
            traceback.print_exc()
    return plugins, problems
