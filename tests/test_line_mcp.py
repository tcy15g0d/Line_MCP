import asyncio
import base64
import hashlib
import hmac
import json
import os
import sqlite3
import sys
from io import BytesIO
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from line_mcp import manage
from line_mcp.config import Config, new_config, save_raw
from line_mcp.exports import parse_export
from line_mcp.media_crypto import decrypt_media
from line_mcp.service import Service, safe_metadata, timestamp

ROOM = "Test Group"


@pytest.fixture
def environment(tmp_path):
    cache = tmp_path / "LINE" / "Cache"
    cache.mkdir(parents=True)
    docs = tmp_path / "files"
    docs.mkdir()
    db_path = tmp_path / "source.edb"
    db = sqlite3.connect(db_path)
    db.executescript('''
        CREATE TABLE _groupChat (_chatMid TEXT, _chatName TEXT);
        CREATE TABLE _message (_id TEXT, _chatId TEXT, _from TEXT, _createdTime INTEGER,
                               _text TEXT, _contentType INTEGER, _contentInfo TEXT, _contentMetadata TEXT);
        CREATE TABLE _contact (_mid TEXT PRIMARY KEY, _displayName TEXT);
    ''')
    db.executemany("INSERT INTO _groupChat VALUES (?,?)", [("room1", ROOM), ("room2", "Private other room")])
    db.execute("INSERT INTO _contact VALUES ('u1','Alice')")
    start = timestamp("2026-10-14T12:00:00+08:00")
    db.executemany("INSERT INTO _message VALUES (?,?,?,?,?,?,?,?)", [
        ("1", "room1", "u1", start, "MCP works", 0, "{}", "{}"),
        ("2", "room1", "u1", start + 1000, "中文第二則", 0, "{}", "{}"),
        ("3", "room2", "u1", start, "NEVER EXPOSE OTHER ROOM", 0, "{}", "{}"),
    ])
    db.commit()
    db.close()
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"line_root": str(tmp_path / "LINE"), "allowed_rooms": [ROOM],
        "auto_memory_key": False, "database": str(db_path), "snapshot_dir": "snapshots",
        "rooms": {ROOM: {"attachment_roots": [str(docs)], "exports": ["chat.txt"]}}}), encoding="utf-8")
    service = Service(Config(config_path))
    yield service, tmp_path, db_path
    service.close()


def test_room_isolation_and_source_unchanged(environment):
    service, root, source = environment
    before = source.read_bytes()
    result = service.messages(ROOM)
    assert len(result["messages"]) == 2
    assert result["messages"][0]["sender"] == "Alice"
    assert "NEVER EXPOSE" not in json.dumps(result)
    assert source.read_bytes() == before
    with pytest.raises(ValueError, match="allowed_rooms"):
        service.messages("Private other room")


def test_sender_name_fallbacks(environment):
    service, tmp_path, _ = environment
    db_path = tmp_path / "names.edb"
    db = sqlite3.connect(db_path)
    db.executescript('''
        CREATE TABLE _groupChat (_chatMid TEXT, _chatName TEXT);
        CREATE TABLE _message (_id TEXT, _chatId TEXT, _from TEXT, _createdTime INTEGER,
                               _text TEXT, _contentType INTEGER, _contentInfo TEXT, _contentMetadata TEXT);
        CREATE TABLE _contact (_mid TEXT PRIMARY KEY, _displayName TEXT, _targetProfileDetail TEXT);
        CREATE TABLE _profile (_mid TEXT, _displayName TEXT);
    ''')
    db.execute("INSERT INTO _groupChat VALUES ('room1', ?)", (ROOM,))
    db.executemany("INSERT INTO _contact VALUES (?,?,?)", [
        ("friend", "Friend", None),
        ("stranger", None, json.dumps({"profileName": "Stranger", "picturePath": "/x"})),
        ("broken", None, "not json"),
    ])
    db.execute("INSERT INTO _profile VALUES ('me', 'Myself')")
    db.executemany("INSERT INTO _message VALUES (?,?,?,?,?,?,?,?)", [
        (str(i), "room1", mid, i, mid, 0, "{}", "{}")
        for i, mid in enumerate(["friend", "stranger", "me", "broken", "unknown"])])
    db.commit()
    db.close()
    config = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    config["database"] = str(db_path)
    config_path = tmp_path / "names.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    named = Service(Config(config_path))
    try:
        senders = {m["text"]: m["sender"] for m in named.messages(ROOM)["messages"]}
    finally:
        named.close()
    assert senders == {"friend": "Friend", "stranger": "Stranger", "me": "Myself",
                       "broken": "broken", "unknown": "unknown"}


