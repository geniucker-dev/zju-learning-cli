# zju-learning-cli

简体中文 | [繁体中文](README.zh-TW.md)

学在浙大（courses.zju.edu.cn）与智云课堂（classroom.zju.edu.cn）的轻量命令行工具：同步课件、把智云课堂的 PPT 截图合并成 PDF（可去除重复截图）、导出课堂语音转录、查待办、按需提取录播视频帧。

它是 [PeiPei233/zju-learning-assistant](https://github.com/PeiPei233/zju-learning-assistant)（ZLA）的 CLI 移植版。ZLA 是很好用的桌面 GUI，但没办法写进脚本、定时任务或让 AI agent 调用；这个项目把它的 API 逻辑改写成轻量 Python CLI，补上增量同步、并行下载、PPT 去重，也修了几个上游的边界状况。

## 功能

| 指令 | 作用 |
| --- | --- |
| `zju login` | 设置学号，密码存进系统凭据库（macOS Keychain、Windows 凭据管理器；其他系统走 [keyring](https://pypi.org/project/keyring/)） |
| `zju courses [--all]` | 课程列表（默认只列最新学年） |
| `zju sync [课程...]` | 增量同步课件到 `<输出目录>/<课程>/` |
| `zju todo` | 待办事项，依截止时间排序（本地时区） |
| `zju activities [课程…] [--type forum homework …]` | 列出所有活动（课件、视频、作业、讨论、网页、链接、测验），附状态与截止时间 |
| `zju show <活动id> [--read]` | 活动详情：说明、附件、完成条件；作业显示自己的提交状态，讨论显示帖数及自己的发帖／回帖记录；`--read` 下载附件并打印文字（docx/pptx/pdf/doc，贴图标成 [图]） |
| `zju forum list <讨论id> [--mine] [--full]` / `forum read <topic>` | 列出讨论帖（`--mine` 只列自己的发帖与回帖）／读帖与回复 |
| `zju forum post <讨论id> --title … --body …` / `forum reply <topic> --body …` | 发帖／回帖，可 `--body-file`、`--attach` 附件 |
| `zju upload 文件…` | 上传文件到学在浙大，输出 upload id |
| `zju submit <作业id> --file … [--body …] [--draft] [-y]` | 交作业；默认提交前确认，已截止会拦下 |
| `./zju.py classroom courses [--has-tasks] [--json]` | 智云个人课程，显示课程 ID、学期、教师和任务数 |
| `./zju.py classroom sync [课程...] [-j 4] [--recording] [--recording-audio]` | 同步智云个人课程的 PPT 和转写，可选录播、音频 |
| `zju classroom search 关键字` | 在智云课堂找课，取得 `course_id` |
| `zju classroom subs <course_id>` | 列出该课每一堂的 `sub_id` |
| `zju classroom day [日期] [--days N]` | 某天（或最近 N 天）自己的课，含追踪中的课 |
| `./zju.py classroom add\|rm <course_id>...` | 追踪或移除课表外课程；`--days` 也包含追踪课程，目录为 `课名 老师 (课程ID)` |
| `./zju.py classroom tracked` | 列出追踪中的课程 |
| `zju ppt --course <id> \| --days N [--dedup]` | 智云 PPT 截图 → `<课程> (<id>)/智云PPT/<堂> (<sub_id>).pdf` |
| `zju transcript --course <id> \| --days N` | 语音转录 → `<课程> (<id>)/转录/<堂> (<sub_id>).txt\|srt\|md` |
| `./zju.py recording --course <id> \| --days N [-j 4]` | 智云录播 → `<课程> (<id>)/录播/<堂> (<sub_id>).mp4`，多连接分片下载 |
| `./zju.py recording-audio --course <id> \| --days N [-j 32]` | 优先从本地录播提取音轨，否则只下载音频 → `<课程> (<id>)/音频/<堂> (<sub_id>).m4a` |
| `./zju.py recording-frame --course <id> --sub <id> --at HH:MM:SS [--json]` | 本地优先，远端 HTTP Range 按需提取真实视频帧；默认保存在独立系统临时目录 |


## 安装

需要 [uv](https://docs.astral.sh/uv/) 和 Python >=3.10（uv 可管理 Python）。依赖写在文件头（PEP 723），第一次运行时 uv 会自动安装。只需 `zju.py`，可以单独复制到任意目录使用。

```bash
git clone https://github.com/geniucker-dev/zju-learning-cli.git
cd zju-learning-cli
./zju.py login
```

没有 uv 的话：`pip install requests httpx img2pdf pillow keyring numpy 'av>=16,<18'`，再用 `python zju.py ...` 运行。

视频帧解码使用 [PyAV](https://pyav.basswood.io/docs/stable/) 的跨平台 wheel。wheel 可内置 FFmpeg 编解码库，用户**无需安装或调用系统 ffmpeg / ffprobe**；其他命令不会初始化视频解码库。常见 macOS、Windows、Linux 的 Python 版本有预编译 wheel；无对应 wheel 的平台需要自行解决库构建，不会回退调用外部程序。

也可以不开终端，直接双击登录脚本（每次都会清掉上一次的学号、密码和 cookie，重新登录）：

- macOS：`zju_login.command`，在访达里双击会打开一个终端窗口。用 git clone 下载的可以直接双击；下载 zip 的会被 Gatekeeper 拦下，先运行一次 `chmod +x zju_login.command && xattr -c zju_login.command`。
- Windows：`zju_login.bat`，需要 Python 启动器 `py -3.11`，并先用 pip 装好上面的依赖。

## 使用

```bash
zju sync --dry-run                 # 先看会下载什么、总共多大
zju sync                           # 最新学年全部课程
zju sync 微积分 123456              # 指定课程（名称片段或 id，id 用 zju courses 查）
zju sync --videos --max-size 0     # 连音视频和大文件一起抓
zju sync -j 8                      # 并行数（默认 4）

zju classroom day --days 7
zju ppt --days 1 --dedup           # 今天所有课的 PPT，重复截图只留最完整的一张
zju transcript --days 1 --format md
```

输出目录的优先级：`--out` > 环境变量 `ZJU_OUT` > `~/.config/zju-learning/config.json` 的 `"out"` > `~/ZJU-Courses`。

`sync --videos` 下载学在浙大活动中的音视频附件；`./zju.py recording` 下载智云课堂的课堂录播，两者来源不同。

程序提示与帮助使用简体中文。智云资料统一保存在 `<课程名> (<course_id>)` 下，按 `智云PPT`、`转录`、`录播`、`音频` 分类，文件名为 `<堂次名> (<sub_id>).扩展名`。`--keep-images` 保留的截图也使用带堂次 ID 的子目录。课程名和转写内容保持平台原文；不识别或迁移旧目录。

定时任务示例（cron，每天 22:00）：

```cron
0 22 * * * ~/.local/bin/zju sync && ~/.local/bin/zju ppt --days 1 && ~/.local/bin/zju transcript --days 1
```

退出码：`0` 成功；`1` 设置、登录或 API 错误；`2` 部分文件或堂次失败（其余照常完成）。

## 跟 ZLA 的差异

### PPT 时间事件映射

每堂 PPT 除 PDF 外还生成同名 `.json`，无论是否启用 `--dedup` 都保留截图事件。`events` 保存每张原始截图的 `created_sec`、原始接口元数据、图片 SHA-256、对应的 `pdf_page` 及代表它的 `representative_event_index`；`pages` 保存每页的代表事件和全部关联事件。事件编号从 0 开始，PDF 页码从 1 开始。

动画补全和翻回旧页的事件仍保留，多个事件可以映射到同一 PDF 页。`relationship: represented` 表示由保留页面代表，并不保证当时画面完全相同；被去掉的空白过场标记为 `blank`，页码为 `null`。若所有截图都是空白，则保留原有行为，全部输出为 PDF 页。缺失或无效时间为 `null`，不会按页码或截图间隔推算时间。时间是截图观测点，不是页面显示区间；与音轨起点的对齐尚未验证（`audio_alignment: unverified`）。

默认记录原始图片身份但不保留图片文件；加 `--keep-images` 保留全部原图，映射中的 `image_file` 为相对于 JSON 所在目录的路径。已有 PDF 缺少同名 JSON 时会重新生成这一堂 PDF 并补建映射；两者齐全才增量跳过。`--force` 可重新生成 PDF 和映射。

### 智云课程同步

默认同步全部个人录播课程的所有堂次，下载 PPT（PDF）和 Markdown 转写；也可用课程 ID 或名称片段选课。`--recording` 同时下载录播，`--recording-audio` 同时获取音频。`-j` / `--jobs` 是整个同步的 worker 总数（默认 4）：每张 PPT 截图、每份转写各占一个 worker，录播的每个分片各占一个 worker，不再额外开录播分片线程池。转写、PPT、录播依次处理并复用同一个线程池。音频在这些步骤完成后处理，远端音频请求也遵循 `-j` 上限（同步默认 4）；单独 `recording-audio` 命令默认 32。搭配 `--recording --recording-audio` 时，音频直接从刚下载的录播提取。

使用统一资料目录和增量跳过规则，录播继续支持跨次运行的断点续传。`--force` 重新下载资料并丢弃录播分片进度；`--format txt|srt|md` 选择转写格式，`--dedup` 去除重复 PPT 截图，`--keep-images` 保留全部截图，`--max-size MB` 限制录播或音频单文件大小（0 为不限）。

`--dry-run` 不下载、不写文件：PPT 和转写按现有文件及堂次预览路径，标记为待检查的资料可能尚未发布；录播读取目录后预览。单项失败仍继续处理其他资料，最终退出码为 2。

Markdown 转写的每段带开始和结束时间，例如 `**[00:01:23 → 00:01:38]** 内容`，精确到秒；SRT 精确到毫秒。已有 `.md` 仍会增量跳过，不自动升级；用独立 `transcript --format md --force` 重新生成，可避免重下 PPT、录播和音轨。

接口标记为已下架（`show: "no"`）的堂次，录播和音轨会显示“已下架，跳过”，不计为失败；PPT 和转写仍按资料可用性处理。

```bash
./zju.py classroom sync -j 8
./zju.py classroom sync 89418 --recording -j 8
./zju.py classroom sync 人工智能 --dry-run --recording
```

### 智云录播下载

默认列出智云「我的课程」中的全部课程，包括任务数为 0 的课程；`--has-tasks` 只列任务数大于 0 的课程。任务数与网页一致，不代表一定有可下载录播。`--json` 输出 `course_id`、`title`、`teacher`、`term`、`type` 和 `task_count`。这些课程 ID 可用于 `classroom subs`、`ppt`、`transcript`、`recording` 和 `recording-audio`；顶层 `courses` 列出的则是学在浙大的 ID。

可以直接运行 `./zju.py`，无需创建软链接。使用智云课程 ID（与学在浙大不同）：

```bash
./zju.py classroom courses
./zju.py classroom courses --has-tasks
./zju.py classroom courses --json
./zju.py classroom search 人工智能
./zju.py classroom subs 89418
./zju.py recording --course 89418 --dry-run       # 只列待下载录播，不下载、不写文件
./zju.py recording --course 89418 --sub 2019095   # 下载指定堂次
./zju.py recording --course 89418 -j 8           # 整门课；每堂录播最多 8 个连接
./zju.py recording --days 7                     # 最近 7 天自己的课堂
./zju.py recording --course 89418 --max-size 4096 # 单堂录播限制 4096MB；默认 0 = 不限
```

默认每堂录播使用 4 个连接，按 32MiB 分片并行下载，显示进度和平均速度。逐个下载录播，分片失败最多尝试 3 次；服务器不支持 Range 时自动回退到单连接。检查每片的响应范围、总大小、实际字节数及最终 MP4 文件头，全部成功才替换目标文件并写入下载清单。若服务器提供 ETag 或 Last-Modified，使用 If-Range 防止混合不同版本。

已下载且大小符合清单的文件会跳过；暂无回放的堂次会跳过，下次运行重新查询。多段回放地址分别保存为带编号的 MP4。录播目录和文件名包含课程、堂次 ID，避免同名覆盖。当前支持直接 MP4，不支持 HLS 播放列表。`--out` 放在子命令前，例如 `./zju.py --out ~/ZJU-Courses recording --course 89418`。

**录播支持跨次运行的断点续传，默认开启。** 中断或失败时，在录播旁保留隐藏的 `.录播名.mp4.part` 数据文件和 `.录播名.mp4.part.json` 进度文件。重新执行相同命令（输出目录不变）即可继续，`-j` 可以调整；只补下载未完成或校验损坏的分片，未完成的单片从头重下。每片完成后将数据写入磁盘，再原子保存进度及 SHA-256；再次运行时校验已完成分片。全部完成后才替换最终 MP4，并清理数据和进度文件。隐藏的 `.录播名.mp4.download.lock` 小锁文件保留，用于阻止多个程序同时下载同一录播。

续传前核对远端 ETag（或 Last-Modified）、总长度及来源；远端版本变化、本地进度损坏、数据文件缺失时重新下载。地址的签名参数刷新不会影响续传，仍须通过远端版本核对。服务器不支持 Range 或没有可用版本标记时，从头下载。`--force` 明确丢弃已完成分片，从头重下；不加 `--force` 才会沿用进度。课件、PPT 和转写的下载行为不变。

目录接口及视频地址提取参考了 Cold_Ink 的 [智云课堂批量下载](https://greasyfork.org/scripts/514465)（MIT）；本项目按实际接口兼容字符串和列表地址，并使用 Python 流式分片下载。

### 智云音频

无需安装 `ffmpeg` 或其他音视频工具。程序直接读取 MP4 索引、复制第一条音轨并重建 M4A，不重新编码：

```bash
./zju.py recording-audio --course 89418 --sub 2019095    # 指定堂次
./zju.py recording-audio --course 89418                  # 整门课，默认 32 个协程并发
./zju.py recording-audio --days 7 -j 8                   # 最近 7 天，调整并发数
./zju.py recording-audio --course 89418 --dry-run         # 预览本地提取或远端下载，不写文件
./zju.py classroom sync 89418 --recording-audio -j 32     # PPT、转写和音频
./zju.py classroom sync 89418 --recording --recording-audio -j 8
```

优先使用统一目录中已完成的录播；未完成的 `.part` 不算完整视频。没有视频时，读取远端 MP4 索引，按音轨偏移下载音频范围，不先下载整个视频。默认最多 32 个异步请求，每批最多 360 个音频分片；Range 请求头接近 8KiB 时自动缩小批次。音频范围请求固定直连，不读取环境代理，也不回退到代理。

路径为 `<输出目录>/<课程名称> (<course_id>)/音频/<堂次名称> (<sub_id>).m4a`；多段回放分别编号。已有音频增量跳过，`--force` 重新提取或下载；`--max-size MB` 限制最终音频大小，而不是原视频大小。成功后才原子替换最终文件并记录清单，失败或中断清理临时音频，不覆盖原文件。音频暂不支持跨次运行的断点续传。

当前支持有完整音轨索引的直接 MP4；没有音轨、HLS、分片 MP4或不支持多范围请求的服务器会报错，不自动下载整个视频。本地视频路径不受远端 Range 支持情况影响。

### 按需提取录播视频帧

供用户或 Agent 临时查看黑板、推导和演示画面；不会加入 `classroom sync` 的默认资料。完整本地 MP4 的路径和大小必须符合原下载清单，本地命中时无需认证或课程查询，否则使用远端来源；`.part` 不作为完整视频。全局 `--out` 只用于寻找本地录播，不控制截图保存位置。

```bash
# 默认创建独立系统临时目录；输出每张图片的完整绝对路径
./zju.py recording-frame --course 89253 --sub 2038840 --at 01:23:45

# 单张图片的完整文件路径，自动创建父目录；也支持相对路径和 PNG
./zju.py recording-frame --course 89253 --sub 2038840 --at 5025.125 --output "./images/板书.png"

# 多时间点，同一个容器、读取缓存及解码上下文
./zju.py recording-frame --course 89253 --sub 2038840 \
  --at 01:23:40 --at 01:23:45 --at 01:23:50 --output-dir "./images/课堂"

# 每张图片分别指定完整路径（只在第一个 = 处分隔）
./zju.py recording-frame --course 89253 --sub 2038840 \
  --frame "01:23:40=./before/板书.jpg" --frame "01:23:45=./after/板书 = 完整.png"

# 前后各 10 秒，每隔 2 秒；越过视频边界的窗口部分会裁剪，始终包含中心时间
./zju.py recording-frame --course 89253 --sub 2038840 \
  --at 01:23:45 --around 10 --interval 2 --output-dir ./sequence

# 多段录播必须明确选段；Agent 使用 JSON，stdout 没有进度或诊断日志
./zju.py recording-frame --course 89253 --sub 2038840 --part 1 --at 01:23:45 --json
```

时间接受 `HH:MM:SS`、`HH:MM:SS.sss` 或浮点秒数，必须非负且小于视频时长。`--at` 相对选中 MP4 分段的起点，**不假定**与转写 BeginSec、PPT created_sec 或音轨时间对齐。只有一个分段时自动选择；多个分段省略 `--part` 会报错。按 PTS 返回请求时刻正在显示的帧（两帧之间取前一帧），并记录真实 `actual_time_sec`。非关键帧从之前的关键帧开始解码，支持 B 帧重排、常见可变帧率和非零流起点。

`--output` 只能搭配单个 `--at`；`--output-dir` 可搭配单帧、批量或窗口；`--frame` 可重复，但不能与 `--at`、`--output`、`--output-dir` 或窗口选项混用。不同时间不能写入同一个完整路径。Windows 下可使用引号包围 `"01:23:45=C:\课程\板书.jpg"`；路径中的空格、中文和 `=` 会保留。

所有模式默认最多请求 **100 帧**，可用 `--max-frames 200` 显式调整，必须为正整数。重复 `--at` / `--frame` 每项都会输出图片，因此逐项计数；窗口先裁剪负时间，以纳秒精度合并采样点，重叠窗口不重复输出，始终包含中心点。在认证、读取视频及创建图片前检查数量；窗口计数在视频尾裁剪之前进行，最终输出可能更少。

默认用 `tempfile.mkdtemp()` 创建 `zju-frames-<随机串>` 系统临时目录，不硬编码 `/tmp`。**进程退出后图片及元数据仍存在**，调用方可读取、分析后自行删除；系统之后也可能清理。显式路径严格写入指定位置，不复制到课程同步目录。自动文件名例如 `89253_2038840_p01_01-23-45-000.jpg`，包含课程、堂次、分段和时间。JPEG 默认质量 95、原分辨率，可用 `--quality 1…100`；自动命名用 `--format png`，完整文件路径按 `.jpg` / `.jpeg` / `.png` 扩展名选择格式。

每张图片旁有 `<图片名>.json`，记录来源身份、请求/实际时间、timebase、编码、尺寸、路径和图片 SHA-256，以及生成请求的网络、耗时、输出数量和临时目录标记。缓存仅在来源身份、时间、编码设置和图片校验全部一致时复用。本地身份包含大小、纳秒修改/变更时间及 inode；远端使用无签名资源路径摘要、总大小与强 ETag。Last-Modified 仅用于本次读取的一致性检查，不能证明跨次内容相同：缺少强 ETag 时仍能取帧，但不会复用之前的截图；显式路径已有图片时须使用 `--force` 或另选路径。签名 URL、Cookie 和 Authorization 不保存。默认临时目录不提供跨次缓存；指定同一目录或路径可复用有效缓存。自动名称冲突时添加 `_1` 等后缀；显式完整路径已有其他内容或缓存失效则报错。`--force` 重新编码并允许覆盖。图片与 JSON 都先在目标目录的临时子目录中完整写入，再加系统锁发布。写入失败时清理本次新建结果；`--force` 发布失败则恢复被替换的原文件。哈希及元数据必须一致才能成为有效缓存。目录保留小型隐藏 `.frame.lock` 文件，系统在进程退出时释放锁。两份文件不是文件系统事务，断电或强制终止可能留下不匹配的一对文件，下次不会将它们作为有效缓存。

`--json` 返回一个对象，单帧与多帧结构相同：`frames[]` 含 `requested_time_sec`、`actual_time_sec`、`path` / `output_path`、`cache_hit` 及上述图片元数据；请求级字段含 `temporary`、`output_dir`（逐帧路径模式为 null）、`frame_count`、`network.requests`、`network.bytes_received`、`elapsed_sec` 和解码统计。所有路径可直接供 Agent 读取。诊断写入 stderr，失败返回非零退出码。网络统计只计媒体 HTTP 响应体，不含认证/API 请求或 HTTP 头；耗时从提取开始计算，不含课程查询与登录。

远端默认直连，严格限量 **64 MiB**（索引、重复读取及失败重试均计入），可用 `--max-fetch-mb 128` 调整，不允许无上限。媒体按 1 MiB 块随机读取；已确认的 moov 索引范围内最多合并 4 块请求，预算不足时使用单块；内存读取缓存最多 8 MiB；不会把完整 MP4 放进内存。只接受正确的 206 / Content-Range、长度和一致的强 ETag 或 Last-Modified。200、鉴权失败、重定向、缺少版本标识/固定 Content-Length 或源文件变化均报错，不退回整文件下载。支持 moov 位于文件头或尾的普通 MP4；HLS、直播、缺少时长/索引或 wheel 不支持的编码会报错。较长 GOP 或很大的索引可能需要调大流量上限。

目前不持久缓存视频索引或 Range 数据；不同调用的新时间点仍需读取索引。默认仅保留本次有界内存读取缓存，不额外创建媒体缓存目录。

离线测试通过 PyAV 生成视频，无需学校账号或外部 FFmpeg：

```bash
uv run --with requests --with httpx --with img2pdf --with pillow --with keyring --with numpy --with 'av>=16,<18' \
  python -m unittest discover -s tests
```

### 其他改进

- **增量同步**：以 `.zju_manifest.json` 记录 upload id，而不是比对文件名和大小；老师换了新版（新 id）才会重新下载。
- **下载完整性**：先写 `.part-*`，核对 `Content-Length` 后才 rename；空文件、截断、服务器回的 HTML 错误页都不会被记成已下载。
- **并行下载**：课件默认 4 个文件同时，PPT 截图 8 张同时；每条线程有自己的 session，共用 cookie jar。遇到 429/503 会照 `Retry-After` 退让。
- **PPT 去重**（`--dedup`）：智云是对投影画面定时截图，同一页会因动画逐步出现、老师边讲边写、翻回前面而被截很多次。一页的笔画若全都还在后面那页（或之前留下的某页）里就删掉，所以动画只留跑完的那张、手写只留写完的那张，批注不会丢。用局部对比找笔画，白底、黑底、底图纹理、教学视频都适用；实测三堂课 73→68、81→48、176→99 页，逐页核对无误删。`--keep-images` 仍保留全部原图。
- **默认直连**，连不上才改走系统 proxy；连接 timeout 6 秒，定时运行时不会卡死。只有幂等请求会自动重试，登录 POST 不会被重送。
- **学年判断**：学校常常不把旧课程标成已结束（`is_closed`），因此改用 `academic_year_id` 找最新学年。
- **同名课程**（不同教学班）分开存放；同一课程里的同名文件一律加上 id，命名不受 API 返回顺序影响。
- 默认跳过音视频文件和 200MB 以上的文件（通常是软件安装包、项目压缩包），`--dry-run` 会列出总大小。

修掉的上游边界状况：

- 智云 `search-ppt` 不遵守 `per_page`：常常第 1 页就返回全部，下一页再重复一次。ZLA 假设每页最多 100 张，超过 100 页的课会一直重试然后失败；这里改成依序去重。
- 转录 API 对「还没有语音数据」返回 `code=10002`，现在当成「无转录」处理，不再中止整批。
- **明文发送登录凭证**：智云 PPT 截图网址是 `http://`，而 `.zju.edu.cn` 的 SSO cookie（包括 `iPlanetDirectoryPro`）没有设 `Secure`，照常下载就会把登录凭证用明文送出。这里把学校主机的网址升级成 HTTPS，而且所有 `http://` 请求都不带 Cookie 和 Authorization。
- `courses.zju.edu.cn` 与 `identity.zju.edu.cn` 只支持 1024-bit DHE／静态 RSA，OpenSSL 3 默认拒绝连接（`DH_KEY_TOO_SMALL`）。`SECLEVEL=1` 只套用在这两台，其他主机维持默认的 TLS 设置。
- 智云的 `_token` cookie 设在 `.zju.edu.cn` 父网域，而不是 `classroom.zju.edu.cn`。

## 附件下载来源

`sync` 对每个附件依优先序尝试以下来源，采用第一个能回档的：

1. `/api/uploads/reference/{rid}/blob`：常规下载
2. `/api/uploads/{id}/blob`：原始文件
3. `/api/uploads/{id}/blob?refer_id={活动id}&refer_type=learning_activity`：reference 参数与官方网页前端下载钮送出的相同（`classroom` 活动用 `classroom_activity`、考试不带）；部分活动的附件只有此来源提供，来源标记 `排程原档`
4. `/api/uploads/reference/document/{rid}/url?preview=true`：预览器转出的 PDF（[Kcalb35/Tronclass-pdf-downloaderforChrome](https://github.com/Kcalb35/Tronclass-pdf-downloaderforChrome)、[fish-can/TronClass-PDF-Downloader](https://github.com/fish-can/TronClass-PDF-Downloader) 采用的方式）

**排程尚未开放的活动**（`is_started=false`）通常由来源 3 照常下载；若所有来源都回 403，该文件标记 `[未开放]（开放时间）`，不算失败，开放后下次 `sync` 会自动抓。

## 安全性

- 密码只存在系统凭据库。macOS 由系统 `security`、Windows 由 Python `getpass` 在终端提示输入，不会出现在命令行参数或 shell 历史记录。也可以改用环境变量 `ZJU_USER` / `ZJU_PASS`。
- 登录时密码先用 CAS 提供的公钥加密再送出，跟网页登录的做法相同。
- Session cookie 以 JSON（不是 pickle）缓存在 `~/.config/zju-learning/cookies.json`；在 macOS／Linux 上文件权限是 `0600`，目录是 `0700`。缓存位置可用环境变量 `ZJU_STATE_DIR` 覆盖。
- Cookie 只会通过 HTTPS 送往 `*.zju.edu.cn`，明文 `http://` 请求一律不带。没有任何遥测。
- TLS 验证失败会直接报错，不会自动重试或改走 proxy，避免把中间人攻击误当成网络不稳。

## 免责声明

在 macOS 和 Windows 上实测过；Linux 理论上可以使用，但没有测试过。

仅供个人学习使用。课件的著作权属于授课教师与学校，请勿散布下载的内容；使用时请遵守学校的相关规定，不要高频或大量抓取。学校的 API 没有公开文档，随时可能改版。

## 致谢

- [PeiPei233/zju-learning-assistant](https://github.com/PeiPei233/zju-learning-assistant)（MIT）：登录流程、学在浙大与智云课堂的 API 调用都移植自它的 `src-tauri/src/zju_assist.rs`。本项目沿用 MIT 授权并保留其版权声明，见 [LICENSE](LICENSE)。
- [eWloYW8/ZJU-course-material-download](https://github.com/eWloYW8/ZJU-course-material-download)（MIT）、[Kcalb35/Tronclass-pdf-downloaderforChrome](https://github.com/Kcalb35/Tronclass-pdf-downloaderforChrome)、[fish-can/TronClass-PDF-Downloader](https://github.com/fish-can/TronClass-PDF-Downloader)、[xzzd-pro/xzzd-pro](https://github.com/xzzd-pro/xzzd-pro)：参考了关闭下载时的端点做法（没有拷贝代码）。

## 授权

[MIT](LICENSE)
