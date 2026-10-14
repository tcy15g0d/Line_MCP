"""Full-screen, tabbed allow-list editor (optional: needs `pip install "line-local-mcp[tui]"`).

Ticked = currently allowed. Ticking adds a chat, unticking removes it. Pure UI: it receives chat
names/IDs from the caller and returns what to add and remove. It never reads messages and never
writes the allow-list; the caller does that after the user confirms.
"""
from __future__ import annotations

try:
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Vertical
    from textual.content import Content
    from textual.screen import ModalScreen
    from textual.widgets import Footer, Input, SelectionList, Static, TabbedContent, TabPane
    from textual.widgets.selection_list import Selection
except ImportError:  # the numbered fallback in manage.py is used instead
    App = None


def available() -> bool:
    return App is not None


TABS = (("group", "群組"), ("friend", "好友"), ("community", "社群"))
HELP = "\n".join((
    "按鍵　↑ ↓ 移動　｜　空白鍵 勾選／取消勾選　｜　Tab／Shift+Tab（或 1 2 3）切換分頁",
    "　　　/ 搜尋（Enter 或 Esc 回到清單）　｜　Enter 檢視變更並確認　｜　q 不儲存離開",
    "說明　☑ 打勾＝目前已允許。勾選沒打勾的＝新增；取消已打勾的＝移除。",
    "　　　按 Enter 只會顯示變更，在確認畫面按 y 才會真正儲存。",
))

