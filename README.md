# LINE Local MCP

讓支援 MCP 的 AI 用戶端（Claude Code、Codex 等）**唯讀**存取你自己電腦上 Windows 版 LINE 的指定群組或好友聊天：搜尋聊天文字、列出附件、讀取 Word／PDF／Excel 文字，並把可驗證的加密圖片縮圖解密後交給支援圖片的用戶端。

> **使用前請先閱讀「隱私與責任」。** 這是非官方工具，只讀取你自己登入中的 LINE 在本機留下的資料。

## 快速開始

需求：Windows、Python 3.11 以上、已登入的 LINE 桌面版。

```powershell
git clone https://github.com/tcy15g0d/Line_MCP.git
Set-Location Line_MCP
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
```

之後的指令都用 `line-local`（在虛擬環境內，路徑為 `.\.venv\Scripts\line-local.exe`）：

```powershell
line-local init                                   # 1. 建立設定檔，允許名單一開始是空的
line-local rooms                                  # 2. 列出你的群組名稱（不讀訊息）；好友聊天用 --type friend
line-local allow --room "你的群組名稱"             # 3. 只允許你選的聊天室
line-local check                                  # 4. 確認可以接通
line-local mcp-config --client claude-code        # 5. 產生這台電腦的 MCP 設定
```

第 5 步會印出可直接貼上的內容。`--client` 可選 `claude-code`、`codex`、`json`（通用 `mcpServers` 設定）。執行 Claude Code 的指令後，重新啟動用戶端連線即可使用。

接著可以對 AI 說：

> 使用 line_local，更新資料並整理「我的群組名稱」今天的重點，列出附件。圖片請標示原檔或縮圖，文件請讀取內容。

LINE 要保持登入。程式會從目前使用者的 LINE 程序記憶體找出資料庫金鑰，不需要你的 LINE 密碼。

## 允許名單

| 指令 | 功能 |
|---|---|
| `line-local rooms` | 列出群組名稱、最後活動時間與是否已允許（不讀訊息） |
| `line-local rooms --type friend` | 改列一對一好友聊天；`--type all` 列出全部 |
| `line-local rooms --search 文字` | 只列出名稱含該文字的聊天室，好友很多時很好用 |
| `line-local allow --room 名稱` | 加入允許名單；名稱需完全相符 |
| `line-local allow --room 名稱 --chat-id ID` | 有同名時，指定其中一個 |
| `line-local disallow --room 名稱` | 從名單移除 |

- **預設一個群組都不開放。** 沒有允許任何群組時，MCP 伺服器會拒絕啟動。
- 名單存在**每個人自己電腦上**的設定檔，不在程式碼裡，也不會被 git 提交。預設位置依序為：環境變數 `LINE_MCP_CONFIG`、目前資料夾的 `config.local.json`、`%APPDATA%\line-local-mcp\config.json`。
- 變更名單後，需重新啟動 MCP 用戶端連線才會生效。
- 名單外的聊天室，MCP 無法搜尋、列出或讀取附件，AI 也無法用工具列出你的其他群組或好友；`rooms` 只存在於命令列。
- 支援**群組**與**一對一好友聊天**。好友聊天以你在 LINE 看到的名稱（你設定的暱稱優先）辨識；多人小聊天室、社群與官方帳號不在支援範圍。
- 群組與好友共用同一個名稱空間：名稱只要對到不只一個聊天室（例如好友與群組同名、兩位好友同名），就會**拒絕接通**，必須用 `--chat-id` 指定。
- 好友聊天預設不會列出，需明確使用 `--type friend` 或 `--type all`。

## 隱私與責任

