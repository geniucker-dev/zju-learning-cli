# 课程与资料操作

`$CLI` 是本 Skill 的 `scripts/zju.py` 绝对路径。所有命令通过 `uv run --script "$CLI" …` 执行，下面省略该前缀。具体参数以附带 CLI 的 `<命令> --help` 为准。

| 意图 | 命令 | 影响 |
| --- | --- | --- |
| 发现本地课程 | `classroom courses --local --json` / `lms courses --local --json` | 离线只读，可加 `--library` |
| 查看本地堂次 | `classroom subs --course-dir DIR --json` | 离线只读 |
| 查看智云个人课程 | `classroom courses --json` | 联网认证；可加 `--has-tasks` |
| 找远端课程/堂次 | `classroom search 关键词`、`classroom subs --course ID --json` | 联网只读 |
| 查看 LMS 课程 | `lms courses --json`，往年加 `--all` | 联网只读；ID 与智云不同 |
| 查看待办、活动 | `lms todo --json`、`lms activities ID --json` | 联网只读 |
| 作业/活动详情 | `lms show ACTIVITY_ID`，读附件加 `--read` | 联网只读；附件会被获取 |
| 同步智云资料 | `classroom sync ID --course-dir DIR --recording-audio -j 32` | 下载 PPT、转写和音频，更新课程状态 |
| 同步 LMS 附件 | `lms sync ID --library DIR` | 本地下载；默认跳过音视频、大于 200 MiB 的附件 |
| 单项资料 | `classroom transcript/ppt/recording-audio --course-dir DIR --sub SID` | 本地增量下载，更新课程元数据 |
| 完整录播 | `classroom recording --course-dir DIR --sub SID` | 大文件下载，仅在用户确实需要完整录播时使用 |
| 追踪课程 | `classroom add/rm ID`、`classroom tracked` | 修改/读取本地追踪设置 |
| 发帖/回复、上传、交作业 | `lms forum post/reply …`、`lms upload …`、`lms submit …` | 远端写入，必须有用户明确授权 |

需要认证但未登录时，指导用户在自己的终端执行 `uv run --script "<CLI路径>" login`。不要替用户读取、打印或分享账号密码；不要自动 `login --reset`。普通查询不需要让用户重复登录。

## 选择目录与下载范围

`--library` 是课程目录的直接父目录，`--course-dir` 是实际课程目录，两者互斥。已初始化目录从 `course.json` 推断课程 ID；首次写入需要 ID。多课程同步用多个 ID 和 `--library`；只有用户明确要求全部课程时才用 `--all`。单项资料可按 `--days N`，但它与具体课程选择互斥。

对用户自建的混合资料目录，沿用其选择的位置。课程改名不改变身份或已登记路径。重复 ID、平台冲突或损坏状态应报告并保留现场，不自行删除或接管文件。

`--dry-run` 可预览写入计划，但仍访问 API、可能更新用户级认证会话；它不是离线开关。已有有效资料按课程级 `.zju/downloads.json` 跳过。不要为刷新元数据使用 `--force`，也不要把文件存在等同于完成登记。

音频优先由本地完整录播无损提取，否则只下载音轨；不用提前下载完整 MP4。同步 `-j` 默认 4，单项音频默认 32；遵循用户设置。`--recording` 会启用完整视频下载，与 `--recording-audio` 不同。

本地查询的 JSON stdout 可直接解析；诊断通常在 stderr。退出码 0 成功，1 配置/认证/运行错误，2 参数错误或批量部分失败，130 中断。批量部分失败时保留已成功资料并报告失败项目；明确区分未发布、已下架、网络失败与损坏文件。不要对未知失败无休止重试。

## 外部写入

先展示/明确具体目标和拟提交内容；用户已明确授权相同内容与目标时不重复索要确认。授权查询或学习不等于授权提交。`--yes` 仅在已有提交授权、参数和附件已核对时使用。无需写入时只调用列表和详情。不要用猜测的活动 ID、课程 ID 或附件路径执行写入。

## 数据与分发

安装/更新 Skill 只更新 Skill 目录，课程目录应位于其外。配置、凭据与缓存继续沿用 `zju-learning` 命名空间；不能因为品牌或安装路径变化而重置它们。不要把课程资料保存进 Skill 目录，安装器更新可能替换整个目录。

[LICENSE](../LICENSE) 保留 MIT 条款。CLI 源自 [8eoyw/zju-learning-cli](https://github.com/8eoyw/zju-learning-cli) 与 [PeiPei233/zju-learning-assistant](https://github.com/PeiPei233/zju-learning-assistant)，原版权声明随脚本与许可证分发。