if App is not None:

    class ConfirmScreen(ModalScreen[bool]):
        """Last look at exactly what will be opened. Only an explicit `y` confirms."""

        BINDINGS = [Binding("y", "answer(True)", "確認寫入"), Binding("n", "answer(False)", "返回"),
                    Binding("escape", "answer(False)", "返回", show=False)]
        DEFAULT_CSS = """
        ConfirmScreen { align: center middle; }
        ConfirmScreen > Vertical { width: 76; max-height: 80%; height: auto; border: thick $warning;
                                   background: $surface; padding: 1 2; }
        """

        def __init__(self, added: list[dict], removed: list[dict], remaining: int,
                     labels: dict[str, str], notices: dict[str, str]):
            super().__init__()
            self.added, self.removed, self.remaining = added, removed, remaining
            self.labels, self.notices = labels, notices

        def compose(self) -> ComposeResult:
            with Vertical():
                if self.added:
                    yield Static(Content("即將開放給 AI 讀取：\n"))
                    for chat in self.added:
                        yield Static(Content(f"  + [{self.labels[chat['kind']]}] {chat['name']}"))
                    for notice in dict.fromkeys(self.notices[c["kind"]] for c in self.added if c["kind"] in self.notices):
                        yield Static(Content("\n提醒：" + notice))
                if self.removed:
                    yield Static(Content(("\n" if self.added else "") + "即將移除（AI 之後讀不到）：\n"))
                    for chat in self.removed:
                        yield Static(Content(f"  - [{self.labels[chat['kind']]}] {chat['name']}"))
                if not self.remaining:
                    yield Static(Content("\n注意：名單將變成空的，MCP 伺服器在重新加入聊天室前無法啟動。"))
                yield Static(Content("\n按 y 確認寫入　按 n 返回繼續選"))
            yield Footer()

        def action_answer(self, yes: bool) -> None:
            self.dismiss(yes)

    class ChatPicker(App[dict]):
        TITLE = "選擇要允許的聊天室"
        CSS = """
        #help { border: round $primary; color: $text; padding: 0 1; margin: 0 1; height: auto; }
        #search { margin: 0 1; }
        #status { padding: 0 1; color: $success; }
        SelectionList { height: 1fr; }
        """
        BINDINGS = [
            Binding("tab", "tab(1)", "下一分頁", priority=True),
            Binding("shift+tab", "tab(-1)", "上一分頁", priority=True),
            Binding("enter", "review", "檢視並確認", priority=True),
            Binding("slash", "search", "搜尋"),
            Binding("escape", "escape", "回清單", show=False),
            Binding("1", "goto('group')", show=False),
            Binding("2", "goto('friend')", show=False),
            Binding("3", "goto('community')", show=False),
            Binding("q", "quit_picker", "離開"),
            Binding("ctrl+c", "quit_picker", show=False),
        ]

        def __init__(self, chats: list[dict], labels: dict[str, str], notices: dict[str, str],
                     initial: str = "group", limit: int = 10):
            super().__init__()
            self.chats = chats
            self.by_id = {c["chat_id"]: c for c in chats}
            self.labels, self.notices, self.limit = labels, notices, limit
            self.initial = initial if initial in dict(TABS) else "group"
            self.original = {c["chat_id"] for c in chats if c["allowed"]}  # the current allow-list
            self.selected = set(self.original)  # ticked chat IDs, kept across tabs and searches

        @property
        def added(self) -> list[str]:
            return [c["chat_id"] for c in self.chats if c["chat_id"] in self.selected - self.original]

        @property
        def removed(self) -> list[str]:
            return [c["chat_id"] for c in self.chats if c["chat_id"] in self.original - self.selected]

        def compose(self) -> ComposeResult:
            yield Static(Content(HELP), id="help")
            yield Input(placeholder="按 / 輸入關鍵字篩選（Enter 或 Esc 回到清單）", id="search")
            with TabbedContent(initial=self.initial):
                for kind, label in TABS:
                    with TabPane(label, id=kind):
                        yield SelectionList[str](id=f"list-{kind}")
            yield Static("", id="status")
            yield Footer()

        def on_mount(self) -> None:
            self._refill()
            self._focus_list()

        # -- rendering -------------------------------------------------------------------
        def _refill(self) -> None:
            term = self.query_one("#search", Input).value.strip().casefold()
            for kind, label in TABS:
                matches = [c for c in self.chats if c["kind"] == kind and term in c["name"].casefold()]
                widget = self.query_one(f"#list-{kind}", SelectionList)
                widget.clear_options()
                widget.add_options([
                    Selection(Content(self._line(c)), c["chat_id"], c["chat_id"] in self.selected)
                    for c in matches])
                widget.highlighted = 0 if matches else None  # so Space works without pressing ↓ first
                self.query_one(TabbedContent).get_tab(kind).label = f"{label} ({len(matches)})"
            self._status()

        @staticmethod
        def _line(chat: dict) -> str:
            text = f"{chat['name']}  （最後活動 {(chat['last_activity'] or '未知')[:16]}）"
            if chat["same_name_count"] > 1:
                text += "  [同名多個]"
            return text

        def _status(self) -> None:
            parts = [f"目前允許 {len(self.selected)} 個"]
            if self.added:
                parts.append(f"新增 {len(self.added)}/{self.limit}：" + "、".join(self.by_id[i]["name"] for i in self.added))
            if self.removed:
                parts.append(f"移除 {len(self.removed)}：" + "、".join(self.by_id[i]["name"] for i in self.removed))
            self.query_one("#status", Static).update(Content("　".join(parts)))

        def _active_list(self) -> SelectionList:
            return self.query_one(f"#list-{self.query_one(TabbedContent).active}", SelectionList)

        def _focus_list(self) -> None:
            self._active_list().focus()

        # -- events ------------------------------------------------------------------------
        def on_selection_list_selection_toggled(self, event: SelectionList.SelectionToggled) -> None:
            chat_id = event.selection.value
            if chat_id in event.selection_list.selected:
                if chat_id not in self.original and len(self.added) >= self.limit:
                    event.selection_list.deselect(chat_id)
                    self.notify(f"一次最多新增 {self.limit} 個，請先確認這一批", severity="warning")
                    return
                self.selected.add(chat_id)
            else:
                self.selected.discard(chat_id)
            self._status()

        def on_input_changed(self, event: Input.Changed) -> None:
            self._refill()

        def on_input_submitted(self, event: Input.Submitted) -> None:
            self._focus_list()

        def on_tabbed_content_tab_activated(self, event: TabbedContent.TabActivated) -> None:
            if not self.query_one("#search", Input).has_focus:
                self.call_after_refresh(self._focus_list)

        # -- actions -----------------------------------------------------------------------
        def action_tab(self, step: int) -> None:
            kinds = [k for k, _ in TABS]
            tabs = self.query_one(TabbedContent)
            tabs.active = kinds[(kinds.index(tabs.active) + step) % len(kinds)]

        def action_goto(self, kind: str) -> None:
            self.query_one(TabbedContent).active = kind

        def action_search(self) -> None:
            self.query_one("#search", Input).focus()

        def action_escape(self) -> None:
            if self.query_one("#search", Input).has_focus:
                self._focus_list()

        def action_review(self) -> None:
            if self.query_one("#search", Input).has_focus:
                self._focus_list()
                return
            added = [self.by_id[i] for i in self.added]
            removed = [self.by_id[i] for i in self.removed]
            if not added and not removed:
                self.notify("名單沒有變更，用空白鍵勾選要新增的，或取消勾選要移除的", severity="warning")
                return
            self.push_screen(ConfirmScreen(added, removed, len(self.selected), self.labels, self.notices),
                             lambda yes: self.exit({"add": added, "remove": removed}) if yes else None)

        def action_quit_picker(self) -> None:
            if self.query_one("#search", Input).has_focus:
                return  # "q" is just a letter while typing a search
            self.exit(None)


def pick_chats(chats: list[dict], labels: dict[str, str], notices: dict[str, str],
               initial: str = "group", limit: int = 10) -> dict | None:
    """Run the editor. Returns {"add": [...], "remove": [...]} once confirmed, or None when the user quit."""
    if App is None:
        raise RuntimeError("textual is not installed")
    return ChatPicker(chats, labels, notices, initial, limit).run()