def test_parameterized_search_and_pagination(environment):
    service, _, _ = environment
    page = service.messages(ROOM, limit=1)
    assert page["has_more"] and page["next_offset"] == 1
    assert service.messages(ROOM, limit=1, offset=1)["messages"][0]["id"] == "1"
    assert service.messages(ROOM, query="' OR 1=1 --")["messages"] == []
    assert len(service.messages(ROOM, query="mcp")["messages"]) == 1
    assert len(service.messages(ROOM, until="2026-10-14T12:00:01+08:00")["messages"]) == 1


def test_ambiguous_group_fails_closed(environment):
    service, _, source = environment
    with sqlite3.connect(source) as db:
        db.execute("INSERT INTO _groupChat VALUES ('duplicate',?)", (ROOM,))
    assert not service.refresh()["database"]["connected"]
    assert service.messages(ROOM)["messages"] == []


def test_configured_group_id_resolves_duplicate(environment):
    service, _, source = environment
    with sqlite3.connect(source) as db:
        db.execute("INSERT INTO _groupChat VALUES ('duplicate',?)", (ROOM,))
    service.config.raw["rooms"][ROOM]["chat_id"] = "room1"
    assert service.refresh()["database"]["connected"]


def test_text_export_multiline_and_room_header(environment):
    _, root, _ = environment
    export = root / "chat.txt"
    export.write_text(f"[LINE] {ROOM}\n儲存日期 2026/10/14\n2026/10/14（三）\n上午 12:05\t小明\tHello\n第二行\n下午 01:00\t小華\t世界\n", encoding="utf-16")
    rows = parse_export(export, ROOM, 10000)
    assert len(rows) == 2
    assert rows[0]["text"] == "Hello\n第二行"
    assert rows[1]["timestamp_ms"] == timestamp("2026-10-14T13:00:00+08:00")
    with pytest.raises(ValueError, match="header"):
        parse_export(export, "Wrong room", 10000)


def test_metadata_does_not_expose_keys_or_tokens():
    safe = safe_metadata(json.dumps({"keyMaterial": "secret", "DOWNLOAD_URL": "token", "FILE_NAME": "a.pdf", "FILE_SIZE": "123"}))
    assert safe == {"FILE_NAME": "a.pdf", "FILE_SIZE": "123"}


def encrypt_test_media(data, video=False):
    material = b"x" * 32
    derived = HKDF(algorithm=hashes.SHA256(), length=76, salt=None, info=b"FileEncryption").derive(material)
    encryptor = Cipher(algorithms.AES(derived[:32]), modes.CTR(derived[64:] + b"\0" * 4)).encryptor()
    encrypted = encryptor.update(data) + encryptor.finalize()
    mac_data = b"".join(hashlib.sha256(encrypted[i:i+131072]).digest() for i in range(0, len(encrypted), 131072)) if video else encrypted
    tag = hmac.new(derived[32:64], mac_data, hashlib.sha256).digest()
    return encrypted + tag, base64.b64encode(material).decode()


@pytest.mark.parametrize("video", [False, True])
def test_media_authentication_and_tampering(video):
    plain = b"valid media" * 20000
    encrypted, material = encrypt_test_media(plain, video)
    assert decrypt_media(encrypted, material, video) == plain
    for index in (0, -1):
        damaged = bytearray(encrypted)
        damaged[index] ^= 1
        with pytest.raises(ValueError, match="authentication"):
            decrypt_media(bytes(damaged), material, video)


