"""按需视频帧：有界 HTTP Range IO、PyAV 解码及可追溯的图片输出。

只在 recording-frame 使用时加载；不调用任何外部音视频程序。
"""
from __future__ import annotations

import argparse
from collections import OrderedDict
from contextlib import ExitStack
from fractions import Fraction
import errno
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import tempfile
import time
from urllib.parse import urlsplit

import httpx


class FrameError(RuntimeError):
    pass


def parse_time(value: str) -> float:
    """时间严格按秒或 H+:MM:SS[.sss] 解析，不拆分路径。"""
    if re.fullmatch(r"\d+:\d{2}:\d{2}(?:\.\d+)?", value):
        h, m, s = value.split(":")
        if int(m) >= 60 or float(s) >= 60:
            raise argparse.ArgumentTypeError(f"非法时间：{value}")
        result = int(h) * 3600 + int(m) * 60 + float(s)
    else:
        try:
            result = float(value)
        except ValueError:
            raise argparse.ArgumentTypeError(f"非法时间：{value}（使用 HH:MM:SS.sss 或秒数）") from None
    if not math.isfinite(result) or result < 0:
        raise argparse.ArgumentTypeError("时间必须是有限的非负秒数")
    return result


def frame_arg(value: str) -> tuple[float, str]:
    stamp, sep, path = value.partition("=")
    if not sep or not path:
        raise argparse.ArgumentTypeError('--frame 使用 "时间=完整图片路径"')
    return parse_time(stamp), path


def add_arguments(sp, fn):
    p = sp.add_parser("recording-frame", help="按需提取智云录播真实视频帧（本地优先，远端限量 Range 读取）",
                      description="时间相对所选 MP4 分段；默认保存在独立系统临时目录，退出后保留。无需系统 FFmpeg。")
    p.add_argument("--course", type=int, required=True, help="智云 course_id")
    p.add_argument("--sub", type=int, required=True, help="智云 sub_id（单堂课）")
    p.add_argument("--part", type=int, help="录播分段，从 1 开始；多段时必须指定")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--at", type=parse_time, action="append", help="目标时间 HH:MM:SS.sss 或秒数，可重复")
    g.add_argument("--frame", type=frame_arg, action="append", metavar="TIME=PATH", help="每帧时间与完整路径，可重复；不能与 --output / --output-dir 混用")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--output", help="单帧完整图片路径（含文件名）")
    g.add_argument("--output-dir", help="图片输出目录；省略时创建系统临时目录")
    p.add_argument("--around", type=float, help="每个 --at 前后窗口秒数；超出分段的部分裁剪")
    p.add_argument("--interval", type=float, help="窗口截图间隔秒数，须搭配 --around")
    p.add_argument("--quality", type=int, default=95, help="JPEG 质量 1–100（默认 95）")
    p.add_argument("--format", choices=("jpg", "png"), default="jpg", help="自动命名的图片格式；完整路径按扩展名选择")
    p.add_argument("--max-fetch-mb", type=float, default=64, help="远端实际响应体读取上限 MiB（默认 64，必须 > 0）")
    p.add_argument("--force", action="store_true", help="覆盖已有图片及元数据，忽略缓存")
    p.add_argument("--json", action="store_true", help="stdout 只输出一个 JSON 对象，诊断信息在 stderr")
    p.set_defaults(fn=fn)


