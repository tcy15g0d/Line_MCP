from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


def decode_text(data: bytes) -> str:
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16")
    for encoding in ("utf-8-sig", "cp950"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError("Unsupported text encoding; save as UTF-8 or UTF-16")


DATE = re.compile(r"^(\d{4})[/.\-](\d{1,2})[/.\-](\d{1,2})(?:\s|\(|（|$)")
MESSAGE = re.compile(r"^(?:(上午|下午|AM|PM)\s*)?(\d{1,2}):(\d{2})(?:\s*(AM|PM))?\t([^\t]+)\t(.*)$", re.I)


def parse_export(path: Path, room: str, max_bytes: int) -> list[dict]:
    if path.stat().st_size > max_bytes:
        raise ValueError("Chat export exceeds configured max_file_bytes")
    text = decode_text(path.read_bytes())
    header = "\n".join(text.splitlines()[:5])
    if room not in header:
        raise ValueError("Chat export header does not contain the exact allowed room name")
    records: list[dict] = []
    day = None
    for number, line in enumerate(text.splitlines(), 1):
        date_match = DATE.match(line)
        if date_match:
            day = tuple(int(x) for x in date_match.groups())
            continue
        match = MESSAGE.match(line)
        if match and day:
            prefix, hour, minute, suffix, sender, body = match.groups()
            hour = int(hour)
            period = (prefix or suffix or "").upper()
            if period:
                hour %= 12
                if period in ("下午", "PM"):
                    hour += 12
            try:
                timestamp = datetime(*day, hour, int(minute), tzinfo=ZoneInfo("Asia/Taipei"))
            except ValueError:
                continue
            records.append({"id": f"export:{path.name}:{number}", "source": "text_export",
                            "timestamp_ms": int(timestamp.timestamp() * 1000),
                            "sender": sender, "text": body, "line": number})
        elif records:
            records[-1]["text"] += "\n" + line
    return records