def test_linked_media_rejects_other_room_and_unapproved_path(environment):
    service, root, source = environment
    image = root / "LINE" / "Cache" / "thumb.eimg"
    encrypted, material = encrypt_test_media(b"\xff\xd8\xfftest jpeg")
    image.write_bytes(encrypted)
    info = json.dumps({"thumbPath": str(image), "fileName": "photo.jpg", "keyMaterial": material})
    with sqlite3.connect(source) as db:
        db.execute("INSERT INTO _message VALUES ('4','room1','u1',1,'',1,?,'{}')", (info,))
    result = service.attachments(ROOM)
    assert result["files"][0]["variant"] == "thumbnail"
    service.ensure_file(ROOM, "msg:4:thumbnail")
    data = service.files.read_bytes(ROOM, "msg:4:thumbnail", 0, 100)
    assert base64.b64decode(data["data"]) == b"\xff\xd8\xfftest jpeg"
    with pytest.raises(ValueError):
        service.ensure_file(ROOM, "msg:3:original")
    denied = service.files.register_linked(ROOM, "5", {"path": str(source)}, 14)
    assert denied[0]["readable"] is False
    with pytest.raises(ValueError):
        service.files.locate(ROOM, "msg:5:original")
    assert "keyMaterial" not in json.dumps(result)


def test_wal_snapshot_preserves_pending_rows(environment):
    service, _, source = environment
    db = sqlite3.connect(source)
    try:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA wal_autocheckpoint=0")
        db.execute("INSERT INTO _message VALUES ('wal','room1','u1',1,'WAL message',0,'{}','{}')")
        db.commit()
        before = Path(str(source) + "-wal").read_bytes()
        result = service.messages(ROOM, query="WAL message")
        assert result["messages"][0]["id"] == "wal"
        assert Path(str(source) + "-wal").read_bytes() == before
    finally:
        db.close()


def test_referenced_download_size_is_checked(environment):
    service, root, _ = environment
    path = root / "files" / "changed.txt"
    path.write_text("changed")
    items = service.files.register_linked(ROOM, "check", {"downloadedPath": str(path),
        "downloadedFileSize": 1, "fileName": "changed.txt"}, 14)
    assert items[0]["exists"]
    with pytest.raises(ValueError, match="size changed"):
        service.files.locate(ROOM, items[0]["file_id"])


def test_files_read_and_limits(environment):
    service, root, _ = environment
    (root / "files" / "example.txt").write_text("你好 MCP", encoding="utf-8")
    result = service.files.scan(ROOM)
    file_id = result["files"][0]["file_id"]
    assert service.files.read_text(ROOM, file_id, 0, 100)["text"] == "你好 MCP"
    assert base64.b64decode(service.files.read_bytes(ROOM, file_id, 0, 6)["data"]) == "你好".encode()
    with pytest.raises(ValueError):
        service.files.read_bytes(ROOM, file_id, 0, 262145)
    with pytest.raises(ValueError):
        service.files.locate(ROOM, "../../source.edb")


def test_office_extraction(environment):
    from docx import Document
    from openpyxl import Workbook
    service, root, _ = environment
    document = Document()
    document.add_paragraph("DOCX paragraph")
    document.add_table(rows=1, cols=1).cell(0, 0).text = "table cell"
    document.save(root / "files" / "sample.docx")
    workbook = Workbook()
    workbook.active.append(["XLSX cell", "=1+1"])
    workbook.save(root / "files" / "sample.xlsx")
    result = service.files.scan(ROOM)
    texts = {r["name"]: service.files.read_text(ROOM, r["file_id"], 0, 1000)["text"] for r in result["files"]}
    assert "table cell" in texts["sample.docx"]
    assert "XLSX cell\t=1+1" in texts["sample.xlsx"]


def test_directory_pagination_over_500_files(environment):
    service, root, _ = environment
    for i in range(503):
        (root / "files" / f"file{i:04}.txt").write_text("test")
    first = service.attachments(ROOM, limit=500)
    last = service.attachments(ROOM, limit=500, offset=500)
    assert len(first["files"]) == 500 and first["has_more"]
    assert len(last["files"]) == 3 and not last["has_more"]


def test_timezone():
    assert timestamp("2026-10-14") == timestamp("2026-10-14T00:00:00+08:00")
    with pytest.raises(ValueError):
        timestamp("bad date")


