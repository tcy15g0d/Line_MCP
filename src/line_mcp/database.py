from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from pathlib import Path

import apsw

from .config import Config
from .discovery import main_databases
from .windows_keys import find_keys


def quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def source_signature(path: Path) -> tuple:
    result = []
    for p in (path, Path(str(path) + "-wal")):
        try:
            s = p.stat()
            result.append((s.st_size, s.st_mtime_ns, s.st_ctime_ns))
        except FileNotFoundError:
            result.append(None)
    return tuple(result)


def copy_snapshot(path: Path, directory: Path) -> tuple[Path, tempfile.TemporaryDirectory]:
    """Copy stable DB + WAL; never open the source using SQLite.

    This is a best-effort file snapshot, not the SQLite online backup API.
    Reject changed sources rather than claim transactional consistency.
    """
    directory.mkdir(parents=True, exist_ok=True)
    for _ in range(2):
        temp = tempfile.TemporaryDirectory(prefix="line-", dir=directory)
        target = Path(temp.name) / path.name
        before = source_signature(path)
        try:
            if before[0] is None:
                raise FileNotFoundError(path)
            needed = sum(s[0] for s in before if s)
            if shutil.disk_usage(directory).free < needed + 64 * 1024 * 1024:
                raise ValueError("Insufficient disk space for encrypted DB/WAL snapshot")
            shutil.copyfile(path, target)
            wal = Path(str(path) + "-wal")
            if before[1] is not None:
                shutil.copyfile(wal, Path(str(target) + "-wal"))
            if before == source_signature(path):
                return target, temp
        except FileNotFoundError:
            pass
        except BaseException:
            temp.cleanup()
            raise
        temp.cleanup()
    raise ValueError("LINE changed its database during copying. Retry while LINE is idle.")


def connect(path: Path, key: str | None, legacy: int = 0) -> apsw.Connection:
    # Only copies are opened read/write, allowing WAL recovery/SHM creation locally.
    db = apsw.Connection(str(path), flags=apsw.SQLITE_OPEN_READWRITE)
    try:
        if key:
            db.pragma("cipher", "aes128cbc")
            db.pragma("legacy", legacy)
            db.pragma("key", key)
        db.pragma("query_only", True)
        db.set_busy_timeout(2000)
        list(db.execute("SELECT name FROM sqlite_master LIMIT 1"))
        return db
    except BaseException:
        db.close()
        raise


