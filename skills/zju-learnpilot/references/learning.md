# 学习与证据定位

这里的 `$CLI` 表示从当前 Skill 解析出的 `scripts/zju.py` 绝对路径，`$COURSE` 是用户的实际课程目录。按宿主 shell 正确引用；使用工具的参数数组时无需构造 shell 字符串。

## 从知识点找到课堂

```bash
uv run --script "$CLI" classroom courses --local --json
uv run --script "$CLI" classroom courses --local --library "/path/to/library" --json
uv run --script "$CLI" classroom subs --course-dir "$COURSE" --json
```

默认资料库为 `~/ZJU-Courses/classroom`；用户可以在 `~/.config/zju-learning/config.json` 的 `libraries.classroom` 配置其他位置。没有结果不代表课程不存在，用户可能采用自己的目录布局。不要读取整个配置/认证目录来寻找课程，也不要输出配置中的凭据。

从 `course.json.lessons[].files.transcripts[]` 取得 Markdown 路径，拼接课程目录。使用 Agent 自身的本地文本搜索工具；有 `rg` 时可对**已验证的具体文件列表**做 `rg -n -F -e '能控性' -e '可控性' -- <文件…>`。没有 `rg` 可直接读取这些 UTF-8 文件检索，不必安装搜索系统。文本命中后读前后段落，排除引用、否定句和同名词误命中，再使用该段的原始时间做 context 查询。时间支持毫秒和旧整秒格式。

例如，找到“能控性矩阵”的讲解后，先读其定义与秩条件对应的原文，再结合课件说明各矩阵列的含义。后续演算或新例子标为补充；不要假定用户给定的知识点必然出现在这门课中。

## 时间窗口与资料状态

```bash
uv run --script "$CLI" classroom context --course-dir "$COURSE" \
  --sub 456 --at 00:01:23.120 --window 20 --json
```

结果主要字段：

- `course` / `lesson`：已确认的课程与堂次身份。
- `location.course_dir`：本次路径基准；所有 `path` / `file` / `pdf_path` 都相对它。
- `transcript.segments`：原始文本、`begin_sec`、`end_sec`、来源文件/行号及异常标记。
- `ppt.events`：窗口内及紧邻窗口前后的候选，含 `created_sec`、`event_index`、`pdf_page`、`representative_event_index`、`relationship`、`selection`。
- `ppt.untimed_events`：缺少可靠时间的事件，不能强行排入时间窗口。
- `audio.parts` / `video.parts`：本地分段文件，时长未登记时为 `null`。
- `warnings` / `recording_frame_hint`：时间轴不确定性和未校准的候选窗口，不是已经执行的取帧结果。

`missing` 表示无登记资料，`invalid` / `unavailable` 表示文件或元数据不可用，`no_matches` 表示附近无命中；`neighbors_only` 仍提供可读的相邻 PPT。`partial` 时逐项检查状态，不能把全部结果当作失败或全部当作有效。异常长段、零时长、重叠段保留原始时间。TXT/SRT 有文件引用，但 context 只解析 Markdown。

用 Agent 的 PDF 阅读/页面渲染能力查看候选 `pdf_page`（从 1 开始）。这些 PDF 通常由截图组成，文本提取为空不代表没有内容；少量渲染候选页后必须实际打开图片，生成 PNG 本身不算读过。引用 PDF 文件页码，区分页脚的课件编号及动画展开后重复出现的编号。若缺少视觉阅读能力，应说明限制，不把未读出的公式补成“老师说过”。页面可能多次出现，不能仅凭相同页码推断唯一时刻。

转写中的公式和术语可能误识。用实际页面核对，保留原始转写时间，把纠正明确表述为“根据课件重述”；不能把通用知识补全冒充为老师原话。

## 按需看板书或推导过程

```bash
# 已有完整本地录播；offline 禁止远端回退
uv run --script "$CLI" classroom recording-frame --course-dir "$COURSE" \
  --sub 456 --at 00:01:23.120 --offline --json
# 需要远端画面且已有访问权限时，去掉 --offline；默认最多读取 64 MiB
uv run --script "$CLI" classroom recording-frame --course-dir "$COURSE" \
  --sub 456 --at 00:01:23.120 --around 4 --interval 2 --json
```

查看返回的真实图片路径；以 `actual_time_sec` 记录实际画面时间，不复制请求时间。先少量取帧，用可观察内容判断附近画面是否对应原文，再按需扩大范围。默认独立临时目录在 CLI 退出后保留，读取后只清理自己本次生成且不再需要的文件。批量时间点复用读取和解码，优于逐帧调用。

多段时 CLI 要求 `--part`；它从 1 开始，`--at` 是该 MP4 分段内播放时间。**context 给出的音频分段列表不等于已知的课堂时间映射**。未能确认分段时先列出候选并解释缺口，不自动从第 1 段猜起。

保留默认共享索引缓存即可；它只缓存经版本校验的原始 MP4 索引。截图缓存位于输出旁边，不与资料库混用。不要通过下载完整视频、重编码、OCR 或 ASR 来绕过当前证据不足。

## 回答时的引用

可采用“课程名 / 堂次名，转写 00:01:20.120–00:01:32.875；PPT 第 3 页”的引用，优先链接原始转写和 PDF。文件链接使用已解析的绝对路径或宿主提供的有效引用，不直接使用 context 的相对路径或依赖当前工作目录的图片链接；临时渲染图仅作辅助。仅在实际阅读过的材料中引用对应内容。视频引用加“第 1 段，实际帧时间 00:01:24.040”；若该画面只是邻近候选，应明确指出。没有对应证据时可以给一般性知识解释，但要标明不是该课堂内容。