def test_real_stdio_protocol(environment):
    from mcp.client import Client
    from mcp.client.stdio import StdioServerParameters
    service, _, _ = environment

    async def run():
        params = StdioServerParameters(command=sys.executable,
            args=["-m", "line_mcp.server", "--config", str(service.config.path)],
            cwd=str(Path(__file__).resolve().parents[1]))
        async with Client(params, read_timeout_seconds=30) as client:
            tools = await client.list_tools()
            assert "search_messages" in [t.name for t in tools.tools]
            result = await client.call_tool("search_messages", {"room": ROOM, "query": "MCP"})
            assert not result.is_error
            serialized = result.model_dump_json()
            assert "MCP works" in serialized
            assert "NEVER EXPOSE" not in serialized
            rejected = await client.call_tool("search_messages", {"room": "Private other room"})
            assert rejected.is_error
    asyncio.run(run())


@pytest.fixture
def managed(environment):
    """A config with an empty allow-list pointing at the fixture database."""
    _, tmp_path, db_path = environment
    path = tmp_path / "managed.json"
    raw = new_config()
    raw.update({"line_root": str(tmp_path / "LINE"), "auto_memory_key": False,
                "database": str(db_path), "snapshot_dir": "snapshots"})
    save_raw(path, raw)
    return path


def test_empty_allow_list_is_rejected_for_server(managed):
    with pytest.raises(ValueError, match="line-local allow"):
        Config(managed)
    assert Config(managed, require_rooms=False).allowed == []


def test_rooms_lists_names_only_and_allow_disallow(managed):
    groups = manage.list_chats(Config(managed, require_rooms=False))
    assert {g["name"] for g in groups} == {ROOM, "Private other room"}
    assert not any(g["allowed"] for g in groups)
    assert "NEVER EXPOSE" not in json.dumps(groups)

    assert manage.allow_room(managed, ROOM)["allowed_rooms"] == [ROOM]
    assert manage.allow_room(managed, ROOM)["allowed_rooms"] == [ROOM]  # idempotent
    service = Service(Config(managed))
    try:
        assert len(service.messages(ROOM)["messages"]) == 2
        with pytest.raises(ValueError, match="allowed_rooms"):
            service.messages("Private other room")
    finally:
        service.close()
    assert any(g["allowed"] for g in manage.list_chats(Config(managed, require_rooms=False)))

    assert manage.disallow_room(managed, ROOM)["allowed_rooms"] == []
    with pytest.raises(ValueError, match="不在允許名單"):
        manage.disallow_room(managed, ROOM)


def test_allow_rejects_unknown_and_requires_chat_id_for_duplicates(managed, environment):
    _, _, db_path = environment
    with pytest.raises(ValueError, match="找不到群組"):
        manage.allow_room(managed, "Does not exist")
    db = sqlite3.connect(db_path)
    db.execute("INSERT INTO _groupChat VALUES ('room3', ?)", (ROOM,))
    db.commit()
    db.close()
    with pytest.raises(ValueError, match="--chat-id"):
        manage.allow_room(managed, ROOM)
    with pytest.raises(ValueError, match="不相符"):
        manage.allow_room(managed, ROOM, "wrong")
    manage.allow_room(managed, ROOM, "room3")
    assert Config(managed).room(ROOM)["chat_id"] == "room3"


def test_init_does_not_overwrite_and_client_config(tmp_path):
    path = tmp_path / "nested" / "config.json"
    manage.init_config(path)
    assert json.loads(path.read_text(encoding="utf-8"))["allowed_rooms"] == []
    with pytest.raises(ValueError, match="--force"):
        manage.init_config(path)
    manage.init_config(path, force=True)
    server = json.loads(manage.client_config("json", path))["mcpServers"]["line_local"]
    assert server["args"][-1] == str(path.resolve()) and server["command"] == str(Path(sys.executable).resolve())
    assert str(path.resolve()) in manage.client_config("claude-code", path)
    assert "[mcp_servers.line_local]" in manage.client_config("codex", path)
    with pytest.raises(ValueError):
        manage.client_config("unknown", path)


