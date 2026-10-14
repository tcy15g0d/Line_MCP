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


KIND_LABELS = {"group": "群組", "friend": "好友", "official": "官方帳號", "community": "社群"}
KIND_NOTICES = {
    "friend": "一對一聊天通常比群組更私密，對方也沒有同意讓 AI 讀取。",
    "official": "官方帳號的對話可能含有訂單、帳務、點數或個人資料。",
    "community": "社群含大量不特定成員的發言，訊息量也可能非常大；請確認適合交給 AI 處理。",
}
PICK_LIMIT = 30  # rows shown by the numbered fallback


def list_chats(config: Config, kind: str = "all", search: str = "") -> list[dict]:
    """Group, friend and community chat names known to the local database, flagged when already allowed."""
    if kind not in ("all", *KIND_LABELS):
        raise ValueError("kind must be all, group, friend, official or community")
    database = LineDatabase(config)
    try:
        status = database.refresh(discover=True)
        if not status.get("connected"):
            raise ValueError("無法開啟 LINE 資料庫：" + json.dumps(
                status.get("failures") or status.get("reason"), ensure_ascii=False))
        chats = database.list_chats()
    finally:
        database.close()
    # Name overlap was computed on the full list, so a hidden same-name chat is still flagged.
    chats = [c for c in chats if (kind == "all" or c["kind"] == kind)
             and search.casefold() in c["name"].casefold()]
    for chat in chats:
        configured = config.raw.get("rooms", {}).get(chat["name"], {}).get("chat_id")
        chat["allowed"] = chat["name"] in config.allowed and (not configured or configured == chat["chat_id"])
        chat["last_activity"] = format_time(chat.pop("last_activity_ms"))
    return chats


def format_time(ms) -> str | None:
    """Local ISO time, or None for missing or out-of-range values (Windows rejects some)."""
    try:
        return datetime.fromtimestamp(ms / 1000).astimezone().isoformat(timespec="minutes") if ms and ms > 0 else None
    except (OSError, OverflowError, ValueError, TypeError):
        return None


def commit_allow(config: Config, chats: list[dict], remove: list[dict] = ()) -> dict:
    """Apply additions and removals in one save. Added chats are pinned by chat ID so later
    same-name chats cannot take over; removal keeps the room's other settings (paths, exports)."""
    raw = config.raw
    allowed = raw.setdefault("allowed_rooms", [])
    rooms = raw.setdefault("rooms", {})
    removed = []
    for chat in remove:
        pinned = rooms.get(chat["name"], {}).get("chat_id")
        if chat["name"] in allowed and (not pinned or pinned == chat["chat_id"]):
            allowed.remove(chat["name"])
            rooms.get(chat["name"], {}).pop("chat_id", None)
            removed.append(chat["name"])
    for chat in chats:  # validate everything first so a conflict never leaves a half-written list
        pinned = rooms.get(chat["name"], {}).get("chat_id")
        if chat["name"] in allowed and pinned and pinned != chat["chat_id"]:
            raise ValueError(f"「{chat['name']}」已指向另一個聊天室，同名的聊天室無法同時允許。"
                             "請先 `disallow` 原本的，或在 LINE 中改成不同的名稱。")
    for chat in chats:
        if chat["name"] not in allowed:
            allowed.append(chat["name"])
        rooms.setdefault(chat["name"], {})["chat_id"] = chat["chat_id"]
    save_raw(config.path, raw)
    return {"allowed_rooms": allowed, "config": str(config.path), "removed": removed,
            "kinds": sorted({c["kind"] for c in chats})}


def allow_room(path: str | Path, name: str, chat_id: str | None = None) -> dict:
    config = Config(path, require_rooms=False)
    matches = [g for g in list_chats(config) if g["name"] == name]
    if not matches:
        raise ValueError(f"找不到群組、好友或社群「{name}」。請先執行 `line-local rooms` 確認名稱（需完全相符）。")
    if chat_id:
        matches = [g for g in matches if g["chat_id"] == chat_id]
        if not matches:
            raise ValueError("名稱與 --chat-id 不相符")
    elif len(matches) > 1:
        ids = "\n".join(f"  {g['chat_id']}  {KIND_LABELS[g['kind']]}  最後活動 {g['last_activity']}"
                        for g in matches)
        raise ValueError(f"有 {len(matches)} 個同名的聊天室，請用 --chat-id 指定其中一個：\n{ids}")
    result = commit_allow(config, matches)
    result["kind"] = matches[0]["kind"]
    return result


def allow_with_picker(path: str | Path, pick, initial: str = "") -> dict | None:
    """Edit the allow-list in a full-screen picker over every chat.
    `pick(chats, initial_tab)` returns the confirmed {"add": [...], "remove": [...]}, or None."""
    config = Config(path, require_rooms=False)
    changes = pick(list_chats(config, "all"), initial or "group")
    if not changes or not (changes["add"] or changes["remove"]):
        return None
    return commit_allow(config, [c for c in changes["add"] if not c["allowed"]],
                        [c for c in changes["remove"] if c["allowed"]])


def allow_interactive(path: str | Path, kind: str = "", ask=input, say=print) -> dict | None:
    """Pick chats by number from a searchable list, then confirm. Returns None when cancelled."""
    config = Config(path, require_rooms=False)
    term = ask("搜尋關鍵字（直接按 Enter 只列群組）：").strip()
    chats = list_chats(config, kind or ("all" if term else "group"), term)
    if not chats:
        say("找不到符合的聊天室。")
        return None
    shown = chats[:PICK_LIMIT]
    for number, chat in enumerate(shown, 1):
        flags = "  [已允許]" if chat["allowed"] else ""
        if chat["same_name_count"] > 1:
            flags += "  [同名多個]"
        say(f"{number:>3}. [{KIND_LABELS[chat['kind']]}] {chat['name']}  （最後活動 {chat['last_activity'] or '未知'}）{flags}")
    if len(chats) > len(shown):
        say(f"（還有 {len(chats) - len(shown)} 筆未顯示，請輸入更精確的關鍵字）")
    answer = ask("要允許哪幾個？輸入編號，可用逗號分隔；直接按 Enter 取消：").strip()
    if not answer:
        say("已取消。")
        return None
    try:
        numbers = sorted({int(part) for part in answer.replace("，", ",").replace(" ", ",").split(",") if part})
        if not numbers or numbers[0] < 1 or numbers[-1] > len(shown):
            raise ValueError
    except ValueError:
        raise ValueError(f"請輸入 1 到 {len(shown)} 之間的編號") from None
    picked = [shown[n - 1] for n in numbers if not shown[n - 1]["allowed"]]
    if not picked:
        say("選到的都已經在允許名單內。")
        return None
    say("\n即將開放給 AI 讀取：")
    for chat in picked:
        say(f"  • [{KIND_LABELS[chat['kind']]}] {chat['name']}")
    for notice in dict.fromkeys(KIND_NOTICES[c["kind"]] for c in picked if c["kind"] in KIND_NOTICES):
        say("提醒：" + notice)
    if ask("確定嗎？[y/N] ").strip().lower() not in ("y", "yes"):
        say("已取消，名單沒有變動。")
        return None
    return commit_allow(config, picked)


def disallow_room(path: str | Path, name: str) -> dict:
    config = Config(path, require_rooms=False)
    raw = config.raw
    if name not in raw.get("allowed_rooms", []):
        raise ValueError(f"「{name}」不在允許名單內")
    raw["allowed_rooms"] = [r for r in raw["allowed_rooms"] if r != name]
    raw.get("rooms", {}).get(name, {}).pop("chat_id", None)
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
