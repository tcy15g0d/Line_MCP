"""Verify the real allowed room without logging messages, attachment content, or keys."""
import json
from collections import Counter
from pathlib import Path

from line_mcp.config import Config, default_config_path
from line_mcp.service import Service


def main():
    service = Service(Config(default_config_path()))
    try:
        report = {"refresh": service.refresh(), "rooms": {}}
        for room in service.config.allowed:
            records = service.messages(room, limit=500)
            files = service.attachments(room, limit=500)
            verified = Counter()
            errors = Counter()
            for item in files["files"]:
                if not item.get("exists", True):
                    errors["missing_local_file"] += 1
                    continue
                try:
                    service.ensure_file(room, item["file_id"])
                    data, name, mime = service.files.content(room, item["file_id"])
                    verified[item.get("variant", "downloaded") + ":" + mime] += 1
                    if Path(name).suffix.lower() in (".pdf", ".docx", ".xlsx", ".txt"):
                        text = service.files.read_text(room, item["file_id"], 0, 2000)
                        verified["text_extraction_nonempty" if text["text"].strip() else "text_extraction_empty"] += 1
                except (OSError, ValueError) as exc:
                    errors[str(exc)] += 1
            missing_paths = []
            if service.database.db:
                for row in service.database.messages(room, attachments_only=True, limit=500):
                    if row.get("_contentType") == 14:
                        info = json.loads(row.get("_contentInfo") or "{}")
                        missing_paths.append({"message_id": row["_id"], "filename": info.get("fileName"),
                                              "info_fields": list(info),
                                              "path_exists": Path(info.get("downloadedPath") or info.get("path") or "__missing__").is_file(),
                                              "downloaded_parent": str(Path(info.get("downloadedPath") or "__missing__").parent)})
            report["rooms"][room] = {
                "message_count": len(records["messages"]), "messages_have_more": records["has_more"],
                "content_types": dict(Counter(str(r.get("content_type", "export")) for r in records["messages"])),
                "attachment_reference_count": len(files["files"]),
                "verified": dict(verified), "errors": dict(errors),
                "file_message_sources": missing_paths,
            }
        Path("reports").mkdir(exist_ok=True)
        Path("reports/local_verification.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=True, indent=2))
    finally:
        service.close()


if __name__ == "__main__":
    main()