def validate_args(a):
    if a.course < 1 or a.sub < 1 or (a.part is not None and a.part < 1):
        raise FrameError("--course、--sub、--part 必须 >= 1")
    if not 1 <= a.quality <= 100:
        raise FrameError("--quality 必须在 1–100 之间")
    if not math.isfinite(a.max_fetch_mb) or a.max_fetch_mb <= 0:
        raise FrameError("--max-fetch-mb 必须是有限正数")
    if a.output == "" or a.output_dir == "":
        raise FrameError("输出路径不能为空")
    if a.frame and (a.output or a.output_dir or a.around is not None or a.interval is not None):
        raise FrameError("--frame 不能与 --output、--output-dir、--around、--interval 混用")
    if a.output and (len(a.at or []) != 1 or a.around is not None):
        raise FrameError("--output 仅用于一个 --at，且不能搭配 --around")
    if (a.around is None) != (a.interval is None):
        raise FrameError("--around 与 --interval 必须一起使用")
    if a.around is not None:
        if not math.isfinite(a.around) or a.around < 0 or not math.isfinite(a.interval) or a.interval <= 0:
            raise FrameError("--around 必须是有限非负数，--interval 必须是有限正数")
        if len(a.at) * (2 * a.around / a.interval + 2) > 10000:
            raise FrameError("单次窗口不能超过 10000 帧，请缩小窗口或增加间隔")
    # 提前发现同一次调用的路径冲突，包括 Windows 上的大小写别名。
    paths = {}
    for sec, path in a.frame or []:
        key = os.path.normcase(str(Path(path).expanduser().resolve()))
        if key in paths:
            raise FrameError("同一次请求不能重复指定同一个完整输出路径")
        paths[key] = sec
    for path in [p for _, p in a.frame or []] + ([a.output] if a.output else []):
        image_format(Path(path))


