from __future__ import annotations

import copy
import json
import os
import tempfile
from pathlib import Path

DEFAULT_CONFIG = {
    "line_root": "%LOCALAPPDATA%/LINE",
    "allowed_rooms": [],
    "auto_memory_key": True,
    "database": None,
    "snapshot_dir": "data/snapshots",
    "linked_file_roots": ["%USERPROFILE%/Downloads", "%USERPROFILE%/Documents", "%USERPROFILE%/Desktop"],
    "rooms": {},
    "max_file_bytes": 20 * 1024 * 1024,
    "max_scan_files": 100000,
    "max_output_chars": 50000,
}


def default_config_path() -> Path:
    """LINE_MCP_CONFIG, else ./config.local.json when present, else a per-user file."""
    supplied = os.environ.get("LINE_MCP_CONFIG")
    if supplied:
        return Path(supplied)
    local = Path("config.local.json")
    if local.is_file():
        return local
    base = os.environ.get("APPDATA")
    return (Path(base) if base else Path.home() / ".config") / "line-local-mcp" / "config.json"


def new_config() -> dict:
    return copy.deepcopy(DEFAULT_CONFIG)


def save_raw(path: str | Path, raw: dict) -> None:
    """Write the config atomically so an interrupted write never leaves a broken allow-list."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(raw, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(temp, path)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise


class Config:
    def __init__(self, path: str | Path, require_rooms: bool = True):
        self.path = Path(path).resolve()
        self.base = self.path.parent
        try:
            self.raw = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except FileNotFoundError:
            raise ValueError(f"Config not found: {self.path}. Run `line-local init` first.") from None
        self.line_root = self.resolve(self.raw.get("line_root", "%LOCALAPPDATA%/LINE"))
        self.allowed = self.raw.get("allowed_rooms", [])
        if not all(isinstance(r, str) and r for r in self.allowed):
            raise ValueError("allowed_rooms must contain explicit room names")
        if require_rooms and not self.allowed:
            raise ValueError("allowed_rooms is empty. Run `line-local rooms` to list groups, "
                             "then `line-local allow --room <name>`.")
        self.max_file = min(max(int(self.raw.get("max_file_bytes", 20 * 1024 * 1024)), 1), 100 * 1024 * 1024)
        self.max_scan = min(max(int(self.raw.get("max_scan_files", 100000)), 1), 500000)
        self.max_chars = min(max(int(self.raw.get("max_output_chars", 50000)), 1), 200000)

    def resolve(self, value: str) -> Path:
        path = Path(os.path.expandvars(value)).expanduser()
        return (path if path.is_absolute() else self.base / path).resolve()

    def room(self, name: str) -> dict:
        if name not in self.allowed:
            raise ValueError("Room is outside allowed_rooms")
        return self.raw.get("rooms", {}).get(name, {})

    def paths(self, room: str, field: str) -> list[Path]:
        return [self.resolve(p) for p in self.room(room).get(field, [])]

    @property
    def snapshot_dir(self) -> Path:
        return self.resolve(self.raw.get("snapshot_dir", "data/snapshots"))
