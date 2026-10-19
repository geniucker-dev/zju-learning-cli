# zju-learning-cli

简体中文 | [繁體中文](README.zh-TW.md)

学在浙大与智云课堂的独立、跨平台单文件 CLI。使用 uv、PEP 723 和 Python wheel；只需分发 `zju.py`，不需要安装项目包或系统 FFmpeg。

## 安装与登录

需要 [uv](https://docs.astral.sh/uv/)；Python >=3.10 可由 uv 管理。首次运行自动安装文件头声明的依赖。

```bash
git clone https://github.com/geniucker-dev/zju-learning-cli.git
cd zju-learning-cli
uv run --script zju.py login
```

macOS/Linux 也可直接 `./zju.py login`。本文的 `./zju.py …` 在 Windows 使用 `uv run --script zju.py …`，无需创建软链接。双击 `zju_login.command` / `zju_login.bat` 会运行 `login --reset`，重新设置凭据和会话，**保留资料库及追踪课程设置**。

密码保存在系统凭据库（macOS Keychain，其他平台使用 keyring）；也支持 `ZJU_USER` / `ZJU_PASS`。配置和会话位于 `~/.config/zju-learning/`，可用 `ZJU_STATE_DIR` 指定整个状态目录。不要将凭据放进课程目录。

## 命令分组

```bash
./zju.py --help
./zju.py lms --help
./zju.py classroom --help
```

| 命令 | 用途 |
| --- | --- |
| `login [学号] [--reset]` | 认证及凭据 |
| `lms courses [--all] [--json]` | 学在浙大课程，默认最新学年；JSON 与文本采用相同筛选 |
| `lms sync ID…` / `lms sync --all` | 增量同步指定课程或全部个人课程 |
| `lms todo [--json]` | 待办 |
| `lms activities [课程…] [--all] [--type …] [--json]` | 活动列表 |
| `lms show 活动ID [--read]` | 详情；`--read` 获取附件并提取文字 |
| `lms forum list/read/post/reply …` | 讨论区查询、发帖和回复 |
| `lms upload 文件…` | 上传附件 |
| `lms submit 活动ID --file 文件… [-y] [--draft]` | 交作业；默认确认，截止时拦截 |
| `classroom courses [--has-tasks] [--json]` | 智云个人课程，ID 与 LMS 不同 |
| `classroom search 关键词 [--teacher 教师]` | 搜索远端课程 |
| `classroom subs --course ID [--json]` | 远端堂次 |
| `classroom day [YYYY-MM-DD] [--days N]` | 截至该日的最近 N 天课堂 |
| `classroom add/rm ID…`、`classroom tracked` | 管理课表外的追踪课程，不改变资料目录名 |
| `classroom sync ID…` / `classroom sync --all` | PPT PDF、事件 JSON、Markdown，可选音视频 |
| `classroom ppt/transcript/recording/recording-audio` | 单独获取某类资料，同时更新课程元数据 |
| `classroom context` | 完全离线的近似上下文定位 |
| `classroom recording-frame` | 本地优先，远端 Range 按需取帧 |

所有智云资料命令都在 `classroom` 下。批量同步必须选择课程或明确 `--all`；不接受名称片段作为同步 ID。单项命令支持 `--course ID --sub ID…` 或 `--days N`，未指定课程时默认今天。`--days` 与课程选择互斥，`--sub` 必须搭配课程 ID 或课程目录。

## 三种路径，各司其职

- `--library DIR`：多门课程目录的直接父目录。
- `--course-dir DIR`：单门课程的实际目录，`course.json` 直接在其中。
- `--output FILE` / `--output-dir DIR` / `--frame TIME=FILE`：截图输出。

路径参数放在对应子命令后。`--library` 与 `--course-dir` 互斥；显式路径按原样使用，不追加平台父目录。课程以 `(platform, course_id)` 识别，不按显示名称判断身份。

默认资料库为 `~/ZJU-Courses/classroom` 和 `~/ZJU-Courses/lms`。配置示例（使用绝对路径或 `~`）：

```json
{
  "libraries": {
    "classroom": "~/ZJU-Courses/classroom",
    "lms": "~/ZJU-Courses/lms"
  }
}
```

优先级：命令的显式目录 > 对应平台 `libraries` 配置 > 默认目录。旧 `--out`、`ZJU_OUT`、配置 `out`、根目录 Manifest 均不属于本版本规范，无兼容或自动迁移。

### 集中式资料库

```bash
./zju.py classroom sync 89253 89418 --library ~/ZJU-Courses/classroom --recording-audio -j 32
./zju.py classroom sync --all
./zju.py lms sync 123456 --library ~/ZJU-Courses/lms
./zju.py lms sync --all --videos --max-size 0
```

```text
ZJU-Courses/
├── classroom/
│   ├── 现代控制理论（研） (89253)/
│   └── 人工智能的现代方法（研） (89418)/
└── lms/
    └── 课程名称 (lms-123456)/
```

同一资料库只检查直接子目录的 `course.json`。已有唯一相同 ID 就复用目录；课程改名只更新元数据。多个同 ID 副本会报错，必须明确 `--course-dir`。不递归扫描磁盘，也不按更新时间挑选。

### 自己的课程文件夹内放智云子目录

```bash
./zju.py classroom sync 89253 --course-dir "/path/to/Class/现代控制理论/智云课堂" --recording-audio
# 后续省略 ID；身份来自 course.json
./zju.py classroom sync --course-dir "/path/to/Class/现代控制理论/智云课堂" --recording-audio
```

### 直接与讲义、作业混放

```bash
./zju.py classroom sync 89253 --course-dir "/path/to/Class/现代控制理论" --recording-audio
```

首次初始化必须给 ID。已有 `course.json` 可省略 ID；同时给 ID 时必须匹配。一个 `--course-dir` 只能同步一门课。工具不登记、删除或覆盖未管理的老师讲义、作业等用户文件，即使指定 `--force` 也一样。碰到目标文件冲突时报告错误。

写入命令不会根据当前目录猜测目标。只读查询及取帧可识别当前目录自身的 `course.json`；显式路径优先。课程目录可以整体移动、复制或重命名，内部引用和完成状态仍然有效。

## 课程目录与增量状态

```text
课程目录/
├── course.json
├── .zju/
│   └── downloads.json
├── 智云PPT/课堂名称 (堂次ID).pdf、同名.json
├── 转录/课堂名称 (堂次ID).md
├── 音频/课堂名称 (堂次ID).m4a
└── 录播/课堂名称 (堂次ID).mp4
```

多段音视频使用 ` - 01`、` - 02` 后缀；状态中明确登记 `part` 和 `part_count`，不靠文件名判断分段身份。LMS 将附件放在 `附件/原名 (upload_id).扩展名`，同名课程和附件始终以 ID 隔离。

`course.json` 的格式版本为 2，主要字段为 `platform`（`classroom` / `lms`）、`course_id`、`name`、`updated_at`。智云还记录教师、学期、堂次 ID/名称/日期/上架状态和 `files`；LMS 记录附件列表及已知学期字段。所有文件路径相对课程目录，统一使用 `/`。

`.zju/downloads.json` 格式版本为 1，包含平台、课程 ID 和 `entries`。记录区分 `pending` 与 `complete`，保存路径、大小、资料类别、堂次/附件 ID、格式或分段信息。只有完整且通过大小和格式检查的资料才列入智云 `course.json`；不扫描目录猜测用户文件归属，不保存签名播放地址或凭据。

同步和单项资料命令都会更新课程元数据；API 暂时遗漏或失败不会删除已有堂次。目录名、课程名、堂次名变化不改变已登记路径。元数据更新失败保留旧文件，并报告失败。

课程写操作使用跨进程课程锁；下载状态在锁内重读、合并并原子替换，避免并发更新丢失。初次在资料库登记时有短时分配锁。锁文件会保留，`.zju/` 和工具锁文件是保留位置。**这些是单机文件锁，不是云盘跨设备分布式锁；不要在两台机器同时写同一课程。**

`--dry-run` 不下载媒体、不写课程资料或状态；在线查询仍可能更新用户级认证会话。完整录播中断后保留旁边的 `.part`、`.part.json`，下次继续核对源版本与分片 SHA-256 后续传。`--force` 重做已登记资料并丢弃录播分片进度，不自动重建旧 Markdown。

## PPT、转写、音频与录播

```bash
./zju.py classroom ppt --course 89253 --sub 2038852 --dedup --keep-images
./zju.py classroom transcript --course 89253 --format srt
./zju.py classroom transcript --course-dir /path/to/course --format md --force
./zju.py classroom recording --course 89253 -j 4
./zju.py classroom recording-audio --course 89253 -j 32
./zju.py classroom recording-audio --days 7
```

同步和单独转写均默认 Markdown，保留毫秒起止时间：`**[01:23:45.120 → 01:23:52.875]** 内容`。SRT 使用逗号分隔毫秒；TXT 仍可显式选择。已有且有效的登记文件增量跳过，改变格式或时间显示规则不会自动覆盖原文件。

PPT JSON 保存每个原始事件的 `created_sec`、从 0 开始的事件编号、从 1 开始的 PDF 页码、代表事件及去重关系。动画和翻回旧页的事件仍保留，空白事件可没有页码。事件时间是观测点，不是页面精确显示区间。`--keep-images` 保留原始截图；这些文件也纳入所有权记录。

音频仍通过异步 HTTP Multi-Range 获取压缩音轨并重建 M4A，不重新编码。已有完整本地录播时优先提取。视频仍按 Range 分片下载，支持跨运行续传；不支持 Range 时完整录播下载可退回单连接，**按需取帧不会这样回退**。

同步 `-j` 默认 4，单项音频默认 32；PPT、转写、视频分片和音频请求在各阶段遵循指定并发上限。`--max-size` 限制单个输出文件的 MiB，0 不限。LMS 默认 200 MiB、跳过音视频附件；`--videos` 开启附件音视频，它与智云录播不是同一来源。

## 完全离线的发现与上下文

```bash
./zju.py classroom courses --local --library /path/to/library --json
./zju.py lms courses --local --library /path/to/lms-library --json
./zju.py classroom subs --course-dir /path/to/course --json
./zju.py classroom subs --local --library /path/to/library --course 89253 --json
./zju.py classroom context --course-dir /path/to/course \
  --sub 2038852 --at 01:23:45.120 --window 20 --json
```

本地命令不登录、不调用 API、不初始化解码器、不修改资料。显式目录查询及 `--help` 不依赖全局配置是否可读。损坏元数据、重复身份或目录歧义明确报错；资料路径禁止绝对路径、`..` 和越界符号链接。

`context --json` 输出一个对象，包含 `schema_version`、`course`、`lesson`、`location`、`query`、`transcript`、`ppt`、`audio`、`video`、`recording_frame_hint`、`warnings`。文件引用相对 `location.course_dir`，拼接即可读取。

转写查询使用闭区间，默认前后 20 秒，支持毫秒、整秒及只有起点的 Markdown。异常长段、重叠和逆序时间会标记；TXT/SRT 提供引用但不解析上下文。PPT 返回窗口内事件及紧邻窗口的前后事件，无时间事件单列，不强行选唯一页面。

`missing`、`invalid`、`unavailable`、`unsafe_path`、`partial`、`no_matches` 等状态区分缺失、损坏与附近无内容。未记录的媒体时长为 `null`。转写与录播可能存在数秒或更大的偏差，多分段与整堂课的映射未知；候选时间窗口**未校准**，不会自动修正或累加分段时长。

## 按需录播取帧

```bash
./zju.py classroom recording-frame --course-dir /path/to/course \
  --sub 2038852 --at 01:23:45.120 --json
./zju.py classroom recording-frame --course 89253 --sub 2038852 \
  --at 5025.12 --output "/path/to/board.jpg"
./zju.py classroom recording-frame --course 89253 --sub 2038852 \
  --at 01:23:40 --at 01:23:45 --output-dir /path/to/frames
./zju.py classroom recording-frame --course 89253 --sub 2038852 \
  --frame "01:23:40=/path/to/before.jpg" --frame "01:23:45=/another/path/after.png"
./zju.py classroom recording-frame --course 89253 --sub 2038852 \
  --part 1 --at 01:23:45 --around 10 --interval 2 --json
```

本地来源使用课程完成记录；`--offline` 禁止远端回退，没有完整本地录播时明确失败。多段未指定 `--part` 会报错；`--at` 是所选 MP4 分段内相对时间，不能认定已经与转写或 PPT 校准。

PyAV wheel 可内含 FFmpeg 编解码库，无需系统 ffmpeg/ffprobe。按实际 PTS 选择显示帧，处理 B 帧、VFR 和非零起点；批量复用解码与网络数据。远端严格验证 206、Content-Range 和来源版本，默认网络预算 64 MiB（`--max-fetch-mb`），不会预下载完整视频。默认最多 100 帧（`--max-frames`）。

默认 JPEG，原分辨率、质量 95（`--quality`）；支持 PNG。未指定输出时创建独立系统临时目录，文件名含课程、堂次、分段及时间，CLI 退出后仍保留供 Agent 读取，由调用方或系统清理。不会复制到资料库或索引缓存。

有效图片缓存可复用；自动文件名冲突会安全另命名，显式完整路径冲突报错，`--force` 才覆盖已存在的截图。图片及 JSON sidecar 共同验证；写入失败尽量保留原有效结果。`--json` stdout 只有一个对象，含真实路径、请求/实际时间、源信息、请求数、网络读取量和耗时，诊断在 stderr。

## 共享 MP4 索引缓存

音频和视频取帧默认共享原始完整 `moov`，不缓存重封装后的 M4A 索引、媒体片段或完整 MP4。

- macOS/Linux：`${XDG_CACHE_HOME:-~/.cache}/zju-learning/`
- Windows：`%LOCALAPPDATA%\zju-learning\cache\`；无有效环境路径时回退到用户目录的 `AppData/Local`。
- `--index-cache-dir DIR` 覆盖位置，`--no-index-cache` 关闭。
- `--index-cache-mb 256` 指定容量上限，默认 256 MiB。

跨调用复用需要强 ETag 和相同资源身份；签名参数不入缓存。版本变化、损坏、不可写或锁冲突时安全回退网络。Last-Modified 只用于单次读取一致性，不作为跨调用强内容身份。索引缓存可以删除，不影响课程完成状态。

## 验证、错误与边界

```bash
uv run --python 3.12 --with requests --with httpx --with img2pdf --with pillow \
  --with keyring --with numpy --with 'av>=16,<18' python -m unittest discover -s tests
```

离线测试使用 Python wheel 生成媒体和本地 HTTP 服务；无需系统 FFmpeg 或学校账号。CI 配置包含 Windows、macOS、Linux，实际结果以相应运行记录为准。

退出码：0 成功；1 配置、认证、元数据或运行错误；2 参数错误或批量部分失败（以 stderr 诊断区分）；130 用户中断。没有资料与下载失败有不同提示，部分失败会继续处理其他项目。

这是发布前的新规范，不提供旧 CLI、旧配置或旧 Manifest 兼容和自动迁移。原资料须由用户在仓库外单独迁移并建立新的课程身份及完成记录；仅复制媒体文件不会自动登记为工具资料。无需因为容器格式变化而重下或转码。本程序不提供时间轴自动校准、全局目录注册、后台服务、Skill 或 MCP。

## 安全与致谢

Cookie 以受权限保护的 JSON 保存在用户状态目录；明文 HTTP 不发送 Cookie/Authorization。没有遥测。资料版权属于授课教师与学校，请遵守平台规则，不公开传播课程材料。

本项目由 [PeiPei233/zju-learning-assistant](https://github.com/PeiPei233/zju-learning-assistant) 的认证与平台 API 逻辑发展而来，保留 MIT 版权声明。录播目录接口参考 Cold_Ink 的[智云课堂批量下载](https://greasyfork.org/scripts/514465)；附件访问方式参考 [eWloYW8/ZJU-course-material-download](https://github.com/eWloYW8/ZJU-course-material-download)、[Kcalb35/Tronclass-pdf-downloaderforChrome](https://github.com/Kcalb35/Tronclass-pdf-downloaderforChrome)、[fish-can/TronClass-PDF-Downloader](https://github.com/fish-can/TronClass-PDF-Downloader)、[xzzd-pro/xzzd-pro](https://github.com/xzzd-pro/xzzd-pro)。

[MIT License](LICENSE)
