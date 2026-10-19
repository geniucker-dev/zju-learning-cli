"""新课程格式的离线测试辅助；只写调用者提供的临时目录。"""

def initialize(zju, directory, cid=1, platform="classroom", name="课程"):
    directory.mkdir(parents=True, exist_ok=True)
    return zju.initialize_course(directory, platform, cid, name)


def register(zju, directory, path, sub, kind, extension, *, part=1, part_count=1):
    initialize(zju, directory, sub["course_id"])
    suffix = f" - {part:02d}" if part_count > 1 else ""
    key = zju.material_key(sub, kind, extension, suffix)
    details = zju.material_details(sub, kind, extension, suffix)
    if kind in ("录播", "音频"):
        details["part_count"] = part_count
    zju.DownloadState(directory).put(key, dict(path=path.relative_to(directory).as_posix(),
        status="complete", size=path.stat().st_size, **details))


def minimal_mp4():
    return b'\x00\x00\x00\x10ftypisom\x00\x00\x00\x00\x00\x00\x00\x08moov\x00\x00\x00\x09mdatx'