@pytest.fixture
def with_friends(tmp_path):
    """A database holding a group, friend chats, and a friend named like the group."""
    db_path = tmp_path / "friends.edb"
    db = sqlite3.connect(db_path)
    db.executescript('''
        CREATE TABLE _groupChat (_chatMid TEXT, _chatName TEXT);
        CREATE TABLE _chat (_id TEXT, _lastUpdatedTime INTEGER);
        CREATE TABLE _message (_id TEXT, _chatId TEXT, _from TEXT, _createdTime INTEGER,
                               _text TEXT, _contentType INTEGER, _contentInfo TEXT, _contentMetadata TEXT);
        CREATE TABLE _contact (_mid TEXT PRIMARY KEY, _displayName TEXT, _displayNameOverridden TEXT,
                               _targetProfileDetail TEXT);
    ''')
    db.execute("INSERT INTO _groupChat VALUES ('c1', 'Team')")
    db.executemany("INSERT INTO _chat VALUES (?,?)", [("c1", 5), ("ualice", 9), ("ubob", 8), ("ubob2", 7), ("unick", 6), ("uteam", 4)])
    db.executemany("INSERT INTO _contact VALUES (?,?,?,?)", [
        ("ualice", "Alice", "", None),
        ("ubob", "Bob", "", None), ("ubob2", "Bob", "", None),
        ("unick", "Real Name", "Nickname", None),
        ("uteam", "Team", "", None),
        ("unchatted", "Never Chatted", "", None),
    ])
    db.executemany("INSERT INTO _message VALUES (?,?,?,?,?,?,?,?)", [
        ("1", "ualice", "ualice", 1, "hello from alice", 0, "{}", "{}"),
        ("2", "ubob", "ubob", 2, "bob private", 0, "{}", "{}"),
        ("3", "c1", "ualice", 3, "group text", 0, "{}", "{}"),
    ])
    db.commit()
    db.close()
    path = tmp_path / "friends.json"
    raw = new_config()
    raw.update({"line_root": str(tmp_path / "LINE"), "auto_memory_key": False,
                "database": str(db_path), "snapshot_dir": "snapshots"})
    save_raw(path, raw)
    return path


def test_friend_chats_are_listed_by_kind_and_search(with_friends):
    config = Config(with_friends, require_rooms=False)
    kinds = {(c["kind"], c["name"]) for c in manage.list_chats(config)}
    assert ("group", "Team") in kinds and ("friend", "Alice") in kinds
    assert ("friend", "Nickname") in kinds and ("friend", "Real Name") not in kinds  # nickname wins
    assert not any(name == "Never Chatted" for _, name in kinds)  # no chat, not listed
    assert {c["kind"] for c in manage.list_chats(config, "group")} == {"group"}
    assert [c["name"] for c in manage.list_chats(config, "friend", "ali")] == ["Alice"]
    team = [c for c in manage.list_chats(config, "friend") if c["name"] == "Team"][0]
    assert team["same_name_count"] == 2


def test_allow_friend_isolates_other_chats(with_friends):
    result = manage.allow_room(with_friends, "Alice")
    assert result["kind"] == "friend"
    service = Service(Config(with_friends))
    try:
        texts = [m["text"] for m in service.messages("Alice")["messages"]]
        assert texts == ["hello from alice"]  # not the group message Alice also sent
        with pytest.raises(ValueError, match="allowed_rooms"):
            service.messages("Bob")
    finally:
        service.close()


def test_same_name_group_and_friend_fail_closed(with_friends):
    with pytest.raises(ValueError, match="--chat-id"):
        manage.allow_room(with_friends, "Team")
    with pytest.raises(ValueError, match="--chat-id"):
        manage.allow_room(with_friends, "Bob")
    # An old allow-list entry that later becomes ambiguous must stop resolving, not guess.
    raw = json.loads(with_friends.read_text(encoding="utf-8"))
    raw["allowed_rooms"] = ["Team"]
    with_friends.write_text(json.dumps(raw), encoding="utf-8")
    service = Service(Config(with_friends))
    try:
        assert service.messages("Team")["messages"] == []
    finally:
        service.close()
    manage.allow_room(with_friends, "Team", "c1")
    service = Service(Config(with_friends))
    try:
        assert [m["text"] for m in service.messages("Team")["messages"]] == ["group text"]
    finally:
        service.close()


def scripted(*answers):
    """An `ask` that replays answers and records what the user would have seen."""
    queue = list(answers)
    return lambda prompt="": queue.pop(0)


