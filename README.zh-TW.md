# ZJU LearnPilot

[简体中文](README.md) | 繁體中文

面向浙江大學課程的學習助手：作為 Agent Skill，結合本地轉寫、課件與錄播畫面學習知識點；作為獨立 CLI，查詢及同步學在浙大、智雲課堂資料。兩種方式使用同一份程式，互不依賴。

## 必需環境

安裝 [uv](https://docs.astral.sh/uv/getting-started/installation/)，確保終端可執行 `uv --version`。uv 根據腳本內的 PEP 723 宣告管理 Python >=3.10 與 Python wheel；首次使用需要下載環境和依賴。不需手動安裝 Python 套件、系統 FFmpeg 或 ffprobe。缺少 uv 時直接入口會提示安裝，不切換系統 Python。

Bun / Node.js 只用於下列對應的 Skill 安裝器，CLI 執行不需要它們。macOS、Windows、Linux 使用相同腳本。

## 方式一：安裝 Agent Skill（內含完整 CLI）

使用 [Vercel skills](https://github.com/vercel-labs/skills)，依使用範圍選擇安裝方式。兩種方式都包含完整的 `scripts/zju.py`，不需另外複製倉庫或下載 CLI。

以下 CLI 路徑以 **Vercel skills 的 Codex 安裝**為例；其他 Agent 或安裝器可能使用不同目錄，請以安裝器回報的 Skill 路徑為準，執行其中的 `scripts/zju.py`。

### 專案級安裝

適合在指定工作區使用。在希望使用 Skill 的工作目錄中執行：

```bash
bunx skills add geniucker-dev/zju-learnpilot
# 安裝後直接執行內附 CLI（Codex 路徑範例，在該工作目錄中執行）
uv run --script ".agents/skills/zju-learnpilot/scripts/zju.py" --help
```

### 使用者級安裝

適合希望在多個專案中使用同一 Skill 的使用者：

```bash
bunx skills add -g geniucker-dev/zju-learnpilot
# 安裝後可從其他目錄執行內附 CLI（Codex 路徑範例）
uv run --script "$HOME/.agents/skills/zju-learnpilot/scripts/zju.py" --help
```

沒有 Bun、已有 Node.js 時可將 `bunx` 換成 `npx`。安裝器會自動發現唯一的 `zju-learnpilot` Skill，無需傳入 `--skill`。依提示選擇 Agent；識別到 Agent 時也可能自動完成安裝。只安裝給 Codex 可加上 `--agent codex`。上例 `$HOME` 同樣適用 PowerShell。安裝器**不會自動建立全域 `zju` 命令**；兩種作用域下執行 CLI 都需要 uv。

Skill 安裝作用域決定 Agent 從哪裡發現 Skill，與 CLI 的課程資料庫位置無關。兩種安裝皆可透過 `--library`、`--course-dir` 或既有設定選擇資料位置，參見[路徑說明](#路徑與設定)。Skill 相對自身定位腳本，不依賴範例路徑或目前工作目錄。

安裝後在對應工作區開啟新的 Agent 會話，或依宿主提示重新載入 Skill，即可提出：

- 「幫我學習現代控制理論的能控性，優先使用既有課程資料。」
- 「解釋這個課程目錄中某堂課 01:23:45 附近的推導。」
- 「列出本地課程；同步我選擇的課程，包含錄播音訊。」

Skill 先搜尋本地轉寫，再以 `classroom context` 定位原文與候選 PPT，必要時少量取幀。回答引用堂次、時間與頁碼，區分課堂內容和 AI 補充。PPT 與媒體時間尚未精確校準，多段錄播不猜所屬分段。遠端查詢/下載需要登入；上傳、交作業及發帖須有明確授權。

### 其他安裝器與更新

- **[OpenSkills](https://github.com/numman-ali/openskills)**：`npx openskills install geniucker-dev/zju-learnpilot` 預設安裝到目前專案的 `.claude/skills`；加 `-g` 安裝到使用者級 `~/.claude/skills`，`--universal` 改用對應作用域的 `.agent/skills`。在需要的 Agent 專案執行 `npx openskills sync`，選擇本 Skill，將發現資訊寫入該專案的 `AGENTS.md`。更新使用 `npx openskills update zju-learnpilot`。
- **Codex 原生 Skill 安裝器**：請 Codex 的 `$skill-installer` 安裝 `https://github.com/geniucker-dev/zju-learnpilot/tree/main/skills/zju-learnpilot`。通常位於 `~/.codex/skills/zju-learnpilot`，完成後按提示重新載入/啟動 Agent。原生安裝器拒絕覆寫同名目錄；更新前備份並移除該 Skill 的舊安裝副本，再重新安裝，不清空其他 Skills。
- **標準目錄分發**：將完整 `skills/zju-learnpilot/` 放進宿主支援的 Skill 目錄，不只複製 `SKILL.md`。遵循 [Agent Skills 規範](https://agentskills.io/specification)，附帶 `scripts/`、`references/` 與 `LICENSE`；規範定義 Skill 結構，安裝位置由宿主或安裝器決定。

Vercel skills 可重跑相同 `add` 命令更新，亦提供 `skills update`。更新時整體替換 Skill，讓說明與 CLI 同步。課程資料及憑據存放在 Skill 外，不放進安裝目錄。各安裝器的路徑、發現與更新行為不同，不能假定共用全域註冊或 PATH 命令。

## 方式二：獨立 CLI（無需 Agent / Skill 安裝器）

```bash
git clone https://github.com/geniucker-dev/zju-learnpilot.git
cd zju-learnpilot
uv run --script zju.py --help
uv run --script zju.py login
```

macOS/Linux 亦可直接 `./zju.py login`。下文的 `./zju.py …` 在 Windows 使用 `uv run --script zju.py …`，不需軟連結。既有倉庫目錄可保留原名稱，透過根目錄 `./zju.py` 呼叫的同步腳本與 CLI 參數不變。

```text
zju-learnpilot/
├── zju.py                         # 薄入口，只轉交下列腳本
├── skills/zju-learnpilot/
│   ├── SKILL.md
│   ├── scripts/zju.py             # 唯一 CLI 實作及 PEP 723 依賴
│   ├── references/                # 按需讀取的學習與課程操作說明
│   ├── agents/openai.yaml         # Codex 展示資訊，其他 Agent 可忽略
│   └── LICENSE
└── tests/
```

根目錄入口需要保留此倉庫結構；Skill 內的 `scripts/zju.py` 本身仍可單獨複製，是完整的單檔 CLI，直接 `uv run --script /path/to/zju.py …` 即可。兩種入口都由 uv 管理環境，不需安裝專案套件。

雙擊 `zju_login.command` / `zju_login.bat` 會執行 `login --reset`，重設憑據及會話，**保留資料庫與追蹤課程設定**。密碼存於系統憑證庫（macOS Keychain，其他平台使用 keyring），亦支援 `ZJU_USER` / `ZJU_PASS`。設定和會話仍位於 `~/.config/zju-learning/`，`ZJU_STATE_DIR` 可覆寫整個狀態目錄。品牌變更不搬移設定、認證、快取、課程資料或改變 JSON 格式；憑據不放進課程目錄。

## 命令分組

| 命令 | 用途 |
| --- | --- |
| `login [學號] [--reset]` | 認證及憑據 |
| `lms courses [--all] [--json]` | 學在浙大課程；預設最新學年，JSON 與文字篩選一致 |
| `lms sync ID…` / `lms sync --all` | 增量同步指定課程或全部個人課程 |
| `lms todo [--json]` | 待辦 |
| `lms activities [課程…] [--all] [--type …] [--json]` | 活動清單 |
| `lms show 活動ID [--read]` | 詳情；`--read` 取得附件並擷取文字 |
| `lms forum list/read/post/reply …` | 查詢討論區、發文及回覆 |
| `lms upload 檔案…` | 上傳附件 |
| `lms submit 活動ID --file 檔案… [-y] [--draft]` | 交作業；預設確認，截止時阻擋 |
| `classroom courses [--has-tasks] [--json]` | 智雲個人課程；ID 與 LMS 不同 |
| `classroom search 關鍵字 [--teacher 教師]` | 搜尋遠端課程 |
| `classroom subs --course ID [--json]` | 遠端堂次 |
| `classroom day [YYYY-MM-DD] [--days N]` | 截至該日最近 N 天課堂 |
| `classroom add/rm ID…`、`classroom tracked` | 追蹤課表外課程，不改變儲存名稱 |
| `classroom sync ID…` / `classroom sync --all` | PPT PDF、事件 JSON、Markdown，可選影音 |
| `classroom ppt/transcript/recording/recording-audio` | 個別取得資料，同時更新課程中繼資料 |
| `classroom context` | 完全離線的近似上下文定位 |
| `classroom recording-frame` | 本機優先，遠端 Range 按需取幀 |

所有智雲資料命令都在 `classroom` 下。批次同步必須指定 ID 或明確 `--all`，不接受名稱片段作為同步 ID。個別命令支援 `--course ID --sub ID…` 或 `--days N`，未選課程時預設今天。`--days` 與課程選擇互斥，`--sub` 必須搭配課程 ID 或課程目錄。詳細參數見各命令 `--help`。

## 路徑與設定

- `--library DIR`：多門課程目錄的直接父目錄。
- `--course-dir DIR`：單門課程的實際目錄，直接包含 `course.json`。
- `--output FILE` / `--output-dir DIR` / `--frame TIME=FILE`：截圖輸出位置。

參數放在對應子命令後。`--library` 和 `--course-dir` 互斥；顯式路徑按原樣使用，不追加平台父目錄。身份為 `(platform, course_id)`，目錄名稱只供顯示。

預設資料庫為 `~/ZJU-Courses/classroom`、`~/ZJU-Courses/lms`。`config.json` 可設定：

```json
{
  "libraries": {
    "classroom": "~/ZJU-Courses/classroom",
    "lms": "~/ZJU-Courses/lms"
  }
}
```

優先順序：命令顯式目錄 > 對應平台 `libraries` 設定 > 預設目錄。使用絕對路徑或 `~`。舊 `--out`、`ZJU_OUT`、設定 `out`、根目錄 Manifest 不屬於本版規範，沒有相容或自動遷移。

### 集中式資料庫

```bash
./zju.py classroom sync 89253 89418 --library ~/ZJU-Courses/classroom --recording-audio -j 32
./zju.py classroom sync --all
./zju.py lms sync 123456 --library ~/ZJU-Courses/lms
./zju.py lms sync --all --videos --max-size 0
```

```text
ZJU-Courses/
├── classroom/
│   ├── 課程A (89253)/
│   └── 課程B (89418)/
└── lms/
    └── 課程C (lms-123456)/
```

只檢查資料庫直接子目錄的 `course.json`。同一 ID 已有唯一目錄就重用，改名只更新中繼資料。多個相同 ID 副本會報錯，需顯式 `--course-dir`；不遞迴掃描磁碟或選擇「最新」副本。

### 自己的課程資料夾內放智雲子目錄

```bash
./zju.py classroom sync 89253 --course-dir "/path/to/Class/現代控制理論/智雲課堂" --recording-audio
./zju.py classroom sync --course-dir "/path/to/Class/現代控制理論/智雲課堂" --recording-audio
```

### 直接與講義、作業混放

```bash
./zju.py classroom sync 89253 --course-dir "/path/to/Class/現代控制理論" --recording-audio
```

首次初始化需明確 ID；已有 `course.json` 可省略，若同時指定則必須相符。一個 `--course-dir` 只能對應一門課。未登記的講義、作業等使用者檔案不會自動接管、刪除或覆寫，即使有 `--force` 也一樣。

寫入命令不從目前目錄猜測目標。唯讀查詢和取幀可識別目前目錄自身的 `course.json`，顯式路徑優先。整個課程目錄可以移動、複製或重新命名。

## 課程與下載狀態

實際資料子目錄沿用簡體命名，兩個語言版本使用相同格式：

```text
課程目錄/
├── course.json
├── .zju/downloads.json
├── 智云PPT/堂次名稱 (堂次ID).pdf、同名.json
├── 转录/堂次名稱 (堂次ID).md
├── 音频/堂次名稱 (堂次ID).m4a
└── 录播/堂次名稱 (堂次ID).mp4
```

多段影音使用 ` - 01`、` - 02` 後綴；狀態明確記錄 `part`、`part_count`，不從名稱猜身份。LMS 附件使用 `附件/原名 (upload_id).副檔名`，同名課程和附件均以 ID 隔離。

`course.json` 版本為 2，含 `platform`（`classroom` / `lms`）、`course_id`、`name`、`updated_at`。智雲另記教師、學期、堂次與已存在資料引用；LMS 記附件及已知學期欄位。所有資料引用相對課程目錄，使用 `/`。

`.zju/downloads.json` 版本為 1，包含平台、課程 ID 和 `entries`，區分 `pending` 與 `complete`，記錄路徑、大小、類別、堂次/附件 ID、格式或分段資訊。只有完成且通過大小與格式檢查的智雲資料才進入公開中繼資料；不掃描目錄接管使用者檔案，不儲存播放簽名 URL 或憑據。

同步和個別資料命令都會更新課程中繼資料；API 遺漏或暫時失敗不刪除既有堂次。顯示名稱改變不改變已登記路徑。原子更新失敗保留原檔並報錯。

課程寫入有跨程序鎖；下載狀態在鎖內重讀、合併、原子替換。資料庫首次登記有短期配置鎖。鎖檔會保留，`.zju/` 和工具鎖檔屬於保留位置。**這是單機檔案鎖，不是雲端硬碟的跨設備分散式鎖；請勿兩台電腦同時寫同一課程。**

`--dry-run` 不下載媒體、不寫課程資料或狀態；線上查詢仍可能更新使用者會話。錄播中斷保留 `.part`、`.part.json`，下次驗證來源及分片 SHA-256 後續傳。`--force` 重做已登記資料並丟棄錄播分片進度；不會自動轉換舊 Markdown。

## PPT、轉寫、音頻與錄播

```bash
./zju.py classroom ppt --course 89253 --sub 2038852 --dedup --keep-images
./zju.py classroom transcript --course 89253 --format srt
./zju.py classroom transcript --course-dir /path/to/course --format md --force
./zju.py classroom recording --course 89253 -j 4
./zju.py classroom recording-audio --course 89253 -j 32
./zju.py classroom recording-audio --days 7
```

轉寫一律預設 Markdown，格式如 `**[01:23:45.120 → 01:23:52.875]** 內容`；SRT 使用逗號分隔毫秒，TXT 可顯式選取。有效且已登記的檔案增量略過。

PPT JSON 保留每個原始事件的 `created_sec`、從 0 起算的事件編號、從 1 起算的 PDF 頁碼、代表事件與去重關係。動畫、返回同頁及空白事件不丟失；時間是觀測點，不是精確顯示區間。`--keep-images` 保留並登記原始截圖。

音頻沿用非同步 HTTP Multi-Range 取得壓縮音軌並重建 M4A，不重新編碼；完整本機錄播優先。錄播沿用 Range 分片及斷點續傳；伺服器不支援 Range 時完整下載可退回單連線，**按需取幀不會退回完整下載**。

同步 `-j` 預設 4，單項音頻預設 32，各階段遵循所設並行上限。`--max-size` 為單一輸出檔案的 MiB 上限，0 不限。LMS 預設 200 MiB、略過影音附件；`--videos` 開啟 LMS 影音附件，不等同智雲錄播。

## 完全離線的發現與上下文

```bash
./zju.py classroom courses --local --library /path/to/library --json
./zju.py lms courses --local --library /path/to/lms-library --json
./zju.py classroom subs --course-dir /path/to/course --json
./zju.py classroom subs --local --library /path/to/library --course 89253 --json
./zju.py classroom context --course-dir /path/to/course \
  --sub 2038852 --at 01:23:45.120 --window 20 --json
```

不登入、不呼叫 API、不初始化解碼器、不修改資料。顯式目錄查詢和 `--help` 不受全域設定損壞影響。重複身份、損壞中繼資料或路徑歧義明確報錯；拒絕絕對、`..` 與越界符號連結資料引用。

JSON 含 `schema_version`、`course`、`lesson`、`location`、`query`、`transcript`、`ppt`、`audio`、`video`、`recording_frame_hint`、`warnings`。資料路徑相對 `location.course_dir`。

轉寫以閉區間比對，預設前後 20 秒，支援毫秒、整秒與只有起點的 Markdown，異常長段、重疊、逆序時間另行標記。TXT/SRT 只提供引用。PPT 回傳視窗內及相鄰事件，無時間事件單列，不強行選唯一頁。

`missing`、`invalid`、`unavailable`、`unsafe_path`、`partial`、`no_matches` 等狀態區分缺失、損壞及附近無內容。未記錄時長為 `null`；轉寫與錄播可能有數秒以上偏差，分段與整堂課的映射未知，候選窗口**未校準**，不自動修正或累加分段時長。

## 按需取幀

```bash
./zju.py classroom recording-frame --course-dir /path/to/course --sub 2038852 --at 01:23:45.120 --json
./zju.py classroom recording-frame --course 89253 --sub 2038852 --at 5025.12 --output /path/to/board.jpg
./zju.py classroom recording-frame --course 89253 --sub 2038852 --at 01:23:40 --at 01:23:45 --output-dir /path/to/frames
./zju.py classroom recording-frame --course 89253 --sub 2038852 \
  --frame "01:23:40=/path/to/before.jpg" --frame "01:23:45=/another/path/after.png"
./zju.py classroom recording-frame --course 89253 --sub 2038852 --part 1 --at 01:23:45 --around 10 --interval 2 --json
```

`--offline` 禁止遠端回退。多段需指定 `--part`，`--at` 為所選 MP4 內相對時間，不能當成已校準的轉寫/PPT 時間。

PyAV wheel 內可包含 FFmpeg 函式庫，無需系統 ffmpeg/ffprobe。以真實 PTS 選擇顯示幀，支援 B 幀、VFR、非零起點及批次資源重用。遠端嚴格驗證 206、範圍與來源版本，`--max-fetch-mb` 預設 64 MiB，`--max-frames` 預設 100，不預先下載完整影片。

預設原解析度 JPEG，品質 95（`--quality`），亦支援 PNG。未指定輸出時建立獨立系統暫存目錄，檔名含課程、堂次、分段、時間，程序結束後仍保留，由呼叫端或系統清理，不複製到課程或索引快取。

有效截圖快取可重用；自動檔名衝突時另命名，明確完整路徑衝突報錯，`--force` 才覆寫截圖。圖片與 JSON sidecar 共同驗證。`--json` stdout 只有單一物件，提供真實路徑、要求/實際時間、來源、網路請求數、位元組與耗時，診斷在 stderr。

## 共享 MP4 索引快取

音頻與取幀預設共享原始完整 `moov`，不快取 M4A 重寫索引、媒體片段或完整 MP4。

- macOS/Linux：`${XDG_CACHE_HOME:-~/.cache}/zju-learning/`
- Windows：`%LOCALAPPDATA%\zju-learning\cache\`；環境路徑無效時回退使用者目錄的 `AppData/Local`。
- `--index-cache-dir DIR` 覆寫，`--no-index-cache` 關閉；`--index-cache-mb 256` 設定總容量，預設 256 MiB。

跨次重用需強 ETag 與同一來源身份，不保存簽名參數。版本變更、損壞、無寫入權或鎖衝突安全退回網路。Last-Modified 只作單次一致性驗證，不充當跨次強身份。刪除索引快取不影響下載完成狀態。

## 驗證與界線

```bash
uv run --python 3.12 --with requests --with httpx --with img2pdf --with pillow \
  --with keyring --with numpy --with 'av>=16,<18' python -m unittest discover -s tests
```

測試使用 Python wheel 產生媒體及本機 HTTP，不需系統 FFmpeg 或學校帳號。CI 設定包含 Windows、macOS、Linux，實際結果以運行記錄為準。

退出碼：0 成功；1 設定、認證、中繼資料或執行錯誤；2 參數錯誤或批次部分失敗（由 stderr 區分）；130 使用者中斷。

本版沒有舊命令、舊設定、舊 Manifest 相容或自動遷移。既有資料由使用者於倉庫外單獨搬移並建立新身份及完成記錄；僅複製媒體不會自動登記，不需因容器格式變化重新下載或轉碼。不提供自動時間軸校準、全域註冊、背景服務或 MCP。

## 安全、致謝與授權

Cookie 以受權限保護的 JSON 放在使用者狀態目錄，明文 HTTP 不傳送 Cookie/Authorization，無遙測。資料版權屬授課教師及學校，請遵守平台規範，不公開散布課程材料。

本專案從 [8eoyw/zju-learning-cli](https://github.com/8eoyw/zju-learning-cli) 發展而來，認證與平台 API 邏輯源自 [PeiPei233/zju-learning-assistant](https://github.com/PeiPei233/zju-learning-assistant)，保留原有 MIT 版權聲明。錄播介面參考 Cold_Ink 的[智雲課堂批量下載](https://greasyfork.org/scripts/514465)；附件存取參考 [ZJU-course-material-download](https://github.com/eWloYW8/ZJU-course-material-download)、[Tronclass-pdf-downloaderforChrome](https://github.com/Kcalb35/Tronclass-pdf-downloaderforChrome)、[TronClass-PDF-Downloader](https://github.com/fish-can/TronClass-PDF-Downloader)、[xzzd-pro](https://github.com/xzzd-pro/xzzd-pro)。

[MIT License](LICENSE)