class RangeReader(io.RawIOBase):
    """PyAV 自定义同步 IO；1MiB 相邻读取合并，最多缓存 8MiB。

    独立无凭据客户端，直连，不继承学校 Cookie、认证或环境代理。
    不跟随重定向，避免把签名查询或未来加入的凭据转发到其他主机。
    """
    block_size = 1024 * 1024
    cache_size = 8 * 1024 * 1024

    def __init__(self, url, max_bytes=64 * 2**20, *, client=None):
        super().__init__()
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
            raise FrameError("录播必须使用无内嵌凭据的 HTTP(S) URL")
        self.url = url
        self.client = client or httpx.Client(timeout=httpx.Timeout(30, connect=15), trust_env=False,
                                             follow_redirects=False, headers={"Accept-Encoding": "identity"})
        self.own_client = client is None
        self.max_bytes = int(max_bytes)
        self.requests = self.bytes_received = self.position = 0
        self.total = None
        self.validator = None
        self.cache = OrderedDict()
        self.last_error = None
        try:
            self._fetch(0, 15)
            # 不保存可过期的签名查询；身份只包含不可逆的资源路径摘要及服务端版本。
            resource = f"{parts.scheme}://{parts.netloc}{parts.path}"
            self.identity = {"resource_sha256": hashlib.sha256(resource.encode()).hexdigest(),
                             "size": self.total, "validator": list(self.validator)}
        except BaseException:
            self.close()
            raise

    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        return self.position

    def seek(self, offset, whence=os.SEEK_SET):
        if whence == os.SEEK_SET:
            pos = offset
        elif whence == os.SEEK_CUR:
            pos = self.position + offset
        elif whence == os.SEEK_END:
            pos = self.total + offset
        else:
            raise ValueError("不支持的 seek 模式")
        if pos < 0:
            raise ValueError("不能 seek 到负位置")
        self.position = pos
        return pos

    def _fetch(self, start, end):
        if self.bytes_received + end - start + 1 > self.max_bytes:
            raise FrameError(f"达到远端读取上限 {self.max_bytes / 2**20:g} MiB；可调整 --max-fetch-mb")
        headers = {"Range": f"bytes={start}-{end}", "Accept-Encoding": "identity"}
        if self.validator:
            headers["If-Range"] = self.validator[1]
        for attempt in range(3):
            try:
                # 失败的响应也消耗预算；每次重试前重新检查。
                if self.bytes_received + end - start + 1 > self.max_bytes:
                    raise FrameError("重试会超过 --max-fetch-mb 读取上限")
                self.requests += 1
                with self.client.stream("GET", self.url, headers=headers, follow_redirects=False) as r:
                    if r.status_code != 206:
                        if r.status_code == 200:
                            raise FrameError("服务器返回 HTTP 200，拒绝下载整个视频（不支持 Range 或视频已变化）")
                        if r.status_code in (401, 403):
                            raise FrameError(f"录播访问被拒绝 HTTP {r.status_code}，请重新登录或刷新录播地址")
                        raise FrameError(f"录播 Range 请求失败 HTTP {r.status_code}（不跟随重定向）")
                    match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", r.headers.get("Content-Range", ""))
                    if not match:
                        raise FrameError("无效的 Content-Range")
                    lo, hi, total = map(int, match.groups())
                    expected_end = min(end, total - 1)
                    if lo != start or hi != expected_end or hi < lo or (self.total is not None and total != self.total):
                        raise FrameError("Content-Range 与请求不符，或远端视频大小已变化")
                    etag, modified = r.headers.get("ETag"), r.headers.get("Last-Modified")
                    validator = (("ETag", etag) if etag and not etag.startswith("W/") else
                                 ("Last-Modified", modified) if modified else None)
                    if not validator:
                        raise FrameError("服务器未提供强 ETag 或 Last-Modified，无法确认视频版本一致性")
                    if self.validator is not None and validator != self.validator:
                        raise FrameError("远端视频版本已变化，停止读取；请重新运行")
                    if r.headers.get("Content-Encoding", "identity").lower() != "identity":
                        raise FrameError("Range 响应使用了不支持的内容编码")
                    length = hi - lo + 1
                    # 固定消息长度让 HTTP 传输层也约束响应体，拒绝无界 chunked
                    # 回应，不能仅相信 Content-Range 后任由服务器流式发送。
                    if r.headers.get("Content-Length") != str(length):
                        raise FrameError("Range 响应缺少 Content-Length 或长度不符，无法保证读取上限")
                    data = bytearray()
                    # 已拒绝压缩编码，iter_bytes 与线上的响应体字节一致；同时
                    # 支持预先构造的测试响应，不强制二次消费已加载的 raw stream。
                    for chunk in r.iter_bytes():
                        self.bytes_received += len(chunk)
                        if self.bytes_received > self.max_bytes:
                            raise FrameError("实际网络读取超过 --max-fetch-mb 上限")
                        if len(data) + len(chunk) > length:
                            raise FrameError("Range 响应长度超过请求范围")
                        data.extend(chunk)
                    if len(data) != length:
                        raise httpx.ReadError("incomplete range")
                    self.total, self.validator = total, validator
                    return bytes(data)
            except (httpx.TransportError, httpx.TimeoutException):
                if attempt == 2:
                    raise FrameError("录播 Range 响应不完整、连接中断或超时（已尝试 3 次）") from None
        raise AssertionError("unreachable")

    def read(self, size=-1):
        if self.closed:
            raise ValueError("读取已关闭的 IO")
        if self.last_error:
            return b""
        # PyAV 按固定缓冲块调用；拒绝调用方请求整段流以避免意外全文件读入。
        if size < 0:
            raise FrameError("远端 IO 不允许无界 read()")
        size = min(size, max(0, self.total - self.position))
        if size > self.cache_size:
            raise FrameError("远端单次读取过大")
        result = bytearray()
        try:
            while len(result) < size:
                base = self.position // self.block_size * self.block_size
                if base not in self.cache:
                    data = self._fetch(base, min(base + self.block_size, self.total) - 1)
                    self.cache[base] = data
                    while len(self.cache) > self.cache_size // self.block_size:
                        self.cache.popitem(last=False)
                data = self.cache[base]
                self.cache.move_to_end(base)
                offset = self.position - base
                take = min(size - len(result), len(data) - offset)
                result.extend(data[offset:offset + take])
                self.position += take
            return bytes(result)
        except (FrameError, KeyboardInterrupt) as e:
            self.last_error = e
            # libavformat 可能忽略回调异常并重复读取；EOF 后在 Python 边界重抛，
            # 防止其继续请求、重复打印底层 traceback 或用不完整结果成功返回。
            return b""

    def close(self):
        if not self.closed:
            if getattr(self, "own_client", False):
                self.client.close()
            if hasattr(self, "cache"):
                self.cache.clear()
        super().close()


