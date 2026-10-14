from __future__ import annotations

import argparse
import json
import sys

from . import manage
from .config import Config, default_config_path
from .service import Service

NEXT_STEPS = """已建立設定檔：{path}

下一步：
  1. line-local rooms                       列出你的群組（只顯示名稱）
  2. line-local allow --room "群組名稱"      加入允許名單
  3. line-local check                       確認可以讀到資料
  4. line-local mcp-config --client claude-code   產生 MCP 用戶端設定
"""


def show_rooms(groups: list[dict]) -> None:
    if not groups:
        print("資料庫中沒有任何有名稱的群組。")
        return
    for number, g in enumerate(groups, 1):
        flags = ["已允許"] if g["allowed"] else []
        if g["same_name_count"] > 1:
            flags.append(f"同名 {g['same_name_count']} 個，需用 --chat-id {g['chat_id']}")
        print(f"{number:>3}. {g['name']}  （最後活動 {g['last_activity'] or '未知'}）" + (f"  [{'；'.join(flags)}]" if flags else ""))


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Manage and inspect your own local LINE data")
    parser.add_argument("--config", default=str(default_config_path()),
                        help="設定檔路徑（預設：LINE_MCP_CONFIG、./config.local.json 或使用者目錄）")
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="建立設定檔（允許名單預設為空）")
    init.add_argument("--force", action="store_true")
    sub.add_parser("rooms", help="列出本機資料庫中的群組名稱（不讀訊息）")
    allow = sub.add_parser("allow", help="把群組加入允許名單")
    allow.add_argument("--room", required=True)
    allow.add_argument("--chat-id", help="同名群組時指定其 ID")
    disallow = sub.add_parser("disallow", help="從允許名單移除群組")
    disallow.add_argument("--room", required=True)
    mcp = sub.add_parser("mcp-config", help="產生這台電腦的 MCP 用戶端設定")
    mcp.add_argument("--client", choices=manage.CLIENTS, default="json")
    sub.add_parser("diagnose", help="檢查 LINE 路徑與資料庫")
    sub.add_parser("check", help="檢查允許的群組是否接通")
    search = sub.add_parser("search", help="搜尋允許群組的訊息文字")
    search.add_argument("--room", required=True)
    search.add_argument("--query", default="")
    search.add_argument("--limit", type=int, default=20)
    files = sub.add_parser("files", help="列出允許群組的附件")
    files.add_argument("--room", required=True)
    args = parser.parse_args()
    try:
        run(args)
    except (ValueError, OSError) as exc:
        print("錯誤：" + str(exc), file=sys.stderr)
        sys.exit(1)


def run(args) -> None:
    if args.command == "init":
        print(NEXT_STEPS.format(path=manage.init_config(args.config, args.force)))
        return
    if args.command == "rooms":
        show_rooms(manage.list_groups(Config(args.config, require_rooms=False)))
        return
    if args.command == "allow":
        result = manage.allow_room(args.config, args.room, args.chat_id)
        print("已允許：" + "、".join(result["allowed_rooms"]) + "\n已在執行中的 MCP 用戶端重新連線後生效。")
        return
    if args.command == "disallow":
        result = manage.disallow_room(args.config, args.room)
        print("目前允許：" + ("、".join(result["allowed_rooms"]) or "（無）"))
        return
    if args.command == "mcp-config":
        Config(args.config, require_rooms=False)
        print(manage.client_config(args.client, args.config))
        return
    service = Service(Config(args.config, require_rooms=args.command != "diagnose"))
    try:
        if args.command == "diagnose":
            result = service.diagnostics()
        elif args.command == "check":
            result = service.refresh()
        elif args.command == "search":
            result = service.messages(args.room, args.query, limit=args.limit)
        else:
            result = service.attachments(args.room)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        service.close()


if __name__ == "__main__":
    main()