- 所有處理都在本機：用 stdio 與用戶端溝通，不開 HTTP 服務、不發送 LINE 訊息、不呼叫遠端 LINE API。
- 但用戶端呼叫工具後，回傳的訊息與檔案內容會成為該 **AI 服務的輸入**。聊天室裡有其他人的發言，請確認你有權限、也適合把這些內容交給 AI 處理。建議只開放必要的聊天室。
- **一對一好友聊天通常比群組更私密**，對方也沒有同意讓 AI 讀取。請特別審慎，並只開放你確實需要處理的對象。
- 這個工具讀取 LINE 的本機加密資料庫，並從 LINE 程序記憶體取得金鑰。請只用在**你自己的帳號與電腦**，不要用來讀取他人的資料。是否符合 LINE 的服務條款，請自行判斷。
- 本專案與 LINE 公司無關，LINE 更新後資料格式可能改變而失效。
- **防毒軟體可能誤報。** 程式會唯讀搜尋你自己 `LINE.exe` 程序的記憶體來取得資料庫金鑰，這個行為與惡意程式竊取憑證的手法相似，部分防毒或端點防護軟體可能警告或封鎖。請只安裝來源可信、你已檢視過原始碼的版本；相關邏輯集中在 [windows_keys.py](src/line_mcp/windows_keys.py)，只在 `auto_memory_key` 為 `true` 時執行。若不想讓程式讀取記憶體，可把它設為 `false`，改用環境變數 `LINE_DB_KEY` 自行提供金鑰。
- 程式不會回傳資料庫金鑰、媒體金鑰、下載 token 或完整原始附件 metadata。不要把金鑰寫進 repo 或用戶端的公開設定；若要自行提供，可用環境變數 `LINE_DB_KEY`（32 字元十六進位）。
- 訊息與檔案內容視為不可信資料，MCP 的指示會要求 AI 不要把它們當成指令執行。

## 工具

| 工具 | 功能 |
|---|---|
| `diagnose_line_storage` | 檢查 LINE 路徑、資料庫和目前接通狀態 |
| `refresh_data` | 重新建立加密 DB／WAL 副本，更新允許的群組 |
| `list_rooms` | 列出允許讀取的群組與來源 |
| `search_messages` | 搜尋文字，支援日期範圍與分頁，並盡量顯示發送者名稱 |
| `list_attachment_messages` | 列出非文字訊息，包括附件、貼圖和系統事件 |
| `list_attachments` | 列出群組訊息指向的檔案、縮圖和可用狀態 |
| `read_attachment` | 讀取 TXT／CSV／JSON／PDF／DOCX／XLSX 的文字 |
| `read_image` | 回傳圖片內容；有媒體金鑰的快取須先通過 HMAC 驗證 |
| `read_file_bytes` | 分段讀取檔案 bytes，包括音訊與影片；每段最多 256 KiB |

訊息依時間由新到舊排序。`since` 包含起點，`until` 不包含終點；無時區的 ISO 日期使用 `Asia/Taipei`。檔案閱讀使用 `list_attachments` 回傳的 `file_id`，不能傳入任意檔案路徑。

發送者名稱依序取自：好友名稱、非好友成員的公開暱稱、你自己的個人檔案；都沒有時顯示 LINE 內部 ID。

## 設定檔欄位

範本是 [config.example.json](config.example.json)，`line-local init` 會建立同樣內容。

- `allowed_rooms`：明確允許的群組名稱。
- `rooms.<名稱>.chat_id`：有同名群組時指定其 ID；沒有唯一對應時不接通。
- `auto_memory_key`：是否唯讀搜尋你的 LINE 程序中的歷史金鑰標記。只辨識 `%LOCALAPPDATA%/LINE/bin` 下的 `LINE.exe`，搜尋有時間與大小上限，不建立記憶體 dump，也不輸出金鑰。
- `database`：可指定資料庫；`null` 時在 `Data/db/qw*.edb` 尋找能解密且包含允許群組的資料庫。
- `linked_file_roots`：可讀取「已由允許群組訊息記錄指向」的附件路徑，預設為 Downloads、Documents 和 Desktop。不會掃描這些資料夾的所有檔案。
- `rooms.<名稱>.attachment_roots`：另外由你確認屬於該群組的附件資料夾；會掃描清單。
- `rooms.<名稱>.cache_roots`：手動確認的群組快取資料夾，通常不需要。
- `rooms.<名稱>.exports`：LINE 匯出的聊天文字檔，資料庫不可用時作為備援。檔案開頭必須含群組名稱。
- `snapshot_dir`：資料庫副本的暫存位置，相對路徑以設定檔所在資料夾為基準。

