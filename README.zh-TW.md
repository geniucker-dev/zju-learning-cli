# zju-learning-cli

[简体中文](README.md) | 繁體中文

學在浙大（courses.zju.edu.cn）與智雲課堂（classroom.zju.edu.cn）的輕量命令列工具：同步課件、把智雲課堂的 PPT 截圖合併成 PDF（可去除重複截圖）、匯出課堂語音轉錄、查待辦。

它是 [PeiPei233/zju-learning-assistant](https://github.com/PeiPei233/zju-learning-assistant)（ZLA）的 CLI 移植版。ZLA 是很好用的桌面 GUI，但沒辦法寫進腳本、排程或讓 AI agent 呼叫；這個專案把它的 API 邏輯改寫成輕量 Python CLI，補上增量同步、並行下載、PPT 去重，也修了幾個上游的邊界狀況。

## 功能

| 指令 | 作用 |
| --- | --- |
| `zju login` | 設定學號，密碼存進系統憑證庫（macOS Keychain、Windows 認證管理員；其他系統走 [keyring](https://pypi.org/project/keyring/)） |
| `zju courses [--all]` | 課程列表（預設只列最新學年） |
| `zju sync [課程...]` | 增量同步課件到 `<輸出目錄>/<課程>/` |
| `zju todo` | 待辦事項，依截止時間排序（本地時區） |
| `zju activities [課程…] [--type forum homework …]` | 列出所有活動（課件、影片、作業、討論、網頁、連結、測驗），附狀態與截止時間 |
| `zju show <活動id> [--read]` | 活動詳情：說明、附件、完成條件；作業顯示自己的提交狀態，討論顯示帖數及自己的發帖／回帖紀錄；`--read` 下載附件並印出文字（docx/pptx/pdf/doc，貼圖標成 [圖]） |
| `zju forum list <討論id> [--mine] [--full]` / `forum read <topic>` | 列出討論帖（`--mine` 只列自己的發帖與回帖）／讀帖與回覆 |
| `zju forum post <討論id> --title … --body …` / `forum reply <topic> --body …` | 發帖／回帖，可 `--body-file`、`--attach` 附件 |
| `zju upload 檔案…` | 上傳檔案到學在浙大，印出 upload id |
| `zju submit <作業id> --file … [--body …] [--draft] [-y]` | 交作業；預設送出前確認，已截止會擋下 |
| `./zju.py classroom courses [--has-tasks] [--json]` | 智雲個人課程，顯示課程 ID、學期、教師與任務數 |
| `./zju.py classroom sync [課程...] [-j 4] [--recording] [--recording-audio]` | 同步智雲個人課程的 PPT 與轉錄，可選錄播、音訊 |
| `zju classroom search 關鍵字` | 在智雲課堂找課，取得 `course_id` |
| `zju classroom subs <course_id>` | 列出該課每一堂的 `sub_id` |
| `zju classroom day [日期] [--days N]` | 某天（或最近 N 天）自己的課，含追蹤中的課 |
| `./zju.py classroom add\|rm <course_id>...` | 追蹤或移除課表外課程；`--days` 也包含追蹤課程，目錄為 `課名 老師 (課程ID)` |
| `./zju.py classroom tracked` | 列出追蹤中的課程 |
| `zju ppt --course <id> \| --days N [--dedup]` | 智雲 PPT 截圖 → `<課程> (<id>)/智云PPT/<堂> (<sub_id>).pdf` |
| `zju transcript --course <id> \| --days N` | 語音轉錄 → `<課程> (<id>)/转录/<堂> (<sub_id>).txt\|srt\|md` |
| `./zju.py recording --course <id> \| --days N [-j 4]` | 智雲錄播 → `<課程> (<id>)/录播/<堂> (<sub_id>).mp4`，多連線分片下載 |
| `./zju.py recording-audio --course <id> \| --days N [-j 32]` | 優先從本地錄播提取音軌，否則只下載音訊 → `<課程> (<id>)/音频/<堂> (<sub_id>).m4a` |
| `./zju.py recording-frame --course <id> --sub <id> --at HH:MM:SS [--json]` | 按需提取真實影片影格，本地優先、遠端限量 Range 讀取；預設系統暫存目錄 |


## 安裝

需要 [uv](https://docs.astral.sh/uv/) 與 Python >=3.10（uv 可管理 Python）。相依套件寫在檔頭（PEP 723），第一次執行時 uv 會自動安裝。只需 `zju.py`，可單獨複製到任意目錄使用。

```bash
git clone https://github.com/geniucker-dev/zju-learning-cli.git
cd zju-learning-cli
./zju.py login
```

沒有 uv 的話：`pip install requests httpx img2pdf pillow keyring numpy 'av>=16,<18'`，再用 `python zju.py ...` 執行。

影片影格解碼使用 [PyAV](https://pyav.basswood.io/docs/stable/) 的跨平台 wheel。wheel 可內建 FFmpeg 編解碼函式庫，使用者**不必安裝或呼叫系統 ffmpeg / ffprobe**；其他命令不會初始化影片解碼函式庫。常見 macOS、Windows、Linux 的 Python 版本有預編譯 wheel；沒有對應 wheel 的平台需要自行處理函式庫建置，不會退回呼叫外部程式。

也可以不開終端機，直接雙擊登入腳本（每次都會清掉上一次的學號、密碼和 cookie，重新登入）：

- macOS：`zju_login.command`，在 Finder 雙擊會開一個終端機視窗。用 git clone 的可以直接雙擊；下載 zip 的會被 Gatekeeper 擋下，先執行一次 `chmod +x zju_login.command && xattr -c zju_login.command`。
- Windows：`zju_login.bat`，需要 Python 啟動器 `py -3.11`，並先用 pip 裝好上面的相依套件。

## 使用

```bash
zju sync --dry-run                 # 先看會下載什麼、總共多大
zju sync                           # 最新學年全部課程
zju sync 微积分 123456              # 指定課程（名稱片段或 id，id 用 zju courses 查）
zju sync --videos --max-size 0     # 連影音和大檔一起抓
zju sync -j 8                      # 並行數（預設 4）

zju classroom day --days 7
zju ppt --days 1 --dedup           # 今天所有課的 PPT，重複截圖只留最完整的一張
zju transcript --days 1 --format md
```

輸出目錄的優先順序：`--out` > 環境變數 `ZJU_OUT` > `~/.config/zju-learning/config.json` 的 `"out"` > `~/ZJU-Courses`。

`sync --videos` 下載學在浙大活動的影音附件；`./zju.py recording` 下載智雲課堂的課堂錄播，兩者來源不同。

程式提示與說明使用簡體中文。智雲資料統一保存在 `<課程名> (<course_id>)` 下，按 `智云PPT`、`转录`、`录播`、`音频` 分類，檔名為 `<堂次名> (<sub_id>).副檔名`。`--keep-images` 保留的截圖也使用含堂次 ID 的子目錄。課程名和轉錄內容保留平台原文；不辨識或遷移舊目錄。

排程範例（cron，每天 22:00）：

```cron
0 22 * * * ~/.local/bin/zju sync && ~/.local/bin/zju ppt --days 1 && ~/.local/bin/zju transcript --days 1
```

退出碼：`0` 成功；`1` 設定、登入或 API 錯誤；`2` 部分檔案或堂次失敗（其餘照常完成）。

## 跟 ZLA 的差異

### PPT 時間事件映射

每堂 PPT 除 PDF 外也產生同名 `.json`，無論是否啟用 `--dedup` 都保留截圖事件。`events` 保存每張原始截圖的 `created_sec`、原始接口資料、圖片 SHA-256、對應 `pdf_page` 及代表它的 `representative_event_index`；`pages` 保存每頁的代表事件和全部關聯事件。事件編號從 0 開始，PDF 頁碼從 1 開始。

動畫補全與翻回舊頁的事件仍保留，多個事件可對應同一 PDF 頁。`relationship: represented` 表示由保留頁面代表，不保證當時畫面完全相同；移除的空白過場標記為 `blank`，頁碼為 `null`。全部截圖為空白時沿用既有行為，全部輸出為 PDF 頁。缺少或無效時間為 `null`，不按頁碼或截圖間隔推算。時間是截圖觀測點，並非頁面顯示區間；與音軌起點的對齊尚未驗證（`audio_alignment: unverified`）。

預設記錄原圖身份但不保存圖片；加 `--keep-images` 保留全部原圖，`image_file` 為相對 JSON 所在目錄的路徑。已有 PDF 缺少同名 JSON 時會重新產生該堂 PDF 並補建映射；兩者齊全才略過。`--force` 可重新產生 PDF 與映射。

### 智雲課程同步

預設同步全部個人錄播課程的所有堂次，下載 PPT（PDF）與 Markdown 轉錄；也可用課程 ID 或名稱片段選課。`--recording` 同時下載錄播，`--recording-audio` 同時取得音訊。`-j` / `--jobs` 是整個同步的 worker 總數（預設 4）：每張 PPT 截圖、每份轉錄各佔一個 worker，錄播的每個分片各佔一個 worker，不再額外開錄播分片執行緒池。轉錄、PPT、錄播依序處理並重用同一個執行緒池。音訊於這些步驟完成後處理，遠端音訊請求也遵循 `-j` 上限（同步預設 4）；單獨 `recording-audio` 命令預設 32。搭配 `--recording --recording-audio` 時直接從剛下載的錄播提取音軌。

使用統一資料目錄與增量跳過規則，錄播繼續支援跨次執行的斷點續傳。`--force` 重新下載資料並丟棄錄播分片進度；`--format txt|srt|md` 選擇轉錄格式，`--dedup` 去除重複 PPT 截圖，`--keep-images` 保留全部截圖，`--max-size MB` 限制錄播單檔大小（0 為不限）。

`--dry-run` 不下載、不寫檔：PPT 與轉錄依現有檔案及堂次預覽路徑，標記為待檢查的資料可能尚未發布；錄播讀取目錄後預覽。單項失敗仍繼續處理其他資料，最終退出碼為 2。

每次非預覽同步都會在 `<out>/<課程名稱> (<course_id>)/course.json` 原子更新課程中繼資料；即使資料全部增量略過，也會替既有目錄補建。它重用本次查詢的課程與堂次資訊，不額外請求 API，不改變資料配置。精簡範例（其他資料陣列可為空）：

```json
{
  "schema_version": 1,
  "source": "zju-classroom",
  "course_id": 123,
  "name": "範例課程",
  "teacher": "教師",
  "term": "2026秋",
  "updated_at": "2026-10-20T02:00:00+00:00",
  "lessons_fetch_status": "ok",
  "lessons_checked_at": "2026-10-20T02:00:00+00:00",
  "lessons": [{
    "sub_id": 456,
    "name": "第一堂",
    "date": "2026-10-01",
    "lecturer": "授課教師",
    "availability": "listed",
    "last_seen_at": "2026-10-20T02:00:00+00:00",
    "files": {
      "transcripts": [{"format": "md", "path": "转录/第一堂 (456).md"}],
      "ppt_pdfs": [{"path": "智云PPT/第一堂 (456).pdf"}],
      "ppt_events": [{"path": "智云PPT/第一堂 (456).json"}],
      "audio": [{"part": 1, "path": "音频/第一堂 (456) - 01.m4a"}],
      "recordings": []
    }
  }]
}
```

所有資料路徑相對於 `course.json`，移動整個課程目錄後仍可使用。`type`、`task_count` 也會記錄既有課程資訊；未知資訊為 `null`。`availability` 僅表示接口的上架狀態：`listed` / `unlisted` / `unknown`，不表示一定有錄播或轉錄。`last_seen_at` 表示最後一次從堂次 API 看到該堂次的時間。本次遺漏的舊堂次繼續保留；堂次查詢失敗時保留舊資訊，`lessons_fetch_status` 為 `error`，`lessons_checked_at` 保留上次成功時間。下載部分失敗仍核對既有檔案，不把失敗當作下架。課程或堂次改名不移動舊檔，舊課程目錄也會更新中繼資料，各目錄僅引用自身資料。

檔案引用包含目錄內既有的所有支援格式，與本次是否啟用影音下載無關；多段影音逐項記錄從 1 開始的 `part`。僅列出非空且通過格式檢查的最終檔案，並核對既有影音清單大小；PDF 事件 JSON 還需有有效的配套 PDF。未完成檔、暫存檔、缺失或明顯損壞的檔案不列入，也不保存憑證或播放網址。檢查不進行整段媒體解碼，無法保證發現任意媒體內容損壞。`course.json` 使用獨立寫鎖與原子替換，寫入失敗保留舊版；已有中繼資料損壞或身份不符時回報失敗並保留原檔，供使用者檢查。`.zju_manifest.json` 的下載狀態管理保持不變。`--dry-run` 不建立或更新中繼資料。

Markdown 轉錄每段包含開始與結束時間，例如 `**[00:01:23 → 00:01:38]** 內容`，精確到秒；SRT 精確到毫秒。已有 `.md` 仍會略過，不自動升級；使用獨立 `transcript --format md --force` 重新產生，可避免重抓 PPT、錄播與音軌。

接口標記為已下架（`show: "no"`）的堂次，錄播與音軌會顯示「已下架，跳過」，不計為失敗；PPT 與轉錄仍按資料可用性處理。

```bash
./zju.py classroom sync -j 8
./zju.py classroom sync 89418 --recording -j 8
./zju.py classroom sync 人工智能 --dry-run --recording
```

### 智雲錄播下載

預設列出智雲「我的課程」中的全部課程，包括任務數為 0 的課程；`--has-tasks` 只列任務數大於 0 的課程。任務數與網頁一致，不代表一定有可下載錄播。`--json` 輸出 `course_id`、`title`、`teacher`、`term`、`type` 與 `task_count`。這些課程 ID 可用於 `classroom subs`、`ppt`、`transcript`、`recording` 與 `recording-audio`；頂層 `courses` 列出的則是學在浙大的 ID。

可直接執行 `./zju.py`，不必建立符號連結。使用智雲課程 ID（與學在浙大不同）：

```bash
./zju.py classroom courses
./zju.py classroom courses --has-tasks
./zju.py classroom courses --json
./zju.py classroom search 人工智能
./zju.py classroom subs 89418
./zju.py recording --course 89418 --dry-run       # 只列待下載影片，不下載、不寫檔
./zju.py recording --course 89418 --sub 2019095   # 指定堂次
./zju.py recording --course 89418 -j 8           # 整門課；每個影片最多 8 個連線
./zju.py recording --days 7                     # 最近 7 天自己的課堂
./zju.py recording --course 89418 --max-size 4096 # 單檔上限 4096MB；預設 0 = 不限
```

預設每個影片使用 4 個連線，以 32MiB 分片平行下載，顯示進度及平均速度。逐個下載影片，分片失敗最多嘗試 3 次；伺服器不支援 Range 時回退到單連線。檢查每片的回應範圍、總大小、實際位元組數及最終 MP4 檔頭，全部成功才替換目標檔並寫入下載清單。伺服器提供 ETag 或 Last-Modified 時，使用 If-Range 防止混合不同版本。

已下載且大小符合清單的檔案會略過；暫無回放的堂次略過，下次重新查詢。多段回放網址分別存成帶編號的 MP4。目錄與檔名含課程、堂次 ID，避免同名覆蓋。目前支援直接 MP4，不支援 HLS。`--out` 放在子命令前，例如 `./zju.py --out ~/ZJU-Courses recording --course 89418`。

**影片預設支援跨次執行的斷點續傳。** 中斷或失敗時，在影片旁保留隱藏的 `.影片名.mp4.part` 資料檔與 `.影片名.mp4.part.json` 進度檔。重新執行相同命令（輸出目錄不變）即可繼續，`-j` 可調整；只補抓未完成或校驗損壞的分片，未完成的單片從頭下載。每片完成後將資料寫入磁碟，再原子儲存進度及 SHA-256；下次校驗已完成分片。全部完成才替換 MP4，並清除資料和進度檔。隱藏的 `.影片名.mp4.download.lock` 小鎖檔保留，以阻止多個程序同時下載同一影片。

續傳前核對遠端 ETag（或 Last-Modified）、總長度與來源；遠端版本變更、本地進度損壞或資料檔缺失時重新下載。網址簽名參數更新不影響續傳，仍須通過遠端版本核對。伺服器不支援 Range 或無可用版本標記時，從頭下載。`--force` 明確丟棄已有分片並重抓；不加 `--force` 才沿用進度。課件、PPT 與轉錄下載行為不變。

目錄接口及網址提取參考 Cold_Ink 的 [智云课堂批量下载](https://greasyfork.org/scripts/514465)（MIT）；本專案依實際接口相容字串與列表網址，使用 Python 串流分片下載。

### 智雲音訊

無需安裝 `ffmpeg` 或其他影音工具。程式直接讀取 MP4 索引、複製第一條音軌並重建 M4A，不重新編碼：

```bash
./zju.py recording-audio --course 89418 --sub 2019095
./zju.py recording-audio --course 89418                  # 預設 32 個協程並行
./zju.py recording-audio --days 7 -j 8
./zju.py recording-audio --course 89418 --dry-run
./zju.py classroom sync 89418 --recording-audio -j 32
./zju.py classroom sync 89418 --recording --recording-audio -j 8
```

優先使用統一目錄中已完成的錄播；未完成的 `.part` 不算完整影片。沒有影片時，讀取遠端 MP4 索引並只下載音軌範圍。每批最多 360 個音訊分片，Range 請求頭接近 8KiB 時自動縮小批次。音軌範圍請求固定直連，不讀取環境代理，也不回退到代理。

輸出為 `<輸出目錄>/<課程名稱> (<course_id>)/音频/<堂次名稱> (<sub_id>).m4a`，多段回放分別編號。已完成音訊會略過；`--force` 重新提取或下載，`--max-size MB` 限制最終音訊大小。成功後才原子替換輸出並更新清單，失敗或中斷會清理暫存資料，保留舊檔案；音訊暫不支援跨次執行的續傳。

支援有完整音軌索引的直接 MP4；沒有音軌、HLS、分片 MP4或不支援多範圍請求的伺服器會報錯，不自動下載整個影片。本地提取不受遠端 Range 支援情況影響。

### 按需提取錄播影片影格

新增 `./zju.py recording-frame`，供使用者或 Agent 臨時查看黑板、推導與操作畫面，不加入 `classroom sync` 預設資料。優先使用路徑及大小符合原下載清單的完整本地 MP4，本地命中時不需要認證或課程查詢，否則以 HTTP Range 讀取遠端；`.part` 不視為完整影片。全域 `--out` 只用來尋找本地錄播，不控制影格輸出位置。

```bash
# 預設建立獨立系統暫存目錄，列出每張圖片的完整絕對路徑
./zju.py recording-frame --course 89253 --sub 2038840 --at 01:23:45

# 單張完整路徑，自動建立父目錄；支援相對路徑、中文和 PNG
./zju.py recording-frame --course 89253 --sub 2038840 --at 5025.125 --output "./images/板書.png"

# 多個時間點共用同一容器、讀取快取與解碼上下文
./zju.py recording-frame --course 89253 --sub 2038840 \
  --at 01:23:40 --at 01:23:45 --at 01:23:50 --output-dir "./images/課堂"

# 逐張指定完整路徑，只在第一個 = 處分隔
./zju.py recording-frame --course 89253 --sub 2038840 \
  --frame "01:23:40=./before/板書.jpg" --frame "01:23:45=./after/板書 = 完整.png"

# 前後各 10 秒，每隔 2 秒；越過影片邊界的部分裁剪，始終包含中心時間
./zju.py recording-frame --course 89253 --sub 2038840 \
  --at 01:23:45 --around 10 --interval 2 --output-dir ./sequence

# 多段錄播必須指定分段；Agent 使用 JSON，stdout 不混入進度或診斷
./zju.py recording-frame --course 89253 --sub 2038840 --part 1 --at 01:23:45 --json
```

時間接受 `HH:MM:SS`、`HH:MM:SS.sss` 或浮點秒數，必須非負且小於影片長度。`--at` 是所選 MP4 分段內的相對時間，**不假定**已與轉錄 BeginSec、PPT created_sec 或音軌時間對齊。單段自動選取，多段省略 `--part` 會報錯。依 PTS 取得該時刻正在顯示的影格（兩影格之間取前一張），並記錄真正的 `actual_time_sec`；從之前的關鍵影格開始解碼，支援 B 影格重排、常見可變影格率與非零串流起點。

`--output` 只可搭配單個 `--at`；`--output-dir` 可搭配單張、批次或時間窗口；可重複的 `--frame` 不能與 `--at`、`--output`、`--output-dir` 或窗口選項混用。同次呼叫不可重複指定同一完整路徑。Windows 可使用引號包圍 `"01:23:45=C:\課程\板書.jpg"`，路徑的空格、中文與 `=` 會保留。

所有模式預設最多請求 **100 幀**，可用 `--max-frames 200` 明確調整，必須為正整數。重複 `--at` / `--frame` 每項均輸出圖片，因此逐項計數；視窗先裁剪負時間，以奈秒精度合併取樣點，重疊視窗不重複輸出，始終包含中心點。在認證、讀取影片與建立圖片前檢查數量；視窗計數在影片尾端裁剪之前進行，最終輸出可能更少。

預設用 `tempfile.mkdtemp()` 建立 `zju-frames-<隨機字串>` 系統暫存目錄，不寫死 `/tmp`。**程式結束後圖片與中繼資料仍存在**，呼叫方分析後自行刪除；系統之後也可能清理。明確指定位置時只寫入該位置，不另存到課程同步目錄。自動檔名例如 `89253_2038840_p01_01-23-45-000.jpg`，包含課程、堂次、分段與時間。JPEG 預設品質 95、原解析度，使用 `--quality 1…100` 調整；自動命名可用 `--format png`，完整路徑則依 `.jpg` / `.jpeg` / `.png` 副檔名選擇。

每張圖片旁有 `<圖片名>.json`，記錄來源身分、請求/實際時間、timebase、編碼、尺寸、路徑與圖片 SHA-256，以及產生請求的網路、耗時、輸出數量與暫存目錄標記。只有來源、時間、編碼設定與圖片校驗都一致時才重用快取。本地身分包括大小、奈秒修改/變更時間及 inode；遠端使用無簽名資源路徑摘要、總大小與強 ETag，不保存簽名 URL、Cookie 或 Authorization。Last-Modified 僅用於本次讀取的一致性檢查，無法證明跨次內容相同：缺少強 ETag 時仍可取幀，但不會重用之前的截圖；完整路徑已有圖片時須用 `--force` 或另選路徑。預設暫存目錄不提供跨次快取；指定同一目錄或完整路徑可重用有效快取。自動檔名衝突時加入 `_1` 等後綴；完整路徑已有其他內容或快取失效則報錯。`--force` 重新編碼並允許覆寫。圖片與 JSON 先在目標目錄的暫存子目錄中完整寫入，再加系統鎖發布。寫入失敗時清理本次新建結果；`--force` 發布失敗則恢復被替換的原檔。雜湊及中繼資料必須一致才視為有效快取。目錄保留小型隱藏 `.frame.lock` 檔，系統在程序結束時釋放鎖。兩份檔案不是檔案系統交易，斷電或強制終止可能留下不匹配的一對檔案，下次不會將其視為有效快取。

`--json` 回傳單一物件，單張與批次結構相同。`frames[]` 包含 `requested_time_sec`、`actual_time_sec`、`path` / `output_path`、`cache_hit` 及上述影格資料。請求層級有 `temporary`、`output_dir`（逐張路徑模式為 null）、`frame_count`、`network.requests`、`network.bytes_received`、`elapsed_sec` 及解碼統計。所有圖片路徑可直接讀取。診斷寫入 stderr，失敗回傳非零退出碼。網路統計只計媒體 HTTP 回應本文，不含登入/API 請求或 HTTP 標頭；耗時從提取開始計算，不含課程查詢與登入。

遠端固定直連，預設嚴格限制 **64 MiB**，索引、重複讀取與失敗重試均計入；以 `--max-fetch-mb 128` 調整，不允許無上限。媒體以 1 MiB 區塊隨機讀取；已確認的 moov 索引範圍內最多合併 4 塊請求，預算不足時使用單塊；讀取快取最多 8 MiB，不把完整 MP4 放入記憶體。只接受正確的 206 / Content-Range、長度及一致的強 ETag 或 Last-Modified；200、授權失敗、重新導向、沒有版本標識/固定 Content-Length 或影片變更都報錯，不退回完整下載。支援 moov 位於頭部或尾部的普通 MP4；HLS、直播、缺少時長/索引或 wheel 不支援的編碼會報錯。長 GOP 或大索引可能需提高流量上限。

普通 `--at` / `--frame` 請求的全部圖片快取有效時，直接回傳，不開啟 MP4 或初始化解碼器；遠端仍需一次 Range 探測驗證來源身分。缺少請求時長紀錄的舊快取會退回讀取索引；視窗或部分命中也沿用正常流程。

`recording-audio`、`recording-frame` 和 `classroom sync --recording-audio` **預設共用原始 MP4 索引快取**。音訊產生的快取可供截圖使用，反過來也一樣；音訊仍透過非同步 Multi-Range 下載並直接重封裝，不重新編碼。

macOS / Linux 的預設目錄為 `${XDG_CACHE_HOME:-~/.cache}/zju-learning/`，Windows 為 `%LOCALAPPDATA%\zju-learning\cache\`。環境變數為空、相對路徑或無效時，分別退回 `~/.cache/zju-learning/`、`~/AppData/Local/zju-learning/cache/`。**這是穩定的索引目錄；預設截圖仍在每次獨立建立的系統暫存目錄，程序結束後保留。** 優先使用完整本地 MP4 時不建立索引快取。

三個命令都可覆寫快取位置、調整容量或關閉快取：

```bash
./zju.py recording-frame --course 89253 --sub 2038840 --at 01:23:45 \
  --index-cache-dir "~/.cache/zju-learning" --index-cache-mb 256 --json
./zju.py recording-audio --course 89253 --sub 2038840 --index-cache-dir "~/.cache/zju-learning"
./zju.py classroom sync 89253 --recording-audio --index-cache-dir "~/.cache/zju-learning"
./zju.py recording-frame --course 89253 --sub 2038840 --at 01:23:45 --no-index-cache --json
```

後續呼叫使用相同快取位置即可跨程序雙向重用；相容舊版指定目錄下的有效快取。檔案位於指定目錄的 `zju-frame-index-v1/` 下，圖片仍遵循原輸出路徑規則。每次遠端 Range 探測確認強 ETag、來源身分及大小後才重用索引；簽名變更不影響相同來源版本，弱 ETag / Last-Modified 則退回正常讀取。課程權限、堂次下架狀態及錄播目錄仍會查詢。只儲存完整 `moov`，不儲存媒體片段、播放 URL 或認證資料，也不額外下載資料來填滿快取。

`--index-cache-mb` 為正整數，總容量預設 **256 MiB**，包含索引檔案中繼資料及寫入中的暫存檔；較舊項目按存取時間淘汰，單個超過 **64 MiB** 的索引不持久快取。中繼資料與讀取的資料塊均驗證 SHA-256；損壞、遺失、無法寫入或容量不足時正常退回網路讀取。寫入者使用跨程序系統鎖，衝突時跳過本次持久化，不等待另一程序的網路請求；已有項目仍可讀取。索引完整後原子發布，異常退出的暫存檔由下次取得鎖的呼叫清理。不再需要時可刪除此命名空間目錄。

索引快取的本地讀取不計入 `network.bytes_received`；網路探測與回退讀取仍受 `--max-fetch-mb` 限制。`cache_hit` 繼續表示**圖片**快取命中；`--force` 重新產生圖片，但仍可使用已驗證索引。`--no-index-cache` 不讀寫持久索引，與 `--index-cache-dir` 互斥；不刪除已有快取。音訊的 `--max-size` 繼續限制輸出檔案大小，不作為網路預算；截圖流量預算不變。

`recording-frame` 使用既有智雲 token 直接請求業務介面，省去學在浙大預檢。認證失效時最多重新登入並重試一次；仍被拒絕則明確報錯。其他命令沿用原認證流程。

離線測試使用 PyAV 產生影片，不需要學校帳號或外部 FFmpeg：

```bash
uv run --with requests --with httpx --with img2pdf --with pillow --with keyring --with numpy --with 'av>=16,<18' \
  python -m unittest discover -s tests
```

### 其他改進

- **增量同步**：以 `.zju_manifest.json` 記錄 upload id，而不是比對檔名和大小；老師換了新版（新 id）才會重抓。
- **下載完整性**：先寫 `.part-*`，核對 `Content-Length` 後才 rename；空檔、截斷、伺服器回的 HTML 錯誤頁都不會被記成已下載。
- **並行下載**：課件預設 4 檔同時，PPT 截圖 8 張同時；每條執行緒有自己的 session，共用 cookie jar。遇到 429/503 會照 `Retry-After` 退讓。
- **PPT 去重**（`--dedup`）：智雲是對投影畫面定時截圖，同一頁會因動畫逐步出現、老師邊講邊寫、翻回前面而被截很多次。一頁的筆畫若全都還在後面那頁（或之前留下的某頁）裡就刪掉，所以動畫只留跑完的那張、手寫只留寫完的那張，註記不會丟。用局部對比找筆畫，白底、黑底、底圖紋理、教學影片都適用；實測三堂課 73→68、81→48、176→99 頁，逐頁核對無誤刪。`--keep-images` 仍保留全部原圖。
- **預設直連**，連不上才改走系統 proxy；連線 timeout 6 秒，排程時不會卡死。只有冪等請求會自動重試，登入 POST 不會被重送。
- **學年判斷**：學校常常不把舊課程標成已結束（`is_closed`），因此改用 `academic_year_id` 找最新學年。
- **同名課程**（不同教學班）分開存放；同一課程裡的同名檔一律加上 id，命名不受 API 回傳順序影響。
- 預設跳過影音檔和 200MB 以上的檔案（通常是軟體安裝包、專題壓縮檔），`--dry-run` 會列出總大小。

修掉的上游邊界狀況：

- 智雲 `search-ppt` 不遵守 `per_page`：常常第 1 頁就回傳全部，下一頁再重複一次。ZLA 假設每頁最多 100 張，超過 100 頁的課會一直重試然後失敗；這裡改成依序去重。
- 轉錄 API 對「還沒有語音資料」回傳 `code=10002`，現在當成「無轉錄」處理，不再中止整批。
- **明文傳送登入憑證**：智雲 PPT 截圖網址是 `http://`，而 `.zju.edu.cn` 的 SSO cookie（包括 `iPlanetDirectoryPro`）沒有設 `Secure`，照常下載就會把登入憑證用明文送出。這裡把學校主機的網址升級成 HTTPS，而且所有 `http://` 請求都不帶 Cookie 和 Authorization。
- `courses.zju.edu.cn` 與 `identity.zju.edu.cn` 只支援 1024-bit DHE／靜態 RSA，OpenSSL 3 預設拒絕連線（`DH_KEY_TOO_SMALL`）。`SECLEVEL=1` 只套用在這兩台，其他主機維持預設的 TLS 設定。
- 智雲的 `_token` cookie 設在 `.zju.edu.cn` 父網域，而不是 `classroom.zju.edu.cn`。

## 附件下載來源

`sync` 對每個附件依優先序嘗試以下來源，採用第一個能回檔的：

1. `/api/uploads/reference/{rid}/blob`：常規下載
2. `/api/uploads/{id}/blob`：原始檔案
3. `/api/uploads/{id}/blob?refer_id={活動id}&refer_type=learning_activity`：reference 參數與官方網頁前端下載鈕送出的相同（`classroom` 活動用 `classroom_activity`、考試不帶）；部分活動的附件只有此來源提供，來源標記 `排程原檔`
4. `/api/uploads/reference/document/{rid}/url?preview=true`：預覽器轉出的 PDF（[Kcalb35/Tronclass-pdf-downloaderforChrome](https://github.com/Kcalb35/Tronclass-pdf-downloaderforChrome)、[fish-can/TronClass-PDF-Downloader](https://github.com/fish-can/TronClass-PDF-Downloader) 採用的方式）

**排程尚未開放的活動**（`is_started=false`）通常由來源 3 照常下載；若所有來源都回 403，該檔案標記 `[未開放]（開放時間）`，不算失敗，開放後下次 `sync` 會自動抓。

## 安全性

- 密碼只存在系統憑證庫。macOS 由系統 `security`、Windows 由 Python `getpass` 在終端機提示輸入，不會出現在命令列參數或 shell 歷史紀錄。也可以改用環境變數 `ZJU_USER` / `ZJU_PASS`。
- 登入時密碼先用 CAS 提供的公鑰加密再送出，跟網頁登入的做法相同。
- Session cookie 以 JSON（不是 pickle）快取在 `~/.config/zju-learning/cookies.json`；在 macOS／Linux 上檔案權限是 `0600`，目錄是 `0700`。快取位置可用環境變數 `ZJU_STATE_DIR` 覆寫。
- Cookie 只會透過 HTTPS 送往 `*.zju.edu.cn`，明文 `http://` 請求一律不帶。沒有任何遙測。
- TLS 驗證失敗會直接報錯，不會自動重試或改走 proxy，避免把中間人攻擊誤當成網路不穩。

## 免責聲明

在 macOS 和 Windows 上實測過；Linux 理論上可以使用，但沒有測試過。

僅供個人學習使用。課件的著作權屬於授課教師與學校，請勿散布下載的內容；使用時請遵守學校的相關規定，不要高頻或大量抓取。學校的 API 沒有公開文件，隨時可能改版。

## 致謝

- [PeiPei233/zju-learning-assistant](https://github.com/PeiPei233/zju-learning-assistant)（MIT）：登入流程、學在浙大與智雲課堂的 API 呼叫都移植自它的 `src-tauri/src/zju_assist.rs`。本專案沿用 MIT 授權並保留其版權聲明，見 [LICENSE](LICENSE)。
- [eWloYW8/ZJU-course-material-download](https://github.com/eWloYW8/ZJU-course-material-download)（MIT）、[Kcalb35/Tronclass-pdf-downloaderforChrome](https://github.com/Kcalb35/Tronclass-pdf-downloaderforChrome)、[fish-can/TronClass-PDF-Downloader](https://github.com/fish-can/TronClass-PDF-Downloader)、[xzzd-pro/xzzd-pro](https://github.com/xzzd-pro/xzzd-pro)：參考了關閉下載時的端點做法（沒有複製程式碼）。

## 授權

[MIT](LICENSE)
