"""Actual stdio MCP calls against this machine, emitting only status/counts."""
import asyncio
import json
import sys
from pathlib import Path

from mcp.client import Client
from mcp.client.stdio import StdioServerParameters

from line_mcp.config import Config, default_config_path


def payload(result):
    if result.is_error:
        raise RuntimeError("MCP call failed: " + " ".join(c.text for c in result.content if c.type == "text"))
    if result.structured_content is not None:
        return result.structured_content
    return json.loads(next(c.text for c in result.content if c.type == "text"))


async def main():
    root = Path(__file__).resolve().parents[1]
    config = Config(default_config_path())
    params = StdioServerParameters(command=sys.executable,
        args=["-m", "line_mcp.server", "--config", str(config.path)], cwd=str(root))
    room = config.allowed[0]
    async with Client(params, read_timeout_seconds=120) as client:
        tools = await client.list_tools()
        refreshed = await client.call_tool("refresh_data", {})
        if refreshed.is_error:
            raise RuntimeError("Refresh tool failed")
        messages = await client.call_tool("search_messages", {"room": room, "limit": 500})
        attachments = await client.call_tool("list_attachments", {"room": room, "limit": 500})
        records = payload(messages)["messages"]
        files = payload(attachments)["files"]
        image_id = next(f["file_id"] for f in files if f.get("variant") == "thumbnail" and f.get("exists"))
        docx_id = next(f["file_id"] for f in files if f.get("name", "").endswith(".docx") and f.get("exists"))
        image = await client.call_tool("read_image", {"room": room, "file_id": image_id})
        document = await client.call_tool("read_attachment", {"room": room, "file_id": docx_id})
        rejected = await client.call_tool("search_messages", {"room": "not-allowed"})
        result = {"tool_count": len(tools.tools), "message_count": len(records),
                  "attachment_count": len(files), "image_tool_ok": not image.is_error and any(c.type == "image" for c in image.content),
                  "docx_tool_ok": not document.is_error and bool(payload(document)["text"].strip()),
                  "room_isolation_ok": rejected.is_error}
        assert result["image_tool_ok"] and result["docx_tool_ok"] and result["room_isolation_ok"]
        (root / "reports").mkdir(exist_ok=True)
        (root / "reports" / "stdio_verification.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(json.dumps(result))


if __name__ == "__main__":
    asyncio.run(main())
