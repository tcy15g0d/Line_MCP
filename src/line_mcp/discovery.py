from __future__ import annotations

from pathlib import Path


def diagnose(root: Path) -> dict:
    databases = []
    db_dir = root / "Data" / "db"
    if db_dir.exists():
        for p in sorted(db_dir.glob("*.edb")):
            try:
                with p.open("rb") as f:
                    h = f.read(32)
                databases.append({
                    "path": str(p), "size": p.stat().st_size,
                    "plain_sqlite": h.startswith(b"SQLite format 3\0"),
                    "wxsqlite3_header_candidate": len(h) == 32 and h[21:24] == b"\x40\x20\x20",
                    "wal_present": Path(str(p) + "-wal").exists(),
                })
            except OSError as exc:
                databases.append({"path": str(p), "error": str(exc)})
    cache = root / "Cache"
    return {
        "line_root": str(root), "exists": root.is_dir(), "databases": databases,
        "cache_exists": cache.is_dir(),
        "limitations": [
            "Encrypted .edb requires a valid account-specific key and compatible cipher.",
            "Cache hashes are not verified chat names; do not infer room membership from them.",
            "Encrypted .eimg is not an ordinary image; original attachments may be absent or expired.",
            "Only data present locally or explicitly exported/downloaded can be read.",
        ],
    }


def main_databases(root: Path) -> list[Path]:
    return sorted((root / "Data" / "db").glob("qw*.edb"), key=lambda p: p.stat().st_mtime, reverse=True)