資料庫只在暫存目錄建立加密副本；LINE 原始資料庫只用檔案讀取方式複製。DB 與 WAL 複製前後會比對檔案狀態，有變動便重試或拒絕使用。這是穩定檔案複製的最佳努力快照，並非 SQLite 線上備份的交易一致性保證。LINE 正在大量寫入時，請等它閒置後再更新。資料庫可能達數 GB，更新需要等量的暫存空間；正常關閉會清理副本，強制中止可能留下暫存檔。

## 限制

- 只有本機現存的內容。過期、未下載、尚未同步的附件無法恢復；要先在 LINE 裡點開檔案下載，再執行 `refresh_data`。
- 掃描型 PDF 沒有可抽取文字，需要 OCR；音訊與影片可取得 bytes，但沒有內建轉錄。圖片可交給支援圖片輸入的用戶端分析。
- 圖片原檔與縮圖可能屬於同一則訊息，不能把兩者數量相加當成圖片訊息數。
- 本專案不能觸發 LINE 下載檔案或發送訊息。

## 疑難排解

- **「No database key found」**：確認 LINE 桌面版已登入並開啟過，且 `auto_memory_key` 為 `true`。
- **「LINE changed its database during copying」**：LINE 正在寫入，稍候再試。
- **找不到群組或好友**：名稱需與 `line-local rooms` 顯示的完全相符，包含空白與符號。
- **明明允許了卻讀不到**：可能有同名的聊天室，導致名稱不唯一而被拒絕；用 `rooms` 查看「同名」標記，再以 `--chat-id` 指定。
- **工具沒有出現**：確認已重新啟動用戶端連線，並用 `line-local mcp-config` 產生的路徑設定。
- 用 `line-local diagnose` 可檢查 LINE 路徑與資料庫狀態。

## 研究背景

1. **直接讀本機加密資料庫**：歷史研究描述了 Windows LINE 的 wxSQLite3 AES-128 資料庫及記憶體金鑰標記。本專案使用 `apsw-sqlite3mc`，並未假設所有 LINE 版本皆相同。[研究論文](https://doi.org/10.3390/electronics13071325)、[SQLite 封裝套件](https://github.com/utelle/apsw-sqlite3mc)
2. **讀取本機媒體**：訊息的 `_contentInfo` 包含媒體 key material 和快取路徑。使用 HKDF／AES-CTR 解密並驗證 HMAC；協定參考 Beeper 的 LINE 實作，本專案自行實作且加入完整性驗證。[媒體協定程式碼](https://github.com/beeper/line/blob/main/pkg/connector/media.go)
3. **LINE 官方匯出**：聊天室選單的「儲存聊天」只匯出文字，不含附件原檔。[LINE 支援說明](https://help.line.me/line/smartphone?contentId=20007388&lang=zh-Hant)
4. **Messaging API**：供官方帳號以 webhook 接收訊息；不能用它取得個人帳號既有的聊天紀錄。[LINE Developers](https://developers.line.biz/en/docs/messaging-api/receiving-messages/)

## 授權

以 [MIT 授權](LICENSE) 釋出。軟體依現狀提供，不附任何保證；使用風險與資料責任由使用者自行承擔。

## 開發與驗證

```powershell
.\.venv\Scripts\python.exe -m pip install -e '.[dev]'
.\.venv\Scripts\python.exe -m pytest -q
```

自動測試涵蓋群組隔離、同名群組、允許名單管理、WAL 副本、分頁、文字匯出、Office 內容、下載檔大小變更、媒體 HMAC 竄改拒絕、發送者名稱解析與真正的 stdio MCP 呼叫。

下列腳本會對你設定檔中第一個允許的群組做實測，只輸出數量與狀態，不輸出訊息、文件內容或金鑰：

```powershell
.\.venv\Scripts\python.exe scripts\verify_local.py
.\.venv\Scripts\python.exe scripts\verify_stdio.py
```
