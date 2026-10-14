from __future__ import annotations

import json
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

from .config import Config
from .database import LineDatabase
from .discovery import diagnose
from .exports import parse_export
from .files import RoomFiles


def bounds(limit: int, offset: int):
    if not 1 <= limit <= 500 or offset < 0:
        raise ValueError("limit must be 1..500 and offset must be >= 0")


def timestamp(value: str | None) -> int | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("Dates must be ISO 8601 (example: 2026-10-14T00:00:00+08:00)") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo("Asia/Taipei"))
    return int(dt.timestamp() * 1000)


def safe_metadata(value) -> dict:
    """Expose only display metadata, never download tokens or media encryption keys."""
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except (ValueError, TypeError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    allowed = {"file_name", "filename", "file_size", "filesize", "width", "height", "duration"}
    return {str(k): v for k, v in parsed.items()
            if str(k).lower() in allowed and isinstance(v, (str, int, float, bool))}


class Service:
    def __init__(self, config: Config):
        self.config = config
        self.database = LineDatabase(config)
        self.files = RoomFiles(config)
        self.lock = threading.RLock()
        self.loaded = False

    def close(self):
        with self.lock:
            self.database.close()

    def diagnostics(self) -> dict:
        with self.lock:
            return {**diagnose(self.config.line_root), "database_status": self.database.status,
                    "allowed_rooms": self.config.allowed, "config": str(self.config.path)}

    def refresh(self) -> dict:
        with self.lock:
            status = self.database.refresh()
            self.files.index.clear()
            self.files.linked.clear()
            self.loaded = True
            return {"database": status, "exports": self._export_status()}

    def _link_message(self, room: str, row: dict) -> list[dict]:
        try:
            info = json.loads(row.get("_contentInfo") or "{}")
        except (TypeError, ValueError):
            return []
        if not isinstance(info, dict):
            return []
        return self.files.register_linked(room, str(row.get("_id", "")), info, int(row.get("_contentType", 0)))

    def ensure_file(self, room: str, file_id: str):
        self.config.room(room)
        if file_id.startswith("msg:"):
            parts = file_id.split(":")
            if len(parts) != 3 or parts[2] not in ("thumbnail", "original"):
                raise ValueError("Invalid message attachment ID")
            if not self.loaded:
                self.refresh()
            row = self.database.message_by_id(room, parts[1])
            if not row:
                raise ValueError("Message attachment is outside the allowed room or unavailable")
            self._link_message(room, row)

    def attachments(self, room: str, query: str = "", limit: int = 100, offset: int = 0) -> dict:
        with self.lock:
            self.config.room(room)
            bounds(limit, offset)
            if not self.loaded:
                self.refresh()
            # DB references give exact room provenance; directory scans are supplemental.
            items = []
            count = 0
            capped = False
            while room in self.database.rooms:
                rows = self.database.messages(room, limit=500, offset=count, attachments_only=True)
                for row in rows:
                    items.extend(d for d in self._link_message(room, row)
                                 if query.casefold() in d["name"].casefold())
                count += len(rows)
                if len(rows) < 500:
                    break
                if count >= self.config.max_scan:
                    capped = True
                    break
            scanned = self.files.scan(room, query, limit=self.config.max_scan, offset=0)
            items.extend(scanned["files"])
            return {"room": room, "files": items[offset:offset+limit], "matched": len(items),
                    "has_more": offset + limit < len(items), "next_offset": offset + min(limit, max(0, len(items)-offset)),
                    "scan_capped": capped or scanned["scan_capped"], "errors": scanned["errors"]}

    def _export_status(self) -> list[dict]:
        return [{"room": room, "path": str(path), "exists": path.is_file()}
                for room in self.config.allowed for path in self.config.paths(room, "exports")]

    def rooms(self) -> dict:
        with self.lock:
            return {"rooms": [{"name": room, "database_connected": room in self.database.rooms,
                               "exports_present": sum(p.is_file() for p in self.config.paths(room, "exports")),
                               "attachment_directories": [str(p) for p in self.files.roots(room)]}
                              for room in self.config.allowed],
                    "loaded": self.loaded}

    def messages(self, room: str, query: str = "", since: str | None = None,
                 until: str | None = None, limit: int = 100, offset: int = 0,
                 attachments_only: bool = False) -> dict:
        with self.lock:
            self.config.room(room)
            bounds(limit, offset)
            start, end = timestamp(since), timestamp(until)
            if start is not None and end is not None and start >= end:
                raise ValueError("since must be earlier than until")
            if not self.loaded:
                self.refresh()
            errors = []
            if room in self.database.rooms:
                rows = self.database.messages(room, query, start, end, limit + 1, offset, attachments_only)
                records = [{"id": str(r.get("_id", "")), "source": "local_database",
                            "timestamp_ms": r["_createdTime"],
                            "sender": r.get("sender_name") or r.get("_from", ""),
                            "text": r.get("_text") or "", "content_type": r.get("_contentType", 0),
                            "attachment_metadata": {**safe_metadata(r.get("_contentInfo")),
                                                    **safe_metadata(r.get("_contentMetadata"))},
                            "attachments": self._link_message(room, r)}
                           for r in rows]
                has_more = len(records) > limit
                records = records[:limit]
                source = "local_database"
            else:
                source = "text_export"
                records = []
                for path in self.config.paths(room, "exports"):
                    if path.is_file():
                        try:
                            records.extend(parse_export(path, room, self.config.max_file))
                        except (OSError, ValueError) as exc:
                            errors.append(str(exc))
                if attachments_only:
                    records = [r for r in records if any(tag in r["text"] for tag in
                               ("[照片]", "[圖片]", "[檔案]", "[影片]", "[語音訊息]", "[Photo]", "[File]", "[Video]", "[Audio]"))]
                records = [r for r in records if query.casefold() in r["text"].casefold()
                           and (start is None or r["timestamp_ms"] >= start)
                           and (end is None or r["timestamp_ms"] < end)]
                records.sort(key=lambda r: (r["timestamp_ms"], r["id"]), reverse=True)
                has_more = len(records) > offset + limit
                records = records[offset:offset + limit]
            budget = self.config.max_chars
            for r in records:
                text = str(r["text"])
                r["text"] = text[:min(10000, budget)]
                budget -= len(r["text"])
                r["text_truncated"] = len(text) > len(r["text"])
                r["timestamp"] = datetime.fromtimestamp(r["timestamp_ms"] / 1000, ZoneInfo("Asia/Taipei")).isoformat()
            return {"room": room, "source": source, "messages": records,
                    "has_more": has_more, "next_offset": offset + len(records),
                    "database_status": self.database.status, "errors": errors,
                    "note": "Only messages present in the snapshot/export are available. Text exports do not include attachment bytes."}