class LineDatabase:
    def __init__(self, config: Config):
        self.config = config
        self.db: apsw.Connection | None = None
        self.temp: tempfile.TemporaryDirectory | None = None
        self.source: Path | None = None
        self.rooms: dict[str, str] = {}
        self.status: dict = {"connected": False, "reason": "Call refresh_data first"}

    def close(self):
        if self.db is not None:
            self.db.close()
            self.db = None
        if self.temp:
            self.temp.cleanup()
            self.temp = None

    def refresh(self, discover: bool = False) -> dict:
        """Open a snapshot. `discover` also accepts a database that resolves no allowed room,
        so the CLI can list group names before anything is allowed. The MCP server never uses it."""
        keys: list[str] = []
        key_report = {"status": "disabled"}
        supplied = os.environ.get("LINE_DB_KEY")
        if supplied:
            if not re.fullmatch(r"[0-9a-fA-F]{32}", supplied):
                raise ValueError("LINE_DB_KEY must be a 32-character hexadecimal passphrase")
            keys.append(supplied)
        if self.config.raw.get("auto_memory_key", False):
            discovered, key_report = find_keys(self.config.line_root)
            keys.extend(k for k in discovered if k not in keys)
        candidates = ([self.config.resolve(self.config.raw["database"])]
                      if self.config.raw.get("database") else main_databases(self.config.line_root))
        failures = []
        for source in candidates:
            with source.open("rb") as f:
                plain = f.read(16) == b"SQLite format 3\0"
            if not plain and not keys:
                failures.append({"file": source.name, "reason": "No database key found"})
                continue
            try:
                target, temp = copy_snapshot(source, self.config.snapshot_dir)
            except (OSError, ValueError) as exc:
                failures.append({"file": source.name, "reason": str(exc)})
                continue
            for key in ([None] if plain else keys):
                for legacy in ([0] if plain else [0, 1]):
                    db = None
                    try:
                        db = connect(target, key, legacy)
                        rooms = self._resolve_rooms(db)
                        if not rooms and not discover:
                            db.close()
                            continue
                        self.close()
                        self.db, self.temp, self.source, self.rooms = db, temp, source, rooms
                        self.status = {"connected": True, "source": str(source),
                                       "rooms": list(rooms), "key_discovery": key_report,
                                       "cipher": "plain_sqlite" if plain else "aes128cbc",
                                       "snapshot": "stable-file-copy-with-wal",
                                       "scope": "Only exact allowed room names resolved from database"}
                        return self.status
                    except (apsw.Error, ValueError):
                        if db:
                            db.close()
            temp.cleanup()
            failures.append({"file": source.name, "reason": "Key/cipher/schema mismatch, or allowed room absent"})
        # Never silently serve stale data following a failed refresh.
        self.close()
        self.rooms = {}
        self.status = {"connected": False, "key_discovery": key_report, "failures": failures,
                       "reason": "Use a LINE chat text export and downloaded files if DB is unavailable"}
        return self.status

    def _resolve_rooms(self, db: apsw.Connection) -> dict[str, str]:
        tables = {row[0].casefold() for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"_groupchat", "_message"}.issubset(tables):
            raise ValueError("Unsupported LINE schema")
        self.message_columns = {row[1] for row in db.execute('PRAGMA table_info("_message")')}
        if not {"_chatId", "_createdTime", "_text"}.issubset(self.message_columns):
            raise ValueError("Unsupported message columns")
        groups = {row[1] for row in db.execute('PRAGMA table_info("_groupchat")')}
        if not {"_chatMid", "_chatName"}.issubset(groups):
            raise ValueError("Unsupported group columns")
        self.contact_columns = ({row[1] for row in db.execute('PRAGMA table_info("_contact")')}
                                if "_contact" in tables else set())
        self.profile_columns = ({row[1] for row in db.execute('PRAGMA table_info("_profile")')}
                                if "_profile" in tables else set())
        self.has_chat_table = "_chat" in tables
        self.tables = tables
        self.square_member_columns = ({row[1] for row in db.execute('PRAGMA table_info("_squareMember")')}
                                      if "_squaremember" in tables else set())
        result = {}
        others = None
        for room in self.config.allowed:
            configured = self.config.room(room).get("chat_id")
            if configured:
                rows = list(db.execute('SELECT _chatMid FROM _groupchat WHERE _chatName = ? AND _chatMid = ? LIMIT 3', (room, configured)))
            else:
                rows = list(db.execute('SELECT _chatMid FROM _groupchat WHERE _chatName = ? LIMIT 3', (room,)))
            ids = [row[0] for row in rows]
            if others is None:
                others = self._friend_chats(db) + self._community_chats(db)
            # Group, friend and community names share one namespace: any overlap is ambiguous and fails closed.
            ids += [f["chat_id"] for f in others if f["name"] == room and (not configured or f["chat_id"] == configured)]
            if len(ids) == 1:
                result[room] = ids[0]
        return result

    def _friend_chats(self, db: apsw.Connection) -> list[dict]:
        """One-to-one chats, named by the friend's name as shown in LINE. Names only, no messages."""
        if not self.has_chat_table or not {"_mid"}.issubset(self.contact_columns):
            return []
        chat_columns = {row[1] for row in db.execute('PRAGMA table_info("_chat")')}
        if "_id" not in chat_columns:
            return []
        wanted = [c for c in ("_displayName", "_displayNameOverridden", "_targetProfileDetail") if c in self.contact_columns]
        activity = 'ch."_lastUpdatedTime"' if "_lastUpdatedTime" in chat_columns else "NULL"
        sql = ('SELECT c."_mid", ' + activity + "".join(', c."%s"' % c for c in wanted) +
               ' FROM _contact c JOIN _chat ch ON ch."_id" = c."_mid" WHERE substr(c."_mid", 1, 1) = \'u\'')
        friends = []
        for mid, last, *values in db.execute(sql):
            fields = dict(zip(wanted, values))
            name = fields.get("_displayNameOverridden") or fields.get("_displayName") or ""
            if not name and fields.get("_targetProfileDetail"):
                try:
                    name = json.loads(fields["_targetProfileDetail"]).get("profileName") or ""
                except (ValueError, AttributeError):
                    pass
            if name:
                friends.append({"name": name, "chat_id": mid, "last_activity_ms": last, "kind": "friend"})
        return friends

    def _community_chats(self, db: apsw.Connection) -> list[dict]:
        """Community (Square) chats that have local messages, named "community / chat". Names only."""
        if not {"_squarechat", "_square", "_chat"}.issubset(self.tables):
            return []
        sc = {row[1] for row in db.execute('PRAGMA table_info("_squareChat")')}
        sq = {row[1] for row in db.execute('PRAGMA table_info("_square")')}
        chat = {row[1] for row in db.execute('PRAGMA table_info("_chat")')}
        if not ({"_squareChatMid", "_squareMid", "_name"} <= sc and {"_mid", "_name"} <= sq and "_id" in chat):
            return []
        activity = 'ch."_lastUpdatedTime"' if "_lastUpdatedTime" in chat else "NULL"
        sql = ('SELECT c."_squareChatMid", c."_name", s."_name", ' + activity + ' FROM _squareChat c '
               'JOIN _chat ch ON ch."_id" = c."_squareChatMid" LEFT JOIN _square s ON s."_mid" = c."_squareMid"')
        chats = []
        for mid, chat_name, square_name, last in db.execute(sql):
            chat_name, square_name = chat_name or "", square_name or ""
            name = (chat_name if not square_name else square_name if not chat_name or chat_name == square_name
                    else f"{square_name} / {chat_name}")
            if name:
                chats.append({"name": name, "chat_id": mid, "last_activity_ms": last, "kind": "community"})
        return chats

    def list_chats(self) -> list[dict]:
        """Group, friend and community chat names only (no messages), newest activity first. For CLI selection."""
        if self.db is None:
            return []
        chats = self.list_groups() + self._friend_chats(self.db) + self._community_chats(self.db)
        chats.sort(key=lambda c: c["last_activity_ms"] or 0, reverse=True)
        counts: dict[str, int] = {}
        for chat in chats:
            counts[chat["name"]] = counts.get(chat["name"], 0) + 1
        for chat in chats:
            chat["same_name_count"] = counts[chat["name"]]
        return chats

    def list_groups(self) -> list[dict]:
        """Group names only (no messages), newest activity first."""
        if self.db is None:
            return []
        chat_columns = {row[1] for row in self.db.execute('PRAGMA table_info("_chat")')}
        has_activity = {"_id", "_lastUpdatedTime"}.issubset(chat_columns)
        sql = 'SELECT g."_chatMid", g."_chatName"'
        sql += ', c."_lastUpdatedTime" FROM _groupChat g' if has_activity else ", NULL FROM _groupChat g"
        if has_activity:
            sql += ' LEFT JOIN _chat c ON c."_id" = g."_chatMid"'
        sql += ' WHERE COALESCE(g."_chatName", \'\') != \'\' ORDER BY 3 DESC'
        return [{"name": name, "chat_id": mid, "last_activity_ms": last, "kind": "group"}
                for mid, name, last in self.db.execute(sql)]

    def messages(self, room: str, query: str = "", since_ms: int | None = None,
                 until_ms: int | None = None, limit: int = 100, offset: int = 0,
                 attachments_only: bool = False) -> list[dict]:
        self.config.room(room)
        if self.db is None or room not in self.rooms:
            return []
        columns = [c for c in ("_id", "_chatId", "_from", "_createdTime", "_text", "_contentType", "_contentInfo", "_contentMetadata")
                   if c in self.message_columns]
        where, params = ['m."_chatId" = ?'], [self.rooms[room]]
        if query:
            where.append('instr(lower(COALESCE(m."_text",\'\')), lower(?)) > 0')
            params.append(query)
        if since_ms is not None:
            where.append('m."_createdTime" >= ?')
            params.append(since_ms)
        if until_ms is not None:
            where.append('m."_createdTime" < ?')
            params.append(until_ms)
        if attachments_only:
            if "_contentType" not in self.message_columns:
                raise ValueError("This schema cannot identify attachment messages")
            where.append('m."_contentType" != 0')
        select = ",".join("m." + quote(c) for c in columns)
        sql = "SELECT " + select + " FROM _message m WHERE " + " AND ".join(where)
        sql += ' ORDER BY m."_createdTime" DESC'
        if "_id" in columns:
            sql += ',m."_id" DESC'
        sql += " LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        records = [dict(zip(columns, row)) for row in self.db.execute(sql, params)]
        if "_from" in columns:
            names = self.sender_names({r["_from"] for r in records if r.get("_from")})
            for record in records:
                record["sender_name"] = names.get(record.get("_from"))
        return records

    def sender_names(self, mids: set[str]) -> dict[str, str]:
        """Resolve display names: friend name, then public profile name, then own profile."""
        names: dict[str, str] = {}
        for mid in mids:
            if {"_mid", "_displayName"}.issubset(self.contact_columns):
                extra = ',"_targetProfileDetail"' if "_targetProfileDetail" in self.contact_columns else ""
                row = next(iter(self.db.execute(
                    'SELECT "_displayName"' + extra + ' FROM _contact WHERE "_mid" = ?', (mid,))), None)
                if row:
                    name = row[0] or ""
                    if not name and len(row) > 1 and row[1]:
                        try:
                            name = json.loads(row[1]).get("profileName") or ""
                        except (ValueError, AttributeError):
                            pass
                    if name:
                        names[mid] = name
                        continue
            if {"_squareMemberMid", "_displayName"}.issubset(self.square_member_columns):
                row = next(iter(self.db.execute(
                    'SELECT "_displayName" FROM _squareMember WHERE "_squareMemberMid" = ?', (mid,))), None)
                if row and row[0]:
                    names[mid] = row[0]
                    continue
            if {"_mid", "_displayName"}.issubset(self.profile_columns):
                row = next(iter(self.db.execute(
                    'SELECT "_displayName" FROM _profile WHERE "_mid" = ?', (mid,))), None)
                if row and row[0]:
                    names[mid] = row[0]
        return names

    def message_by_id(self, room: str, message_id: str) -> dict | None:
        self.config.room(room)
        if self.db is None or room not in self.rooms:
            return None
        columns = [c for c in ("_id", "_contentType", "_contentInfo", "_contentMetadata", "_createdTime", "_text")
                   if c in self.message_columns]
        sql = "SELECT " + ",".join(quote(c) for c in columns) + " FROM _message WHERE _chatId=? AND _id=? LIMIT 1"
        row = next(iter(self.db.execute(sql, (self.rooms[room], message_id))), None)
        return dict(zip(columns, row)) if row is not None else None
