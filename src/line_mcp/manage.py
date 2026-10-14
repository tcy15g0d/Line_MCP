"""Allow-list and client-setup helpers behind the `line-local` CLI. Never reads message content."""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from .config import Config, new_config, save_raw
from .database import LineDatabase

CLIENTS = ("json", "claude-code", "codex")


def init_config(path: str | Path, force: bool = False) -> Path:
    path = Path(path)
    if path.exists() and not force:
        raise ValueError(f"設定檔已存在：{path}（要覆寫請加 --force）")
    save_raw(path, new_config())
    return path


def list_chats(config: Config, kind: str = "all", search: str = "") -> list[dict]:
    """Group and friend chat names known to the local database, flagged when already allowed."""
    if kind not in ("all", "group", "friend"):
        raise ValueError("kind must be all, group or friend")
    database = LineDatabase(config)
    try:
        status = database.refresh(discover=True)
        if not status.get("connected"):
            raise ValueError("無法開啟 LINE 資料庫：" + json.dumps(
                status.get("failures") or status.get("reason"), ensure_ascii=False))
        groups = database.list_chats()
    finally:
        database.close()
    # Compute name overlap on the full list so a hidden same-name chat is still flagged.
    groups = [g for g in groups if (kind == "all" or g["kind"] == kind)
              and search.casefold() in g["name"].casefold()]
    for group in groups:
        group["allowed"] = group["name"] in config.allowed
        ms = group.pop("last_activity_ms")
        group["last_activity"] = format_time(ms)
    return groups


def format_time(ms) -> str | None:
    """Local ISO time, or None for missing or out-of-range values (Windows rejects some)."""
    try:
        return datetime.fromtimestamp(ms / 1000).astimezone().isoformat(timespec="minutes") if ms and ms > 0 else None
    except (OSError, OverflowError, ValueError, TypeError):
        return None


def allow_room(path: str | Path, name: str, chat_id: str | None = None) -> dict:
    config = Config(path, require_rooms=False)
    matches = [g for g in list_chats(config) if g["name"] == name]
    if not matches:
        raise ValueError(f"找不到群組或好友「{name}」。請先執行 `line-local rooms` 確認名稱（需完全相符）。")
    if chat_id:
        matches = [g for g in matches if g["chat_id"] == chat_id]
        if not matches:
            raise ValueError("名稱與 --chat-id 不相符")
    elif len(matches) > 1:
        ids = "\n".join(f"  {g['chat_id']}  {'好友' if g['kind'] == 'friend' else '群組'}  最後活動 {g['last_activity']}"
                        for g in matches)
        raise ValueError(f"有 {len(matches)} 個同名的群組或好友，請用 --chat-id 指定其中一個：\n{ids}")
    raw = config.raw
    if name not in raw.setdefault("allowed_rooms", []):
        raw["allowed_rooms"].append(name)
    if chat_id:
        raw.setdefault("rooms", {}).setdefault(name, {})["chat_id"] = chat_id
    save_raw(config.path, raw)
    return {"allowed_rooms": raw["allowed_rooms"], "config": str(config.path), "kind": matches[0]["kind"]}


def disallow_room(path: str | Path, name: str) -> dict:
    config = Config(path, require_rooms=False)
    raw = config.raw
    if name not in raw.get("allowed_rooms", []):
        raise ValueError(f"「{name}」不在允許名單內")
    raw["allowed_rooms"] = [r for r in raw["allowed_rooms"] if r != name]
    raw.get("rooms", {}).pop(name, None)
    save_raw(config.path, raw)
    return {"allowed_rooms": raw["allowed_rooms"], "config": str(config.path)}


def client_config(kind: str, config_path: str | Path) -> str:
    """Ready-to-paste MCP client setup using this machine's interpreter and config path."""
    python = str(Path(sys.executable).resolve())
    args = ["-m", "line_mcp.server", "--config", str(Path(config_path).resolve())]
    if kind == "json":
        return json.dumps({"mcpServers": {"line_local": {"command": python, "args": args,
                                                           "env": {"PYTHONUTF8": "1"}}}},
                          ensure_ascii=False, indent=2)
    if kind == "claude-code":
        return "claude mcp add line_local --scope user -e PYTHONUTF8=1 -- " + subprocess.list2cmdline([python, *args])
    if kind == "codex":
        quoted = ", ".join("'" + a + "'" for a in args)
        return (f"[mcp_servers.line_local]\ncommand = '{python}'\nargs = [{quoted}]\n"
                "startup_timeout_sec = 20\ntool_timeout_sec = 120\n\n"
                "[mcp_servers.line_local.env]\nPYTHONUTF8 = \"1\"\n")
    raise ValueError("kind must be one of: " + ", ".join(CLIENTS))