def test_interactive_pick_confirms_and_pins_chat_id(with_friends):
    shown = []
    result = manage.allow_interactive(with_friends, ask=scripted("ali", "1", "y"), say=shown.append)
    assert result["allowed_rooms"] == ["Alice"] and result["kinds"] == ["friend"]
    assert Config(with_friends).room("Alice")["chat_id"] == "ualice"  # pinned by ID
    assert any("提醒" in line and "私密" in line for line in shown)  # friend notice
    service = Service(Config(with_friends))
    try:
        assert [m["text"] for m in service.messages("Alice")["messages"]] == ["hello from alice"]
    finally:
        service.close()


def test_interactive_pick_cancel_leaves_allow_list_unchanged(with_friends):
    for answers in (("", ""), ("ali", ""), ("ali", "1", "n"), ("ali", "1", "")):
        assert manage.allow_interactive(with_friends, ask=scripted(*answers), say=lambda _: None) is None
    assert Config(with_friends, require_rooms=False).allowed == []
    with pytest.raises(ValueError, match="編號"):
        manage.allow_interactive(with_friends, ask=scripted("ali", "9"), say=lambda _: None)
    with pytest.raises(ValueError, match="編號"):
        manage.allow_interactive(with_friends, ask=scripted("ali", "abc"), say=lambda _: None)


def test_interactive_default_lists_groups_only(with_friends):
    shown = []
    manage.allow_interactive(with_friends, ask=scripted("", ""), say=shown.append)
    listing = "\n".join(shown)
    assert "Team" in listing and "Alice" not in listing  # friends need an explicit search


def test_same_name_chats_cannot_both_be_allowed(with_friends):
    manage.allow_room(with_friends, "Team", "c1")
    with pytest.raises(ValueError, match="同名"):
        manage.allow_room(with_friends, "Team", "uteam")
    assert Config(with_friends).room("Team")["chat_id"] == "c1"


@pytest.fixture
def with_community(with_friends, tmp_path):
    db = sqlite3.connect(tmp_path / "friends.edb")
    db.executescript('''
        CREATE TABLE _square (_mid TEXT, _name TEXT);
        CREATE TABLE _squareChat (_squareChatMid TEXT, _squareMid TEXT, _name TEXT);
        CREATE TABLE _squareMember (_squareMemberMid TEXT, _displayName TEXT);
    ''')
    db.executemany("INSERT INTO _square VALUES (?,?)", [("s1", "Open Club"), ("s2", "Solo")])
    db.executemany("INSERT INTO _squareChat VALUES (?,?,?)", [
        ("mgen", "s1", "General"), ("mdev", "s1", "Dev"), ("msolo", "s2", "Solo"), ("mnone", "s1", "No Messages")])
    db.executemany("INSERT INTO _squareMember VALUES (?,?)", [("pmember", "Stranger One")])
    db.executemany("INSERT INTO _chat VALUES (?,?)", [("mgen", 3), ("mdev", 2), ("msolo", 1)])
    db.executemany("INSERT INTO _message VALUES (?,?,?,?,?,?,?,?)", [
        ("10", "mgen", "pmember", 1, "welcome all", 0, "{}", "{}"),
        ("11", "mdev", "pother", 2, "dev only", 0, "{}", "{}")])
    db.commit()
    db.close()
    return with_friends


def test_community_names_listing_and_isolation(with_community):
    config = Config(with_community, require_rooms=False)
    names = {c["name"] for c in manage.list_chats(config, "community")}
    assert names == {"Open Club / General", "Open Club / Dev", "Solo"}  # chat name omitted when it equals the community
    assert manage.allow_room(with_community, "Open Club / General")["kind"] == "community"
    service = Service(Config(with_community))
    try:
        messages = service.messages("Open Club / General")["messages"]
        assert [m["text"] for m in messages] == ["welcome all"]
        assert messages[0]["sender"] == "Stranger One"  # resolved through _squareMember
        with pytest.raises(ValueError, match="allowed_rooms"):
            service.messages("Open Club / Dev")
    finally:
        service.close()