def select_part(urls, part):
    if not urls:
        raise FrameError("该堂次暂无回放")
    if part is None and len(urls) != 1:
        raise FrameError(f"该堂次有 {len(urls)} 个录播分段，请指定 --part 1…{len(urls)}")
    part = part or 1
    if not 1 <= part <= len(urls):
        raise FrameError(f"--part 超出范围（共 {len(urls)} 段）")
    return part, urls[part - 1]


def image_format(path):
    suffix = path.suffix.lower()
    if suffix not in (".jpg", ".jpeg", ".png"):
        raise FrameError("图片完整路径必须以 .jpg、.jpeg 或 .png 结尾")
    return "PNG" if suffix == ".png" else "JPEG"


def stamp_name(sec):
    ms = round(sec * 1000)
    seconds, ms = divmod(ms, 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02d}-{minutes:02d}-{seconds:02d}-{ms:03d}"


def file_hash(path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(256 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def sidecar(path):
    return path.with_name(path.name + ".json")


def cached(path, key):
    try:
        m = json.loads(sidecar(path).read_text(encoding="utf-8"))
        required = {"course_id", "sub_id", "part", "requested_time_sec", "actual_time_sec", "timebase",
                    "source_type", "codec", "width", "height", "output_path", "cache_hit"}
        if (m["cache_key"] == key and required <= m["frame"].keys()
                and path.is_file() and m["image_sha256"] == file_hash(path)):
            return dict(m["frame"], path=str(path), output_path=str(path), cache_hit=True)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        pass
    return None


def publish_without_links(tmp, path, overwrite=False):
    """不支持硬链接的文件系统（如部分云盘）使用系统锁保护检查和原子替换。

    小锁文件保留，避免删除锁文件导致并行进程各自持有不同 inode 的锁。
    进程结束时系统自动释放锁；没有常驻进程或全局缓存。
    """
    fd = os.open(path.with_name(f".{path.name}.frame.lock"), os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(fd, "r+b") as lock:
        try:
            if os.name == "nt":
                import msvcrt
                lock.write(b"\0")
                lock.flush()
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise FrameError(f"另一进程正在写入图片：{path}") from None
        if path.exists() and not overwrite:
            raise FrameError(f"图片已存在，未覆盖：{path}（使用 --force）")
        os.replace(tmp, path)


def atomic_image(frame, path, quality, overwrite):
    """同目录临时图片，完整编码后才发布；未授权覆盖时原子 link 防竞态覆盖。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".zju-frame-", suffix=path.suffix, dir=path.parent)
    tmp = Path(name)
    try:
        with os.fdopen(fd, "wb") as f:
            opts = {"quality": quality, "subsampling": 0} if image_format(path) == "JPEG" else {}
            frame.to_image().save(f, format=image_format(path), **opts)
            f.flush()
            os.fsync(f.fileno())
        digest = file_hash(tmp)
        if overwrite:
            publish_without_links(tmp, path, overwrite=True)
        else:
            try:
                os.link(tmp, path)
            except FileExistsError:
                raise FrameError(f"图片已存在，未覆盖：{path}（使用 --force）") from None
            except OSError as e:
                if e.errno not in (errno.EPERM, errno.ENOSYS, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EXDEV):
                    raise
                publish_without_links(tmp, path)
        return digest
    finally:
        tmp.unlink(missing_ok=True)


def atomic_json(path, data):
    fd, name = tempfile.mkstemp(prefix=".zju-frame-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, allow_nan=False)
            f.write("\n")
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


class Decoder:
    """单个容器及解码上下文，按 PTS 选 target 前最后一帧，保留后一帧供批量请求。"""
    def __init__(self, container):
        self.container = container
        if not container.streams.video:
            raise FrameError("MP4 不包含视频流")
        self.stream = container.streams.video[0]
        self.stream.thread_type = "AUTO"
        self.stream.codec_context.thread_count = 2
        self.timebase = self.stream.time_base
        self.origin = (self.stream.start_time or 0) * self.timebase
        if self.stream.duration is None:
            raise FrameError("MP4 缺少视频时长索引，无法安全验证请求时间")
        self.duration = float(self.stream.duration * self.timebase)
        self.iterator = None
        self.previous = self.next_frame = None
        self.last_target = None
        self.decoded_frames = self.seeks = 0

    def timestamp(self, frame):
        if frame.pts is None:
            raise FrameError("解码帧缺少 PTS，不能确定真实显示时间")
        return frame.pts * frame.time_base - self.origin

    def advance(self):
        frame = next(self.iterator, None)
        if frame is not None:
            self.decoded_frames += 1
        return frame

    def at(self, target):
        point = Fraction(str(target))
        if self.iterator is None or target < self.last_target or target - self.last_target > 10:
            offset = math.floor((point + self.origin) / self.timebase)
            self.container.seek(offset, stream=self.stream, backward=True, any_frame=False)
            self.seeks += 1
            self.iterator = iter(self.container.decode(self.stream))
            self.previous = None
            self.next_frame = self.advance()
        # 解码器按显示顺序输出（包含 B 帧重排），不使用 DTS 或平均帧率。
        while self.next_frame is not None and self.timestamp(self.next_frame) <= point:
            self.previous = self.next_frame
            self.next_frame = self.advance()
        self.last_target = target
        if self.previous is None:
            raise FrameError(f"时间 {target:g} 秒没有可显示的视频帧")
        return self.previous, float(self.timestamp(self.previous))


def extract(a, source, part, *, client=None):
    """source 是完整本地 Path 或远端 URL；返回稳定的请求级 JSON 对象。"""
    validate_args(a)
    started = time.monotonic()
    reader = None
    try:
        # 运行其他命令不初始化原生视频库。
        import av
        with ExitStack() as stack:
            if isinstance(source, Path):
                source = source.resolve()
                before = source.stat()
                if source.suffix.lower() != ".mp4" or before.st_size == 0:
                    raise FrameError("本地来源必须是完整非空 MP4，不能是 .part")
                handle = stack.enter_context(source.open("rb"))
                source_type = "local"
                identity = {"path_sha256": hashlib.sha256(str(source).encode()).hexdigest(),
                            "size": before.st_size, "mtime_ns": before.st_mtime_ns,
                            "ctime_ns": before.st_ctime_ns, "inode": before.st_ino}
            else:
                reader = stack.enter_context(RangeReader(source, int(a.max_fetch_mb * 2**20), client=client))
                handle, source_type, identity = reader, "remote", reader.identity
            container = stack.enter_context(av.open(handle, mode="r", format="mp4"))
            if reader and reader.last_error:
                raise reader.last_error
            decoder = Decoder(container)
            times = a.frame or [(t, a.output) for t in a.at]
            for sec, _ in times:
                if sec >= decoder.duration:
                    raise FrameError(f"请求时间 {sec:g} 秒超出视频范围 [0, {decoder.duration:g}) 秒")
            if a.around is not None:
                expanded = set()
                for center, _ in times:
                    count = math.floor(2 * a.around / a.interval + 1e-9)
                    for i in range(count + 1):
                        sec = round(center - a.around + i * a.interval, 9)
                        if 0 <= sec < decoder.duration:
                            expanded.add(sec)
                    expanded.add(center)
                times = [(t, None) for t in sorted(expanded)]
            temporary = not (a.frame or a.output or a.output_dir)
            directory = (Path(tempfile.mkdtemp(prefix="zju-frames-")).resolve() if temporary else
                         Path(a.output_dir).expanduser().resolve() if a.output_dir else None)
            plans, occupied = [], set()
            for sec, explicit in times:
                path = (Path(explicit).expanduser().resolve() if explicit else
                        directory / f"{a.course}_{a.sub}_p{part:02d}_{stamp_name(sec)}.{a.format}")
                key = {"version": 1, "source": identity, "course_id": a.course, "sub_id": a.sub,
                       "part": part, "requested_time_sec": sec, "format": image_format(path), "quality": a.quality}
                hit = None if a.force else cached(path, key)
                exists = path.exists() or sidecar(path).exists()
                if explicit and exists and not hit and not a.force:
                    raise FrameError(f"已有图片无法确认缓存有效，未覆盖：{path}（使用 --force）")
                if not explicit:
                    base, number = path, 1
                    while ((path.exists() or sidecar(path).exists()) and not hit and not a.force) or path in occupied:
                        path = base.with_name(f"{base.stem}_{number}{base.suffix}")
                        number += 1
                        hit = None if a.force else cached(path, key)
                occupied.add(path)
                plans.append((sec, path, key, hit))
            results, written = {}, []
            # 排序解码，返回时恢复请求顺序；相同时间复用解码结果。
            frame = None
            last_sec = None
            for index, (sec, path, key, hit) in sorted(enumerate(plans), key=lambda item: item[1][0]):
                if hit:
                    results[index] = hit
                    continue
                if last_sec != sec:
                    frame, actual = decoder.at(sec)
                    if reader and reader.last_error:
                        raise reader.last_error
                    last_sec = sec
                metadata = {"course_id": a.course, "sub_id": a.sub, "part": part,
                            "requested_time_sec": sec, "actual_time_sec": actual,
                            "timebase": str(frame.time_base), "source_type": source_type,
                            "codec": decoder.stream.codec_context.name,
                            "width": frame.width, "height": frame.height,
                            "path": str(path), "output_path": str(path), "cache_hit": False}
                digest = atomic_image(frame, path, a.quality, a.force)
                # 校验码来自发布前的临时图片；并行 --force 也不会把另一张图的哈希
                # 错配到本帧时间，最坏情况下 sidecar 失效，下次明确拒绝或重新生成。
                record = {"cache_key": key, "image_sha256": digest, "frame": metadata}
                atomic_json(sidecar(path), record)
                written.append((path, record))
                results[index] = metadata
            after = source.stat() if source_type == "local" else None
            if after and (after.st_size, after.st_mtime_ns, after.st_ctime_ns, after.st_ino) != (
                    before.st_size, before.st_mtime_ns, before.st_ctime_ns, before.st_ino):
                raise FrameError("本地视频在提取期间发生变化，输出不可复用；请重新运行")
            frames = [results[i] for i in range(len(plans))]
            result = {"course_id": a.course, "sub_id": a.sub, "part": part, "temporary": temporary,
                    "output_dir": str(directory) if directory else None, "frames": frames,
                    "frame_count": len(frames), "source_type": source_type,
                    "source_size": identity["size"], "duration_sec": decoder.duration,
                    "network": {"requests": reader.requests if reader else 0,
                                "bytes_received": reader.bytes_received if reader else 0},
                    "decode": {"seeks": decoder.seeks, "frames": decoder.decoded_frames},
                    "elapsed_sec": time.monotonic() - started}
            request = {key: value for key, value in result.items() if key != "frames"}
            for path, record in written:
                atomic_json(sidecar(path), dict(record, request=request))
            result["elapsed_sec"] = time.monotonic() - started
            return result
    except (FrameError, KeyboardInterrupt):
        raise
    except Exception as e:
        if reader and reader.last_error:
            raise reader.last_error from None
        # 原始 HTTP/FFmpeg 异常可能携带签名 URL；只返回异常类型。
        raise FrameError(f"视频帧提取失败（{type(e).__name__}）；请检查视频、输出权限和解码支持") from None