def test_allow_with_picker_commits_only_new_confirmed_chats(with_community):
    seen = {}

    def pick(chats, initial):
        seen["kinds"] = {c["kind"] for c in chats}
        seen["initial"] = initial
        return [c for c in chats if c["name"] in ("Alice", "Open Club / General")]

    result = manage.allow_with_picker(with_community, pick, "friend")
    assert seen == {"kinds": {"group", "friend", "community"}, "initial": "friend"}
    assert sorted(result["allowed_rooms"]) == ["Alice", "Open Club / General"]
    assert Config(with_community).room("Alice")["chat_id"] == "ualice"
    assert manage.allow_with_picker(with_community, lambda chats, initial: None) is None  # cancelled
    assert sorted(Config(with_community).allowed) == ["Alice", "Open Club / General"]


def run_picker(keys, limit=10):
    """Drive the real Textual picker headlessly with key presses."""
    import asyncio
    from line_mcp.picker import ChatPicker

    def chat(kind, name, chat_id, allowed=False):
        return {"kind": kind, "name": name, "chat_id": chat_id, "allowed": allowed,
                "same_name_count": 1, "last_activity": "2026-10-14T13:36+08:00"}

    chats = [chat("group", "Team [beta]", "c1"), chat("group", "Study", "c2", allowed=True),
             chat("friend", "Alice", "ua"), chat("friend", "Bob", "ub"), chat("friend", "Alicia", "uc"),
             chat("community", "Open Club", "m1")]

    async def drive():
        app = ChatPicker(chats, manage.KIND_LABELS, manage.KIND_NOTICES, "group", limit)
        async with app.run_test(size=(110, 32)) as pilot:
            for key in keys:
                await pilot.press(key)
                await pilot.pause()
            tabs = app.query_one("TabbedContent")
            state = {"tab": tabs.active, "selected": sorted(app.selected), "screen": type(app.screen).__name__,
                     "labels": [str(tabs.get_tab(k).label) for k in ("group", "friend", "community")]}
        return state, app.return_value

    return asyncio.run(drive())


def test_tui_tabs_selection_and_confirmation():
    pytest.importorskip("textual")
    state, _ = run_picker([])
    assert state["tab"] == "group" and state["labels"] == ["群組 (2)", "好友 (3)", "社群 (1)"]
    assert run_picker(["tab"])[0]["tab"] == "friend"
    assert run_picker(["tab", "tab", "tab"])[0]["tab"] == "group"      # wraps around
    assert run_picker(["shift+tab"])[0]["tab"] == "community"
    assert run_picker(["space"])[0]["selected"] == ["c1"]               # first item is highlighted at start
    assert run_picker(["down", "space"])[0]["selected"] == ["c1"]       # already-allowed "Study" is skipped
    assert run_picker(["space", "tab", "space", "tab", "space"])[0]["selected"] == ["c1", "m1", "ua"]  # across tabs


def test_tui_search_keeps_selection_and_typing_is_not_a_command():
    pytest.importorskip("textual")
    state, _ = run_picker(["tab", "space", "slash", "q", "1", "enter"])  # "q" and "1" typed into search
    assert state["selected"] == ["ua"] and state["labels"][1] == "好友 (0)"
    state, _ = run_picker(["tab", "slash", "a", "l", "i", "enter", "space"])
    assert state["selected"] == ["ua"] and state["labels"][1] == "好友 (2)"


def test_tui_only_explicit_y_confirms():
    pytest.importorskip("textual")
    assert run_picker(["enter"])[0]["screen"] != "ConfirmScreen"        # nothing selected yet
    assert run_picker(["space", "enter"])[0]["screen"] == "ConfirmScreen"
    state, result = run_picker(["space", "enter", "enter"])             # Enter must not confirm
    assert state["screen"] == "ConfirmScreen" and result is None
    state, result = run_picker(["space", "enter", "n"])
    assert state["screen"] != "ConfirmScreen" and result is None
    _, result = run_picker(["space", "enter", "y"])
    assert [c["name"] for c in result] == ["Team [beta]"]               # brackets in a name are literal
    assert run_picker(["q"])[1] is None


def test_tui_limits_how_many_can_be_picked_at_once():
    pytest.importorskip("textual")
    assert run_picker(["tab", "space", "down", "space", "down", "space"], limit=2)[0]["selected"] == ["ua", "ub"]
