#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["requests", "httpx", "img2pdf", "pillow", "keyring", "numpy", "av>=16,<18"]
# ///
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 8eoyw
# Portions ported from PeiPei233/zju-learning-assistant, Copyright (c) 2023 PeiPei233 (MIT).
"""学在浙大 / 智云课堂单文件 CLI。

  ./zju.py login
  ./zju.py lms sync 123 --library ~/ZJU-Courses/lms
  ./zju.py classroom sync 89253 --recording-audio
  ./zju.py classroom context --course-dir /path/to/course --sub 2038852 --at 01:23:45 --json

课程以 (platform, course_id) 识别，课程目录携带独立元数据与下载状态。
媒体算法使用 Python wheel，不依赖系统 FFmpeg。
"""
from __future__ import annotations

import argparse
import io
from collections import OrderedDict
from fractions import Fraction
from decimal import Decimal, ROUND_HALF_UP
import math
import array
import asyncio
import datetime as dt
import html
import hashlib
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed, wait
from contextlib import AsyncExitStack, ExitStack, contextmanager, nullcontext
from pathlib import Path, PureWindowsPath
from urllib.parse import unquote, urljoin, urlparse, urlsplit

import ssl
import struct

import requests
import httpx
from requests.adapters import HTTPAdapter

KEYCHAIN_SERVICE = "zju-learning"
STATE_DIR = Path(os.environ.get("ZJU_STATE_DIR") or (Path.home() / ".config" / "zju-learning"))
CONFIG_FILE = STATE_DIR / "config.json"
COOKIE_FILE = STATE_DIR / "cookies.json"
# 这两台只支持 1024-bit DHE / 静态 RSA，OpenSSL 3 默认拒绝；降级只套用在它们身上
LEGACY_TLS_HOSTS = ("courses.zju.edu.cn", "identity.zju.edu.cn")
CST = dt.timezone(dt.timedelta(hours=8))  # 学校 API 没带时区时视为北京时间
LMS = "https://courses.zju.edu.cn"
DEFAULT_LIBRARIES = {p: Path.home() / "ZJU-Courses" / p for p in ("classroom", "lms")}
UA = "Mozilla/5.0 (X11; Linux x86_64; rv:88.0) Gecko/20100101 Firefox/88.0"
MEDIA_EXT = {".mp4", ".mov", ".avi", ".mkv", ".flv", ".m4v", ".wmv", ".webm", ".mp3", ".m4a", ".wav"}
TIMEOUT = (6, 60)  # connect, read — 排程时别卡在单一 hop 上

COURSE_FIELDS = (
    "id,name,course_code,department(id,name),start_date,end_date,is_started,is_closed,"
    "academic_year_id,semester_id,credit,display_name,instructors(id,name)"
)


class ZjuError(RuntimeError):
    pass


def log(*a):
    print(*a, file=sys.stderr, flush=True)


WIN_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def safe_name(s: str) -> str:
    s = re.sub(r'[/\\:*?"<>|\x00-\x1f]', "_", str(s)).strip(" .")
    s = s[:150].rstrip(" .") or "_"
    if s.split(".")[0].upper() in WIN_RESERVED:  # Windows 保留装置名
        s = "_" + s
    return s


def material_key(sub, kind, extension, part=""):
    prefix = {"转录": "transcript", "智云PPT": "ppt", "录播": "video", "音频": "audio"}[kind]
    variant = int(part.strip(" -") or 1) if prefix in ("audio", "video") else extension
    return f"{prefix}:{sub['course_id']}:{sub['sub_id']}:{variant}"


def classroom_material_path(directory: Path, sub: dict, kind: str, extension: str, part: str = "") -> Path:
    """课程内已登记路径优先；显示名称变化不改变文件身份。"""
    record = DownloadState(directory).get(material_key(sub, kind, extension, part))
    if record:
        return context_path(directory, record["path"])
    stem = f"{safe_name(sub['sub_name'])} ({sub['sub_id']})"
    return directory / kind / f"{stem}{part}.{extension}"


# ---------------- config / credentials ----------------

def state_dir() -> Path:
    STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(STATE_DIR, 0o700)
    return STATE_DIR


def load_config() -> dict:
    if not CONFIG_FILE.exists():
        return {}
    try:
        value = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("配置必须是 JSON 对象")
        return value
    except (OSError, ValueError) as e:
        raise ZjuError(f"{CONFIG_FILE} 格式错误：{e}")


def save_config(cfg: dict):
    state_dir()
    atomic_text(CONFIG_FILE, json.dumps(cfg, ensure_ascii=False, indent=2) + "\n")


def keychain_get(user: str) -> str | None:
    if sys.platform != "darwin":
        try:
            import keyring
            return keyring.get_password(KEYCHAIN_SERVICE, user)
        except Exception:
            return None
    r = subprocess.run(
        ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", user, "-w"],
        capture_output=True, text=True,
    )
    return r.stdout.rstrip("\n") if r.returncode == 0 else None


def keychain_set_interactive(user: str):
    if sys.platform != "darwin":  # Windows 凭证管理员 / Linux Secret Service
        import getpass
        import keyring
        try:
            keyring.set_password(KEYCHAIN_SERVICE, user, getpass.getpass("密码："))
        except Exception as e:
            raise ZjuError(f"系统凭据库不可用（{e}）；改用环境变数 ZJU_USER / ZJU_PASS")
        return
    # macOS 走系统 security CLI（排程读取不会跳授权视窗）；-w 放最后 = security 自己在 tty 上问密码，密码不进 argv / shell history
    r = subprocess.run(
        ["security", "add-generic-password", "-U", "-s", KEYCHAIN_SERVICE, "-a", user, "-w"]
    )
    if r.returncode != 0:
        raise ZjuError("写入 Keychain 失败")


def get_credentials() -> tuple[str, str]:
    user = os.environ.get("ZJU_USER") or load_config().get("username")
    if not user:
        raise ZjuError("尚未设置帐号，先跑：zju.py login")
    pwd = os.environ.get("ZJU_PASS") or keychain_get(user)
    if not pwd:
        raise ZjuError("Keychain 找不到密码，先跑：zju.py login")
    return user, pwd


# ---------------- client ----------------

class LegacyTLS(HTTPAdapter):
    """只挂在 LEGACY_TLS_HOSTS：它们只给 1024-bit DHE，OpenSSL 3 报 DH_KEY_TOO_SMALL。"""

    def init_poolmanager(self, *a, **kw):
        ctx = ssl.create_default_context()
        ctx.set_ciphers("DEFAULT:@SECLEVEL=1")
        kw["ssl_context"] = ctx
        return super().init_poolmanager(*a, **kw)

    def proxy_manager_for(self, *a, **kw):
        ctx = ssl.create_default_context()
        ctx.set_ciphers("DEFAULT:@SECLEVEL=1")
        kw["ssl_context"] = ctx
        return super().proxy_manager_for(*a, **kw)


class NoCookieHTTP(HTTPAdapter):
    """明文 http:// 一律不带 cookie / Authorization。
    .zju.edu.cn 的 SSO cookie（iPlanetDirectoryPro 等）没设 Secure，浏览器和 requests
    都会照送给 http 网址 —— 智云 PPT 图片就是 http，等于把登录凭证明文送出。"""

    def send(self, request, **kw):
        request.headers.pop("Cookie", None)
        request.headers.pop("Authorization", None)
        return super().send(request, **kw)


class DownloadError(ZjuError):
    def __init__(self, msg: str, codes: list[int]):
        super().__init__(msg)
        self.codes = codes


class TooBig(ZjuError):
    pass


def secure_url(u: str) -> str:
    """学校主机的 http 网址升级成 https（实测 video.cmc 等都支持）。"""
    p = urlparse(u)
    if p.scheme == "http" and (p.hostname or "").endswith(".zju.edu.cn"):
        return "https" + u[4:]
    return u


def refer_params(activity: dict | None) -> dict | None:
    """组 /uploads/{id}/blob 的 reference 参数，与官方网页前端下载钮送出的相同：
    classroom→classroom_activity、exam 不带、其余→learning_activity；
    服务器只认 snake_case 参数名。"""
    if not activity or not activity.get("id"):
        return None
    t = activity.get("type")
    if t == "exam":
        return None
    return {"refer_id": activity["id"],
            "refer_type": "classroom_activity" if t == "classroom" else "learning_activity"}


class Zju:
    def __init__(self, *, classroom_first=False):
        self.jar = requests.cookies.RequestsCookieJar()  # 各执行绪 session 共用（CookieJar 自带锁）
        self._tl = threading.local()
        self.logged_in = False
        self.classroom_first = classroom_first
        self._frame_reauthenticated = False
        self._load_cookies()

    def _load_cookies(self):
        if not COOKIE_FILE.exists():
            return
        try:
            for d in json.loads(COOKIE_FILE.read_text()):
                self.jar.set_cookie(requests.cookies.create_cookie(**d))
        except (ValueError, TypeError, KeyError):
            COOKIE_FILE.unlink(missing_ok=True)  # 坏了就重登

    @property
    def s(self) -> requests.Session:
        """每个执行绪一个 session：连接池不互抢，trust_env 切换也不会互相干扰。"""
        if not hasattr(self._tl, "s"):
            s = requests.Session()
            s.mount("https://", HTTPAdapter(pool_connections=8, pool_maxsize=8))
            for h in LEGACY_TLS_HOSTS:
                s.mount(f"https://{h}", LegacyTLS(pool_connections=8, pool_maxsize=8))
            s.mount("http://", NoCookieHTTP())
            s.headers["User-Agent"] = UA
            s.cookies = self.jar
            self._tl.s = s
        return self._tl.s

    def req(self, method: str, url: str, retry: bool | None = None, **kw) -> requests.Response:
        """默认直连，连不上才退环境 proxy。
        重试只给幂等请求（GET 或明确 retry=True）；非幂等只在「确定没送出」（连接逾时／proxy 错）时重试，
        免得登录 POST 被重送、触发 CAS 验证码。TLS 错误不重试：跟断线要分得出来。"""
        endpoint = urlsplit(url)
        frame_api = (getattr(self, "classroom_first", False) and method == "GET" and endpoint.scheme == "https"
                     and (endpoint.hostname, endpoint.path) in {
            ("classroom.zju.edu.cn", "/userapi/v1/infosimple"),
            ("classroom.zju.edu.cn", "/courseapi/v2/course/catalogue"),
            ("yjapi.cmc.zju.edu.cn", "/courseapi/v3/multi-search/get-course-detail"),
        })
        if frame_api:
            kw["allow_redirects"] = False
        kw.setdefault("timeout", TIMEOUT)
        idempotent = method in ("GET", "HEAD") if retry is None else retry
        last = None
        for attempt in range(4):
            self.s.trust_env = attempt % 2 == 1
            try:
                r = self.s.request(method, url, **kw)
            except requests.exceptions.SSLError as e:
                raise ZjuError(f"TLS 验证失败（网路可能被拦截，或学校凭证有问题）：{url}\n{e}")
            except (requests.exceptions.ConnectTimeout, requests.exceptions.ProxyError) as e:
                last = e
            except (requests.ConnectionError, requests.Timeout) as e:
                if not idempotent:
                    raise ZjuError(f"连接中断（请求可能已送出，不自动重送）：{url}\n{e}")
                last = e
            else:
                if r.status_code in (429, 503) and attempt < 3:  # 被限流：照 Retry-After 退让
                    wait = r.headers.get("Retry-After", "")
                    r.close()
                    time.sleep(min(int(wait), 60) if wait.isdigit() else 2 * 2 ** attempt)
                    continue
                if frame_api:
                    if self._frame_auth_expired(r):
                        r.close()
                        self._frame_login()
                        kw["headers"] = {**kw.get("headers", {}), **self.bearer()}
                        return self.req(method, url, retry=retry, **kw)
                    if not r.ok or r.is_redirect:
                        r.close()
                        raise ZjuError(f"智云接口请求失败 HTTP {r.status_code}（不跟随重定向）")
                return r
            time.sleep(0.3 * 2 ** attempt)
        raise ZjuError(f"连接失败：{url}\n{last}")

    def get(self, url, **kw):
        return self.req("GET", url, **kw)

    def post(self, url, **kw):
        return self.req("POST", url, **kw)

    def save_cookies(self):
        """JSON 不是 pickle：快取档被别人改了也只是读到坏 cookie，不会执行程式码。"""
        state_dir()
        data = [{"name": c.name, "value": c.value, "domain": c.domain, "path": c.path,
                 "secure": c.secure, "expires": c.expires, "rest": c._rest} for c in self.jar]
        tmp = COOKIE_FILE.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        os.replace(tmp, COOKIE_FILE)
        os.chmod(COOKIE_FILE, 0o600)

    # ---- auth ----

    def login(self, user: str, pwd: str):
        self.s.cookies.clear()
        text = self.get("https://zjuam.zju.edu.cn/cas/login").text
        m = re.search(r'name="execution" value="(.*?)"', text)
        if not m:
            raise ZjuError("CAS 页面找不到 execution 栏位（登录页改版？）")
        key = self.get("https://zjuam.zju.edu.cn/cas/v2/getPubKey").json()
        n, e = int(key["modulus"], 16), int(key["exponent"], 16)
        enc = format(pow(int.from_bytes(pwd.encode(), "big"), e, n), "x")
        if len(enc) % 2:
            enc = "0" + enc
        r = self.post("https://zjuam.zju.edu.cn/cas/login", data={
            "username": user, "password": enc, "execution": m.group(1),
            "_eventId": "submit", "authcode": "",
        })
        if "统一身份认证平台" in r.text:
            raise ZjuError("登录失败：学号或密码错误（或需要验证码，先在浏览器登录一次）")
        # 让各子系统吃到 SSO
        self.get("https://courses.zju.edu.cn/user/courses")
        try:
            self.get("https://tgmedia.cmc.zju.edu.cn/index.php?r=auth/login&auType=cmc&tenant_code=112"
                     "&forward=https%3A%2F%2Fclassroom.zju.edu.cn%2F")
        except ZjuError as e:
            log(f"警告：智云 SSO 连不上（只影响 classroom/ppt/transcript）：{str(e).splitlines()[0]}")
        self.logged_in = True
        self.save_cookies()

    def courses_alive(self) -> bool:
        try:
            r = self.get("https://courses.zju.edu.cn/api/todos?no-intercept=true", allow_redirects=False)
            return r.status_code == 200 and "todo_list" in r.json()
        except (ValueError, ZjuError):
            return False

    def ensure(self, need_classroom: bool = False):
        if self.logged_in:
            return
        if getattr(self, "classroom_first", False) and need_classroom:
            # 使用业务请求验证现有智云 token；不先访问学在浙大待办接口。
            if not self._token(silent=True):
                self._frame_login()
            return
        if self.courses_alive() and (not need_classroom or self._token(silent=True)):
            self.logged_in = True
            return
        self.login(*get_credentials())

    def _frame_login(self):
        if self._frame_reauthenticated:
            raise ZjuError("智云认证失败或无权访问该接口（重新登录后仍被拒绝）")
        self._frame_reauthenticated = True
        self.logged_in = False
        self.login(*get_credentials())

    @staticmethod
    def _frame_auth_expired(r):
        if r.status_code in (401, 403):
            return True
        if r.is_redirect:
            target = urlsplit(urljoin(r.url, r.headers.get("Location", "")))
            return (target.hostname in {"zjuam.zju.edu.cn", "identity.zju.edu.cn",
                                        "classroom.zju.edu.cn", "tgmedia.cmc.zju.edu.cn"}
                    and any(word in target.path.lower() for word in ("login", "auth")))
        try:
            data = r.json()
        except ValueError:
            return "统一身份认证" in r.text and 'name="execution"' in r.text
        if not isinstance(data, dict):
            return False
        result = data.get("result")
        return (data.get("code") in (401, 403, "401", "403") or
                (isinstance(result, dict) and (result.get("status") in (401, 403, "401", "403")
                                              or result.get("name") == "Unauthorized")))

    def _token(self, silent=False) -> str | None:
        for c in self.s.cookies:
            if (c.domain or "").lstrip(".") not in ("classroom.zju.edu.cn", "zju.edu.cn"):
                continue
            m = re.search(r'\{i:\d+;s:\d+:"_token";i:\d+;s:\d+:"(.+?)";\}', unquote(c.value or ""))
            if m:
                return m.group(1)
        if silent:
            return None
        raise ZjuError("智云课堂 token 解析失败：classroom cookie 格式可能改了，检查 _token 正则")

    def bearer(self) -> dict:
        return {"Authorization": f"Bearer {self._token()}"}

    def json(self, r: requests.Response, what: str):
        try:
            return r.json()
        except ValueError:
            raise ZjuError(f"{what}：回应不是 JSON（HTTP {r.status_code}），session 可能失效，重跑即可")

    # ---- 学在浙大 ----

    def courses(self) -> list[dict]:
        self.ensure()
        out, page = [], 1
        while True:
            j = self.json(self.post("https://courses.zju.edu.cn/api/my-courses", retry=True, json={
                "fields": COURSE_FIELDS, "page": page, "page_size": 100,
                "conditions": {"status": ["ongoing", "notStarted", "closed"], "keyword": "",
                               "classify_type": "recently_started", "display_studio_list": False},
                "showScorePassedStatus": False,
            }), "my-courses")
            out += j.get("courses", [])
            if page >= j.get("pages", 1):
                return out
            page += 1

    def semesters(self) -> dict[int, str]:
        self.ensure()
        j = self.json(self.get("https://courses.zju.edu.cn/api/my-semesters?"), "semesters")
        return {x["id"]: x.get("name") or x.get("real_name") or str(x["id"]) for x in j.get("semesters", [])}

    def uploads(self, course_id: int) -> list[tuple[dict, dict]]:
        """返回 (活动, upload) — 含一般活动与作业附件。"""
        res = []
        j = self.json(self.get(f"https://courses.zju.edu.cn/api/courses/{course_id}/activities"), "activities")
        for a in j.get("activities", []):
            for u in a.get("uploads") or []:
                res.append((a, u))
        page = 1
        while True:
            j = self.json(self.get(
                f"https://courses.zju.edu.cn/api/courses/{course_id}/homework-activities",
                params={"conditions": '{"itemsSortBy":{"predicate":"module","reverse":false}}',
                        "page": page, "page_size": 20, "reloadPage": "false"}), "homework")
            for h in j.get("homework_activities", []):
                for u in h.get("uploads") or []:
                    res.append((h, u))
            if page >= (j.get("pages") or 1):
                return res
            page += 1

    def upload_response(self, uid: int, rid: int, activity: dict | None = None) -> tuple[requests.Response, str]:
        """附件下载来源，依优先序尝试、采用第一个能回档的，回 (response, 来源)：
        1. reference blob — 常规下载
        2. upload blob — 原始档
        3. upload blob + reference 参数 — 参数与官方网页前端相同，部分活动的附件由此提供
        4. 预览器的转档 PDF — document/{rid}/url?preview=true 回 {url}
        全部来源都不可用时丢 DownloadError（codes 为各来源的 HTTP 码）。"""
        base = "https://courses.zju.edu.cn/api/uploads"
        sources = [
            (f"{base}/reference/{rid}/blob", None, "下载"),
            (f"{base}/{uid}/blob", None, "原档"),
        ]
        refer = refer_params(activity)
        if refer:
            sources.append((f"{base}/{uid}/blob", refer, "排程原档"))
        codes = []
        for url, params, src in sources:
            r = self.get(url, params=params, stream=True)
            if r.ok:
                return r, src
            codes.append(r.status_code)
            r.close()
        r = self.get(f"{base}/reference/document/{rid}/url", params={"preview": "true"})
        codes.append(r.status_code)
        if r.ok:
            try:
                url = r.json().get("url")
            except ValueError:
                url = None
            if url:
                r = self.get(secure_url(urljoin(base, url)), stream=True)
                if r.ok:
                    return r, "预览PDF"
                codes.append(r.status_code)
                r.close()
        raise DownloadError(f"下载失败 HTTP {'/'.join(map(str, codes))}", codes)

    def todos(self) -> list[dict]:
        self.ensure()
        return self.json(self.get("https://courses.zju.edu.cn/api/todos?no-intercept=true"), "todos").get("todo_list", [])

    # ---- 活动 / 讨论 / 作业提交（端点取自官方前端 JS）----

    def user_id(self) -> int:
        if not hasattr(self, "_uid"):
            self.ensure()
            m = re.search(r'ng-init="userId=(\d+);', self.get(f"{LMS}/user/index").text)
            if not m:
                raise ZjuError("抓不到自己的 user id（/user/index 改版？）")
            self._uid = int(m.group(1))
        return self._uid

    def activities(self, course_id: int) -> list[dict]:
        """课程所有活动（课件、视频、作业、讨论、网页、连结…）加上测验。"""
        self.ensure()
        acts = self.json(self.get(f"{LMS}/api/courses/{course_id}/activities"), "activities").get("activities", [])
        r = self.get(f"{LMS}/api/courses/{course_id}/exams")
        if r.ok:
            acts += [dict(e, type="exam") for e in self.json(r, "exams").get("exams", [])]
        return acts

    def activity(self, aid: int) -> dict:
        self.ensure()
        r = self.get(f"{LMS}/api/activities/{aid}")
        if r.status_code == 404:
            raise ZjuError(f"找不到活动 {aid}（测验请用课程的 activities 看）")
        return self.json(r, "activity")

    def forum_category(self, aid: int) -> int:
        """讨论活动 id → 讨论区分类 id（发帖、列帖都用分类 id）。"""
        cid = self.activity(aid)["course_id"]
        j = self.json(self.get(f"{LMS}/api/courses/{cid}/topic-categories"), "topic-categories")
        for cat in j.get("topic_categories", []):
            if cat.get("activity_id") == aid:
                return cat["id"]
        raise ZjuError(f"活动 {aid} 不是讨论（或没有讨论区分类）")

    def topics(self, category_id: int) -> list[dict]:
        out, page = [], 1
        while True:
            j = self.json(self.get(f"{LMS}/api/forum/categories/{category_id}",
                                   params={"page": page, "page_size": 50}), "forum")["result"]
            out += j.get("topics", [])
            if page >= (j.get("pages") or 1):
                return out
            page += 1

    def topic(self, tid: int) -> dict:
        self.ensure()
        return self.json(self.get(f"{LMS}/api/topics/{tid}"), "topic")

    def my_forum_posts(self, aid: int) -> tuple[list[dict], list[tuple[dict, dict]]]:
        """自己在某讨论活动的发帖与回帖（回帖附所属主帖）。回帖要逐帖抓详情，只抓有回复的帖。"""
        uid = self.user_id()
        ts = self.topics(self.forum_category(aid))
        mine = [t for t in ts if (t.get("created_by") or {}).get("id") == uid]
        replies = []

        def walk(t, rs):
            for r in rs or []:
                if (r.get("created_by") or {}).get("id") == uid:
                    replies.append((t, r))
                walk(t, r.get("replies"))

        with ThreadPoolExecutor(8) as ex:
            for t in ex.map(lambda t: self.topic(t["id"]), [t for t in ts if t.get("reply_count")]):
                walk(t, t.get("replies"))
        replies.sort(key=lambda x: x[1].get("created_at") or "")
        return mine, replies

    def upload_file(self, path: Path) -> dict:
        """两段式：先登记取得 upload_url，再依 storage_type 送档（学校目前是本地储存 multipart PUT）。"""
        self.ensure()
        pre = self.json(self.post(f"{LMS}/api/uploads", json={
            "name": path.name, "size": path.stat().st_size, "parent_type": None, "parent_id": 0,
            "is_scorm": False, "is_wmpkg": False, "source": "", "is_marked_attachment": False,
            "embed_material_type": "",
        }), "uploads")
        if "upload_url" not in pre:
            raise ZjuError(f"上传登记失败：{pre}")
        if pre.get("storage_type") in ("S3", "QINIU"):
            raise ZjuError(f"储存后端 {pre['storage_type']} 尚未支持（学校改了上传方式）")
        with path.open("rb") as f:
            r = self.req("PUT", pre["upload_url"], files={"file": (path.name, f)}, retry=False,
                         timeout=(6, 600))
        if not r.ok:
            raise ZjuError(f"上传 {path.name} 失败 HTTP {r.status_code}：{r.text[:200]}")
        return pre

    def create_topic(self, category_id: int, title: str, content: str, uploads: list[int]) -> dict:
        r = self.post(f"{LMS}/api/topics", json={"title": title, "content": content,
                                                  "category_id": category_id, "uploads": uploads})
        if not r.ok:
            raise ZjuError(f"发帖失败 HTTP {r.status_code}：{r.text[:200]}")
        return self.json(r, "topic")

    def reply_topic(self, tid: int, content: str, uploads: list[int]) -> dict:
        self.ensure()
        r = self.post(f"{LMS}/api/topics/{tid}/replies", json={"content": content, "uploads": uploads})
        if not r.ok:
            raise ZjuError(f"回帖失败 HTTP {r.status_code}：{r.text[:200]}")
        return self.json(r, "reply")

    def my_submission(self, aid: int) -> dict:
        return self.json(self.get(f"{LMS}/api/course/activities/{aid}/students/{self.user_id()}/submission"),
                         "submission")

    def submit(self, aid: int, comment: str, uploads: list[int], draft: bool, mode: str,
               draft_id: int | None) -> dict:
        """与网页「提交」相同的 payload；已有草稿时用 PUT 盖掉草稿。"""
        body = {"comment": comment, "uploads": uploads, "slides": [], "is_draft": draft, "mode": mode,
                "other_resources": [], "uploads_in_rich_text": []}
        method = "POST"
        if draft_id:
            method, body["submission_id"] = "PUT", draft_id
        r = self.req(method, f"{LMS}/api/course/activities/{aid}/submissions", json=body)
        if not r.ok:
            raise ZjuError(f"提交失败 HTTP {r.status_code}：{r.text[:300]}")
        return self.json(r, "submission")

    # ---- 智云课堂 ----

    def infosimple(self) -> dict:
        self.ensure(need_classroom=True)
        return self.json(self.get("https://classroom.zju.edu.cn/userapi/v1/infosimple",
                                  headers=self.bearer()), "infosimple")["params"]

    def classroom_courses(self) -> list[dict]:
        """智云「我的课程」，任务数沿用网页的 progress.subjectNum。"""
        self.ensure(need_classroom=True)
        out, seen, page = [], set(), 1
        while True:
            j = self.json(self.get(
                "https://education.cmc.zju.edu.cn/personal/courseapi/vlabpassportapi/v1/account-profile/course",
                headers=self.bearer(), params={"nowpage": page, "per-page": 100,
                                              "force_mycourse": 1, "type": "", "model": "", "search": ""}),
                "classroom-courses")
            result = (j.get("params") or {}).get("result")
            if j.get("code") != 1000 or not isinstance(result, dict) or not isinstance(result.get("data"), list):
                raise ZjuError(j.get("message") or "智云个人课程接口返回异常")
            rows = result["data"]
            if not rows:
                return out
            added = 0
            for c in rows:
                cid = int(c["Id"])
                if cid in seen:
                    continue
                seen.add(cid)
                out.append({"course_id": cid, "title": c.get("Title") or "",
                            "teacher": c.get("Teacher") or "", "term": c.get("TermName") or "",
                            "type": c.get("Type") or "",
                            "task_count": int((c.get("progress") or {}).get("subjectNum") or 0)})
                added += 1
            if len(out) >= int(result["total"]):
                return out
            if not added:
                raise ZjuError("智云个人课程分页重复，未能取得完整列表")
            page += 1

    def classroom_search(self, title: str, teacher: str = "") -> list[dict]:
        info = self.infosimple()
        out, page = [], 1
        while True:
            j = self.json(self.get("https://classroom.zju.edu.cn/pptnote/v1/searchlist", headers=self.bearer(), params={
                "tenant_id": 112, "user_id": info["id"], "user_name": info["account"], "page": page,
                "per_page": 16, "title": title, "realname": teacher, "trans": "", "tenant_code": 112,
                "randomKey": random.random()}), "searchlist")
            if j.get("code") != 0:
                raise ZjuError(j.get("msg", "searchlist 失败"))
            lst = j["total"]["list"]
            out += lst
            if not lst or len(out) >= int(j["total"]["total"]):
                return out
            page += 1

    def course_subs(self, course_id: int) -> list[dict]:
        info = self.infosimple()
        j = self.json(self.get("https://yjapi.cmc.zju.edu.cn/courseapi/v3/multi-search/get-course-detail",
                               headers=self.bearer(),
                               params={"course_id": course_id, "student": info["account"]}), "course-detail")
        data = j["data"]
        subs = []
        for year in (data.get("sub_list") or {}).values():
            for month in year.values():
                for week in month.values():
                    for s in week:
                        begin = int(s.get("class_begin") or 0)
                        subs.append({"course_id": course_id, "course_name": data["title"],
                                     "sub_id": int(s["id"]), "sub_name": s["sub_title"],
                                     "lecturer": s.get("lecturer_name", ""), "show": s.get("show"),
                                     "day": dt.datetime.fromtimestamp(begin, CST).date() if begin else None})
        subs.sort(key=lambda s: s["sub_name"])
        return subs

    def day_subs(self, day: dt.date) -> list[dict]:
        self.ensure(need_classroom=True)
        j = self.json(self.get("https://classroom.zju.edu.cn/courseapi/v2/course-live/get-my-course-day",
                               headers=self.bearer(), params={"day": day.isoformat()}), "course-day")
        subs = []
        lst = j.get("list")
        for d in (lst.values() if isinstance(lst, dict) else lst or []):
            for c in d.get("course", []):
                subs.append({"course_id": int(c["id"]), "course_name": c["title"], "sub_id": int(c["sub_id"]),
                             "sub_name": c["sub_title"], "lecturer": c.get("realname", "")})
        return subs

    def ppt_events(self, course_id: int, sub_id: int) -> list[dict]:
        """保留截图时间及原始元数据；只去除分页重复返回的同一事件。"""
        self.ensure(need_classroom=True)
        events, seen = [], set()
        page = 1
        while True:
            j = self.json(self.get("https://classroom.zju.edu.cn/pptnote/v1/schedule/search-ppt", params={
                "course_id": course_id, "sub_id": sub_id, "page": page, "per_page": 100},
                headers=self.bearer()), "search-ppt")
            total = int(j.get("total") or 0)
            added = 0
            for row in j.get("list") or []:
                content = row["content"]
                content = json.loads(content) if isinstance(content, str) else content
                url = content.get("pptimgurl")
                # 同一 URL 在不同时间出现仍是不同事件，不能按 URL 去重。
                key = json.dumps(row, sort_keys=True, ensure_ascii=False)
                if not url or key in seen:
                    continue
                seen.add(key)
                sec = row.get("created_sec")
                try:
                    sec = float(sec) if sec is not None and sec != "" and not isinstance(sec, bool) else None
                    if sec is not None and (not math.isfinite(sec) or sec < 0):
                        sec = None
                except (TypeError, ValueError):
                    sec = None
                events.append({"url": url, "created_sec": sec, "source": row})
                added += 1
            if len(events) >= total or added == 0 or page >= 50:
                if len(events) < total:
                    log(f"[注意] PPT 只拿到 {len(events)}/{total} 个截图事件 course={course_id} sub={sub_id}")
                return events
            page += 1

    def subtitle(self, sub_id: int) -> list[dict]:
        self.ensure(need_classroom=True)
        j = self.json(self.get("https://yjapi.cmc.zju.edu.cn/courseapi/v3/web-socket/search-trans-result",
                               params={"sub_id": sub_id, "format": "json"}), "trans-result")
        if j.get("code") == 10002:  # 未查询到语音数据：当天课程通常还没转完
            return []
        if j.get("code") != 0:
            raise ZjuError(f"取转录失败 code={j.get('code')} {j.get('msg', '')}")
        lst = j.get("list") or []
        return lst[0].get("all_content", []) if lst else []

    def video_catalogue(self, course_id: int) -> dict[int, list[str]]:
        self.ensure(need_classroom=True)
        r = self.get("https://classroom.zju.edu.cn/courseapi/v2/course/catalogue",
                     params={"course_id": course_id}, headers=self.bearer())
        j = self.json(r, "catalogue")
        if not r.ok or not j.get("success"):
            raise ZjuError(f"录播目录读取失败 HTTP {r.status_code}")
        items = (j.get("result") or {}).get("data")
        if not isinstance(items, list):
            raise ZjuError("录播目录格式错误：缺少 result.data 列表")
        return parse_video_catalogue(items)


# ---------------- helpers ----------------

def read_course(directory: Path, platform=None, cid=None):
    path = directory / "course.json"
    try:
        context_path(directory, "course.json")
        data = context_json(path)
        if (not isinstance(data, dict) or data.get("schema_version") != 2
                or not isinstance(data.get("platform"), str) or data["platform"] not in DEFAULT_LIBRARIES
                or type(data.get("course_id")) is not int or data["course_id"] < 1):
            raise ValueError("不支持的课程格式")
        if (platform and data["platform"] != platform) or (cid is not None and data["course_id"] != cid):
            raise ValueError("课程平台或 ID 不符")
        items = data.get("lessons" if data["platform"] == "classroom" else "files")
        if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
            raise ValueError("课程资料列表无效")
        if data["platform"] == "classroom":
            ids = [item.get("sub_id") for item in items]
            if any(type(sid) is not int or sid < 1 for sid in ids) or len(ids) != len(set(ids)):
                raise ValueError("堂次 ID 无效或重复")
        return data
    except (OSError, ValueError, RuntimeError) as e:
        raise ZjuError(f"课程元数据不可用或身份冲突：{path}（{e}）") from None


def library_path(a, platform):
    if getattr(a, "library", None) is not None:
        value = a.library
    else:
        libraries = load_config().get("libraries", {})
        if not isinstance(libraries, dict):
            raise ZjuError("config.json 的 libraries 必须是对象")
        value = libraries.get(platform, DEFAULT_LIBRARIES[platform])
    if not isinstance(value, (str, Path)) or not str(value).strip():
        raise ZjuError("资料库路径必须是非空路径")
    return Path(value).expanduser().resolve()


def local_courses(library, platform):
    if not library.exists():
        return []
    if not library.is_dir() or (library / "course.json").exists():
        raise ZjuError("--library 必须是课程目录的父目录；单门课程请使用 --course-dir")
    rows = []
    for path in sorted(library.glob("*/course.json")):
        data = read_course(path.parent)
        if data["platform"] == platform:
            rows.append((path.parent.resolve(), data))
    ids = [data["course_id"] for _, data in rows]
    if len(ids) != len(set(ids)):
        duplicates = [str(p) for p, d in rows if ids.count(d["course_id"]) > 1]
        raise ZjuError("同一课程有多个本地目录，请用 --course-dir 明确选择：\n" + "\n".join(duplicates))
    return rows


def resolve_course(a, platform, cid=None, *, name=None, writing=False, optional=False):
    """唯一课程定位入口；只读可用 cwd，写操作只用显式目录或配置的资料库。"""
    direct = getattr(a, "course_dir", None)
    if direct is not None and getattr(a, "library", None) is not None:
        raise ZjuError("--library 与 --course-dir 互斥")
    if direct is None and not writing and getattr(a, "library", None) is None and (Path.cwd() / "course.json").exists():
        direct = Path.cwd()
    if direct is not None:
        if not str(direct).strip():
            raise ZjuError("--course-dir 不能为空")
        directory = Path(direct).expanduser().resolve()
        if (directory / "course.json").exists():
            data = read_course(directory, platform, cid)
            return directory, data["course_id"], data
        if not writing or cid is None:
            raise ZjuError("课程目录尚未初始化；首次 sync 必须明确课程 ID")
        if directory.exists() and not directory.is_dir():
            raise ZjuError("课程路径不是目录")
        if (directory / ".zju").exists():
            raise ZjuError("未初始化目录含 .zju 状态，不能自动接管")
        return directory, cid, None
    if cid is None:
        raise ZjuError("请提供课程 ID 或 --course-dir")
    library = library_path(a, platform)
    matches = [(p, d) for p, d in local_courses(library, platform) if d["course_id"] == cid]
    if matches:
        return matches[0][0], cid, matches[0][1]
    if not writing:
        if optional:
            return None, cid, None
        raise ZjuError(f"资料库中未找到课程 {cid}；请检查 --library 或指定 --course-dir")
    suffix = str(cid) if platform == "classroom" else f"lms-{cid}"
    directory = library / f"{safe_name(name or str(cid))[:110]} ({suffix})"
    if directory.exists():
        raise ZjuError(f"新课程目标目录已存在但未登记，不能自动接管：{directory}；请明确 --course-dir")
    return directory, cid, None


def initialize_course(directory, platform, cid, name):
    if (directory / "course.json").exists():
        return read_course(directory, platform, cid)
    data = dict(schema_version=2, platform=platform, course_id=cid, name=name,
                updated_at=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"))
    data["lessons" if platform == "classroom" else "files"] = []
    atomic_text(directory / "course.json", json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    return data


class DownloadState:
    """课程内状态；每次更新在锁内重读合并，pending 只代表文件所有权，不代表下载完成。"""
    def __init__(self, directory: Path):
        self.directory = directory
        self.path = context_path(directory, ".zju/downloads.json")
        self.course_id = None
        self.data = self._read()

    def _read(self):
        if not self.path.exists():
            return {}
        try:
            data = context_json(self.path)
            course = read_course(self.directory)
            self.course_id = course["course_id"]
            if (not isinstance(data, dict) or data.get("schema_version") != 1
                    or data.get("platform") != course["platform"] or data.get("course_id") != course["course_id"]
                    or not isinstance(data.get("entries"), dict)):
                raise ValueError("状态身份不符")
            for key, entry in data["entries"].items():
                if not isinstance(entry, dict) or entry.get("status") not in ("pending", "complete"):
                    raise ValueError("下载记录无效")
                context_path(self.directory, entry.get("path"))
                kind = entry.get("kind")
                kinds = {"attachment"} if course["platform"] == "lms" else {
                    "transcripts", "ppt_pdfs", "ppt_events", "recordings", "audio", "ppt_images", "ppt_image"}
                if not isinstance(kind, str) or kind not in kinds:
                    raise ValueError("下载资料类别无效")
                if kind != "attachment" and (type(entry.get("sub_id")) is not int or entry["sub_id"] < 1):
                    raise ValueError("下载堂次 ID 无效")
                if kind in ("recordings", "audio") and (type(entry.get("part")) is not int or entry["part"] < 1):
                    raise ValueError("媒体分段无效")
                if kind in ("recordings", "audio") and entry["status"] == "complete" and (
                        type(entry.get("part_count")) is not int or entry["part_count"] < entry["part"]):
                    raise ValueError("媒体分段总数无效")
                if entry["status"] == "complete" and (type(entry.get("size")) is not int or entry["size"] <= 0):
                    raise ValueError("完成记录大小无效")
            return data["entries"]
        except (OSError, ValueError, RuntimeError) as e:
            raise ZjuError(f"下载状态损坏，保留原文件：{self.path}（{e}）") from None

    def get(self, key):
        return self.data.get(key)

    def put(self, key, value):
        context_path(self.directory, value.get("path"))
        with download_lock(self.path, "下载状态", timeout=30):
            data = self._read()
            if any(k != key and r["path"] == value["path"] for k, r in data.items()):
                raise ZjuError(f"文件已由其他资料登记：{value['path']}")
            data[key] = value
            course = read_course(self.directory)
            payload = dict(schema_version=1, platform=course["platform"], course_id=course["course_id"], entries=data)
            atomic_text(self.path, json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
            self.data = data

    def claim(self, key, path, **details):
        # 调用者持课程锁；未登记的用户文件，即使 --force 也不能覆盖。
        relative = path.relative_to(self.directory).as_posix()
        context_path(self.directory, relative)
        rec = self.get(key)
        if rec and rec["path"] == relative:
            return
        ancillary = [path]
        if details.get("kind") == "recordings":
            ancillary += [path.with_name(f".{path.name}{suffix}") for suffix in (".part", ".part.json", ".part.json.tmp", ".download.lock")]
        if any(p.exists() or p.is_symlink() for p in ancillary):
            raise ZjuError(f"目标或进度是未登记的用户文件，未覆盖：{path}")
        self.put(key, dict(path=relative, status="pending", **details))

    def finish(self, key, path, **details):
        previous = self.get(key)
        if not previous or previous["path"] != path.relative_to(self.directory).as_posix():
            raise ZjuError("资料尚未登记，不能标记下载完成")
        self.put(key, dict(previous, **details, status="complete", size=path.stat().st_size,
                           at=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")))


def material_details(sub, kind, extension, part=""):
    category = {"转录": "transcripts", "智云PPT": "ppt_pdfs" if extension == "pdf" else "ppt_events",
                "录播": "recordings", "音频": "audio"}[kind]
    details = dict(kind=category, sub_id=sub["sub_id"])
    if category == "transcripts":
        details["format"] = extension
    if category in ("recordings", "audio"):
        details["part"] = int(part.strip(" -") or 1)
    return details


def material_complete(directory, key, state=None):
    state = state if state is not None else DownloadState(directory)
    rec = state.get(key)
    if not rec or rec["status"] != "complete":
        return False
    path = context_path(directory, rec["path"])
    try:
        return path.is_file() and valid_course_material(path, rec["kind"], state.course_id,
                                                        rec["sub_id"], rec)
    except (OSError, ValueError, ZjuError):
        return False


def atomic_text(path: Path, text: str):
    fd, name = tempfile.mkstemp(prefix=".part-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def valid_course_material(path: Path, kind: str, cid: int, sid: int, recorded: dict | None) -> bool:
    """只检查最终文件：文本/JSON、PDF 结束标记、MP4 顶层边界及已记录大小，不解码媒体。"""
    size = path.stat().st_size
    if not size or (recorded is not None and size != recorded.get("size")):
        return False
    if kind == "transcripts":
        try:
            return bool(path.read_text(encoding="utf-8").strip())
        except UnicodeError:
            return False
    if kind == "ppt_events":
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return (isinstance(data, dict) and data.get("schema_version") == 1
                    and data.get("course_id") == cid and data.get("sub_id") == sid
                    and data.get("pdf_file") == path.with_suffix(".pdf").name
                    and isinstance(data.get("events"), list) and isinstance(data.get("pages"), list))
        except (ValueError, UnicodeError):
            return False
    with path.open("rb") as f:
        if kind == "ppt_pdfs":
            if f.read(5) != b"%PDF-":
                return False
            f.seek(max(0, size - 1024))
            return b"%%EOF" in f.read()
        offset, boxes = 0, set()
        for _ in range(128):
            f.seek(offset)
            header = f.read(16)
            if len(header) < 8:
                return False
            length, name = struct.unpack_from(">I4s", header)
            width = 8
            if length == 1:
                if len(header) < 16:
                    return False
                length, = struct.unpack_from(">Q", header, 8)
                width = 16
            elif length == 0:
                length = size - offset
            if length < width or offset + length > size or (offset == 0 and name != b"ftyp"):
                return False
            boxes.add(name)
            offset += length
            if offset == size:
                return {b"ftyp", b"moov", b"mdat"} <= boxes
    return False


def course_materials(directory: Path, cid: int) -> dict:
    materials = {}
    state = DownloadState(directory)
    for key, rec in state.data.items():
        if rec.get("kind") not in ("transcripts", "ppt_pdfs", "ppt_events", "audio", "recordings"):
            continue
        if not material_complete(directory, key, state):
            continue
        item = {k: rec[k] for k in ("path", "format", "part") if k in rec}
        materials.setdefault(rec["sub_id"], {}).setdefault(rec["kind"], []).append(item)
    for files in materials.values():
        pdfs = {Path(item["path"]).with_suffix(".json").as_posix() for item in files.get("ppt_pdfs", [])}
        files["ppt_events"] = [item for item in files.get("ppt_events", []) if item["path"] in pdfs]
        for items in files.values():
            items.sort(key=lambda item: (item.get("part", 0), item["path"]))
    return materials


def write_course_metadata(directory: Path, course: dict, subs: list[dict] | None):
    """调用者持课程锁；未获取的堂次信息保留，资料只来自本课程完成记录。"""
    cid = course["course_id"]
    out = directory / "course.json"
    old = read_course(directory, "classroom", cid)
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    data = {"schema_version": 2, "platform": "classroom", "course_id": cid,
            "updated_at": now, "lessons_fetch_status": "ok" if subs is not None else "error"}
    for target, source in (("name", "title"), ("teacher", "teacher"), ("term", "term"), ("type", "type")):
        data[target] = course.get(source) or old.get(target)
    data["task_count"] = course.get("task_count", old.get("task_count"))
    data["lessons_checked_at"] = now if subs is not None else old.get("lessons_checked_at")
    lessons = {}
    for lesson in old.get("lessons", []):
        if not isinstance(lesson, dict) or not isinstance(lesson.get("sub_id"), int):
            raise ZjuError(f"课程堂次元数据格式错误，未覆盖：{out}")
        # 只保留白名单字段，文件引用每次从磁盘重新核对。
        lessons[lesson["sub_id"]] = {k: lesson.get(k) for k in
            ("sub_id", "name", "date", "lecturer", "availability", "last_seen_at")}
    for s in subs or []:
        lesson = lessons.setdefault(s["sub_id"], {"sub_id": s["sub_id"]})
        for target, source in (("name", "sub_name"), ("date", "day"), ("lecturer", "lecturer")):
            value = s.get(source)
            if value is not None and value != "":
                lesson[target] = value.isoformat() if isinstance(value, dt.date) else value
        if s.get("show") in ("yes", "no"):
            lesson["availability"] = "listed" if s["show"] == "yes" else "unlisted"
        lesson["last_seen_at"] = now
    materials = course_materials(directory, cid)
    for sid in materials:
        lessons.setdefault(sid, {"sub_id": sid})
    for sid, lesson in lessons.items():
        for key in ("name", "date", "lecturer", "last_seen_at"):
            lesson.setdefault(key, None)
        lesson["availability"] = lesson.get("availability") or "unknown"
        lesson["files"] = {kind: materials.get(sid, {}).get(kind, []) for kind in
                           ("transcripts", "ppt_pdfs", "ppt_events", "audio", "recordings")}
    data["lessons"] = sorted(lessons.values(), key=lambda s: (s["date"] or "9999", s["sub_id"]))
    atomic_text(out, json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    log(f"[课程元数据] {out}")


def stream_to(r: requests.Response, dest: Path, limit: int | None = None, *, mp4: bool = False, prepare=None) -> Path:
    """写暂存档再 rename；验证长度、拒收空档和错误页，免得坏档被记进完成状态后永远不再重新下载。"""
    expected = r.headers.get("Content-Length")
    expected = int(expected) if expected and expected.isdigit() and not r.headers.get("Content-Encoding") else None
    if limit and expected and expected > limit:
        r.close()
        raise TooBig(f"{expected / 2**20:.0f}MB")
    if "text/html" in r.headers.get("Content-Type", "") and dest.suffix.lower() not in (".html", ".htm"):
        r.close()
        raise ZjuError("服务器返回 HTML（错误页或登录页），不存档")
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix=".part-")
    try:
        written = 0
        with os.fdopen(fd, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
                written += len(chunk)
                if limit and written > limit:
                    raise TooBig(f">{limit / 2**20:.0f}MB")
        if written == 0:
            raise ZjuError("服务器返回空档")
        if expected is not None and written != expected:
            raise ZjuError(f"下载不完整：{written}/{expected} bytes")
        with open(tmp, "rb") as f:
            head = f.read(12)
        if mp4 and head[4:8] != b"ftyp":
            raise ZjuError("回应不是 MP4（可能是错误页或 HLS 播放列表），不存档")
        # preview 版常是 PDF，但档名还是 .pptx/.docx — 补副档名免得打不开
        if head[:5] == b"%PDF-" and dest.suffix.lower() != ".pdf":
            dest = dest.with_name(dest.name + ".pdf")
        if prepare is not None:
            prepare(dest)
        os.replace(tmp, dest)
        return dest
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    finally:
        r.close()


@contextmanager
def download_lock(dest: Path, kind: str, *, timeout=0):
    """同一输出文件只允许一个程序写入，锁文件保留以免 inode 更换。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    # 锁档保留以避免删除后新旧 inode 被不同程序同时锁定。
    lock_path = dest.with_name(f".{dest.name}.download.lock")
    with lock_path.open("a+b") as lock_file:
        deadline = time.monotonic() + timeout
        while True:
            try:
                if sys.platform == "win32":
                    import msvcrt
                    lock_file.seek(0)
                    if not lock_file.read(1):
                        lock_file.write(b"\0")
                        lock_file.flush()
                    lock_file.seek(0)
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as e:
                if time.monotonic() >= deadline:
                    raise ZjuError(f"另一个程序正在处理此{kind}") from e
                time.sleep(0.05)
                continue
            break
        yield


def download_video(z: Zju, url: str, dest: Path, limit: int | None, jobs: int,
                   *, chunk_size: int = 32 * 2**20, restart: bool = False,
                   pool: ThreadPoolExecutor | None = None) -> Path:
    """保留已校验分片及 checkpoint，跨次执行只补缺片。"""
    with download_lock(dest, "录播"):
        return _download_video(z, url, dest, limit, jobs, chunk_size, restart, pool)


def _download_video(z: Zju, url: str, dest: Path, limit: int | None, jobs: int,
                    chunk_size: int, restart: bool, pool: ThreadPoolExecutor | None = None) -> Path:
    tmp = dest.with_name(f".{dest.name}.part")
    checkpoint = dest.with_name(f".{dest.name}.part.json")

    def clear_partial():
        tmp.unlink(missing_ok=True)
        checkpoint.unlink(missing_ok=True)
        checkpoint.with_suffix(".json.tmp").unlink(missing_ok=True)

    headers = {"Range": "bytes=0-0", "Accept-Encoding": "identity"}
    probe = z.get(url, headers=headers, stream=True)
    if probe.status_code == 200:
        log("[单连接] 服务器不支持 Range，这次从头下载")
        result = stream_to(probe, dest, limit, mp4=True)
        clear_partial()
        return result
    try:
        match = re.fullmatch(r"bytes 0-0/(\d+)", probe.headers.get("Content-Range", ""))
        if probe.status_code != 206 or not match:
            raise ZjuError(f"录播 Range 探测失败 HTTP {probe.status_code}")
        total = int(match[1])
        if total < 12:
            raise ZjuError("录播文件过小")
        if limit and total > limit:
            raise TooBig(f"{total / 2**20:.1f}MB")
        if probe.headers.get("Content-Encoding", "identity") != "identity":
            raise ZjuError("Range 回应不应使用压缩编码")
        if len(probe.content) != 1:
            raise ZjuError("Range 探测长度错误")
        etag = probe.headers.get("ETag", "")
        validator_type = "etag" if etag and not etag.startswith("W/") else "last_modified"
        validator = etag if validator_type == "etag" else probe.headers.get("Last-Modified")
    finally:
        probe.close()
    # 去掉可能刷新的签名参数；远端版本仍须由 validator 及长度确认。
    source = urlparse(url)._replace(query="", fragment="").geturl()
    metadata = {"version": 1, "source": hashlib.sha256(source.encode()).hexdigest(),
                "total": total, "chunk_size": chunk_size,
                "validator_type": validator_type, "validator": validator}
    saved = {}
    try:
        saved = json.loads(checkpoint.read_text())
    except (OSError, ValueError):
        pass
    reusable = (not restart and bool(validator) and isinstance(saved, dict)
                and all(saved.get(k) == v for k, v in metadata.items())
                and tmp.is_file() and tmp.stat().st_size == total)
    done = {}
    if reusable and isinstance(saved.get("done"), dict):
        with tmp.open("rb") as f:
            for start in range(0, total, chunk_size):
                digest = saved["done"].get(str(start))
                if not isinstance(digest, str):
                    continue
                f.seek(start)
                h = hashlib.sha256()
                remaining = min(chunk_size, total - start)
                while remaining:
                    block = f.read(min(1 << 20, remaining))
                    if not block:
                        break
                    h.update(block)
                    remaining -= len(block)
                if not remaining and h.hexdigest() == digest:
                    done[str(start)] = digest
    else:
        if tmp.exists():
            log("[重新下载] 本地状态或远端版本变更，无法沿用分片")
        with tmp.open("wb") as f:
            f.truncate(total)
    if not validator:
        log("[提示] 服务器未提供版本标记，跨次执行需重新下载")
    state = dict(metadata, done=done)
    state_lock = threading.Lock()
    stopped = threading.Event()

    def save_state():
        staging = checkpoint.with_suffix(".json.tmp")
        with staging.open("w") as f:
            json.dump(state, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(staging, checkpoint)

    save_state()
    resumed = sum(min(chunk_size, total - int(start)) for start in done)
    if resumed:
        log(f"[续传] 已有 {resumed / 2**20:.1f}/{total / 2**20:.1f}MB，补下载缺少分片")

    def grab(start):
        end = min(start + chunk_size, total) - 1
        h = {"Range": f"bytes={start}-{end}", "Accept-Encoding": "identity"}
        if validator:
            h["If-Range"] = validator
        for attempt in range(3):
            if stopped.is_set():
                raise ZjuError("下载已中止")
            try:
                r = z.get(url, headers=h, stream=True)
                try:
                    expected_range = f"bytes {start}-{end}/{total}"
                    if r.status_code != 206 or r.headers.get("Content-Range") != expected_range:
                        raise ZjuError(f"分片 {start}-{end} 范围不符 HTTP {r.status_code}")
                    if r.headers.get("Content-Encoding", "identity") != "identity":
                        raise ZjuError("分片回应使用压缩编码")
                    written = 0
                    digest = hashlib.sha256()
                    with tmp.open("r+b") as f:
                        f.seek(start)
                        for block in r.iter_content(1 << 20):
                            if stopped.is_set():
                                raise ZjuError("下载已中止")
                            if written + len(block) > end - start + 1:
                                raise ZjuError("分片长度超出范围")
                            f.write(block)
                            digest.update(block)
                            written += len(block)
                        if written != end - start + 1:
                            raise ZjuError(f"分片下载不完整：{written}/{end - start + 1}")
                        f.flush()
                        os.fsync(f.fileno())
                    with state_lock:
                        done[str(start)] = digest.hexdigest()
                        save_state()
                    return written
                finally:
                    r.close()
            except (requests.RequestException, ZjuError):
                if attempt == 2 or stopped.is_set():
                    raise
                time.sleep(0.5 * 2**attempt)

    completed = resumed
    next_report = completed * 100 // total
    started = time.monotonic()
    try:
        with (nullcontext(pool) if pool is not None else ThreadPoolExecutor(max_workers=jobs)) as pool:
            futures = [pool.submit(grab, start) for start in range(0, total, chunk_size)
                       if str(start) not in done]
            try:
                for future in as_completed(futures):
                    completed += future.result()
                    percent = completed * 100 // total
                    if percent >= next_report or completed == total:
                        speed = (completed - resumed) / 2**20 / max(time.monotonic() - started, 0.001)
                        log(f"[进度] {percent}%  {completed / 2**20:.1f}/{total / 2**20:.1f}MB  {speed:.1f}MB/s")
                        next_report = percent + 10
            except BaseException:
                stopped.set()
                for future in futures:
                    future.cancel()
                # 外部共享线程池不会在这里关闭，释放文件锁前仍须等本文件的 worker 停下。
                wait(futures)
                raise
    except BaseException:
        log("[保留分片] 下次执行相同下载指令即可续传")
        raise
    with tmp.open("rb") as f:
        valid_mp4 = f.read(12)[4:8] == b"ftyp"
    if not valid_mp4:
        clear_partial()
        raise ZjuError("回应不是 MP4（可能是错误页或 HLS 播放列表），不存档")
    os.replace(tmp, dest)
    clear_partial()
    return dest


# ---------------- audio-only MP4 ranges ----------------

def mp4_boxes(data):
    data = memoryview(data)
    offset = 0
    while offset < len(data):
        if len(data) - offset < 8:
            raise ZjuError("MP4 索引截断")
        size, kind = struct.unpack_from(">I4s", data, offset)
        header = 8
        if size == 1:
            if len(data) - offset < 16:
                raise ZjuError("MP4 扩展索引截断")
            size, = struct.unpack_from(">Q", data, offset + 8)
            header = 16
        elif size == 0:
            size = len(data) - offset
        if size < header or size > len(data) - offset:
            raise ZjuError("MP4 索引长度错误")
        yield kind, data[offset + header:offset + size]
        offset += size


def mp4_child(data, kind):
    for name, payload in mp4_boxes(data):
        if name == kind:
            return payload
    raise ZjuError(f"MP4 音轨索引缺少 {kind.decode('ascii', 'replace')}")


def mp4_box(kind, data):
    return struct.pack(">I4s", len(data) + 8, kind) + bytes(data)


def mp4_ints(data, kind="I"):
    result = array.array(kind)
    if len(data) % result.itemsize:
        raise ZjuError("MP4 音轨索引长度错误")
    result.frombytes(data)
    if sys.byteorder == "little":
        result.byteswap()
    return result


class AudioIndex:
    """紧凑数组保存音频偏移；保留第一条音轨，重建时移除视频索引。"""

    def __init__(self, ftyp, moov, total):
        self.ftyp = bytes(ftyp)
        track = next((p for n, p in mp4_boxes(moov) if n == b"trak"
                      and bytes(mp4_child(mp4_child(p, b"mdia"), b"hdlr")[8:12]) == b"soun"), None)
        if track is None:
            raise ZjuError("录播没有音轨")
        self.moov = bytearray(mp4_box(b"mvhd", mp4_child(moov, b"mvhd")) + mp4_box(b"trak", track))
        stbl = mp4_child(mp4_child(mp4_child(track, b"mdia"), b"minf"), b"stbl")
        stsz = mp4_child(stbl, b"stsz")
        if len(stsz) < 12:
            raise ZjuError("MP4 音频样本索引截断")
        constant, samples = struct.unpack_from(">II", stsz, 4)
        sizes = mp4_ints(stsz[12:])
        if (constant and len(sizes)) or (not constant and len(sizes) != samples):
            raise ZjuError("MP4 音频样本数错误")
        stsc = mp4_child(stbl, b"stsc")
        offsets = next(((n, p) for n, p in mp4_boxes(stbl) if n in (b"stco", b"co64")), None)
        if len(stsc) < 8 or offsets is None or len(offsets[1]) < 8:
            raise ZjuError("MP4 音频分片索引缺失")
        count, = struct.unpack_from(">I", stsc, 4)
        sc = mp4_ints(stsc[8:])
        name, co = offsets
        self.starts = mp4_ints(co[8:], "I" if name == b"stco" else "Q")
        if len(sc) != count * 3 or len(self.starts) != struct.unpack_from(">I", co, 4)[0]:
            raise ZjuError("MP4 音频分片数错误")
        if (not samples or not self.starts or not count or sc[0] != 1
                or any(sc[i + 1] == 0 or sc[i + 2] == 0 or sc[i] > len(self.starts)
                       or (i and sc[i] <= sc[i - 3]) for i in range(0, len(sc), 3))):
            raise ZjuError("MP4 音轨索引无效（不支持分片 MP4）")
        self.lengths, self.positions = array.array("Q"), array.array("Q")
        self.size = sample = entry = 0
        for i, offset in enumerate(self.starts, 1):
            while entry + 3 < len(sc) and i >= sc[entry + 3]:
                entry += 3
            n = sc[entry + 1]
            if sample + n > samples:
                raise ZjuError("MP4 音频样本数不匹配")
            length = constant * n if constant else sum(sizes[sample:sample + n])
            if (not length or offset + length > total
                    or (i > 1 and offset < self.starts[i - 2] + self.lengths[-1])):
                raise ZjuError("MP4 音频范围越界或重叠")
            self.positions.append(self.size)
            self.lengths.append(length)
            self.size += length
            sample += n
        if sample != samples:
            raise ZjuError("MP4 音频样本未完整覆盖")

    def groups(self):
        # 每批最多 360 个音频分片；较长偏移自动缩小批次，避免 Range 头超过 8KiB。
        first = count = 0
        header_size = 6
        for i, (start, length) in enumerate(zip(self.starts, self.lengths)):
            size = len(f"{start}-{start + length - 1},")
            if count and (count == 360 or header_size + size > 8000):
                yield first, i
                first, count, header_size = i, 0, 6
            count += 1
            header_size += size
        if count:
            yield first, len(self.starts)

    def assemble(self, raw: Path, dest: Path):
        start = len(self.ftyp) + len(self.moov) + 8 + 16

        def rewrite(data):
            for kind, payload in mp4_boxes(data):
                if kind in (b"trak", b"mdia", b"minf", b"stbl"):
                    rewrite(payload)
                elif kind in (b"stco", b"co64"):
                    width = "I" if kind == b"stco" else "Q"
                    if width == "I" and start + self.positions[-1] >= 2**32:
                        raise ZjuError("音轨过大，超出原 MP4 的 32 位索引范围")
                    values = array.array(width, (start + p for p in self.positions))
                    if sys.byteorder == "little":
                        values.byteswap()
                    payload[8:] = values.tobytes()

        rewrite(self.moov)
        with dest.open("wb") as out, raw.open("rb") as source:
            out.write(self.ftyp)
            out.write(mp4_box(b"moov", self.moov))
            out.write(struct.pack(">I4sQ", 1, b"mdat", self.size + 16))
            shutil.copyfileobj(source, out, 1 << 20)


async def audio_parallel(items, jobs, fn):
    """只创建 jobs 个协程，失败或中断时先停止所有请求再清理临时文件。"""
    items = iter(items)

    async def worker():
        for item in items:
            await fn(item)

    tasks = [asyncio.create_task(worker()) for _ in range(jobs)]
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def default_index_cache_dir() -> Path:
    """用户级缓存；XDG/LOCALAPPDATA 必须为有效绝对路径，否则使用 home 回退。"""
    windows = sys.platform == "win32"
    value = os.environ.get("LOCALAPPDATA" if windows else "XDG_CACHE_HOME", "")
    try:
        base = Path(value).expanduser()
        valid = bool(value.strip()) and "\0" not in value and base.is_absolute()
    except (OSError, RuntimeError, ValueError):
        valid = False
    if not valid:
        base = Path.home() / ("AppData/Local" if windows else ".cache")
    return base / ("zju-learning/cache" if windows else "zju-learning")


def add_index_cache_arguments(p):
    group = p.add_mutually_exclusive_group()
    group.add_argument("--index-cache-dir", help="共享 MP4 索引缓存目录；默认用户缓存目录/zju-learning（Windows 为 zju-learning/cache）")
    group.add_argument("--no-index-cache", action="store_true", help="关闭跨调用 MP4 索引缓存；仍正常按需读取")
    p.add_argument("--index-cache-mb", type=int, default=256, help="共享索引缓存总容量 MiB（默认 256，正整数）")


def index_cache_options(a):
    size = getattr(a, "index_cache_mb", 256)
    if isinstance(size, bool) or not isinstance(size, int) or size < 1:
        raise ZjuError("--index-cache-mb 必须是正整数")
    directory = getattr(a, "index_cache_dir", None)
    if directory == "":
        raise ZjuError("--index-cache-dir 不能为空")
    disabled = getattr(a, "no_index_cache", False)
    if disabled and directory is not None:
        raise ZjuError("--no-index-cache 不能与 --index-cache-dir 混用")
    return dict(index_cache_dir=None if disabled else directory or default_index_cache_dir(),
                index_cache_bytes=size * 2**20)


def mp4_resource(url):
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
        raise ZjuError("录播必须使用无内嵌凭据的 HTTP(S) URL")
    # 不保存签名查询，也不把学校认证交给媒体客户端。
    return hashlib.sha256(f"{parts.scheme}://{parts.netloc}{parts.path}".encode()).hexdigest()


def mp4_validator(headers, previous=None):
    etag, modified = headers.get("ETag"), headers.get("Last-Modified")
    strong = etag and re.fullmatch(r'"[\x21\x23-\x7e\x80-\xff]*"', etag)
    validator = (("ETag", etag) if strong else
                 ("Last-Modified", modified) if modified else None)
    if not validator:
        raise ZjuError("服务器未提供强 ETag 或 Last-Modified，无法确认视频版本一致性")
    if previous is not None and validator != previous:
        raise ZjuError("远端视频版本已变化，停止读取；请重新运行")
    return validator


def mp4_identity(url, total, validator):
    identity = dict(resource_sha256=mp4_resource(url), size=total, validator=list(validator))
    if validator[0] != "ETag":
        # Last-Modified 仅验证本次 Range，不能证明跨调用内容相同。
        identity["invocation"] = os.urandom(16).hex()
    return identity


_DEFAULT_INDEX_CACHE = object()


def open_mp4_index_cache(directory, limit, identity):
    if directory is None or identity["validator"][0] != "ETag":
        return None
    try:
        if directory is _DEFAULT_INDEX_CACHE:
            directory = default_index_cache_dir()
        return MP4IndexCache(directory, limit, identity)
    except (OSError, RuntimeError, ValueError) as error:
        log(f"[索引缓存] 不可用（{type(error).__name__}），继续正常 Range 读取")
        return None


class AudioRanges:
    def __init__(self, client, url):
        mp4_resource(url)
        self.client, self.url = client, url
        self.total = None
        self.validator = None
        self.received = 0
        self.index_cache = None

    async def get(self, ranges):
        if self.index_cache and len(ranges) == 1:
            cached = self.index_cache.read(*ranges[0])
            if cached is not None:
                return cached
        headers = {"Range": "bytes=" + ",".join(f"{a}-{b}" for a, b in ranges),
                   "Accept-Encoding": "identity"}
        if self.validator:
            headers["If-Range"] = self.validator[1]
        for attempt in range(3):
            try:
                async with self.client.stream("GET", self.url, headers=headers, follow_redirects=False) as r:
                    if r.status_code != 206:
                        raise ZjuError(f"音频范围请求失败 HTTP {r.status_code}（不下载整个视频）")
                    if r.headers.get("Content-Encoding", "identity") != "identity":
                        raise ZjuError("音频范围响应使用了压缩编码")
                    if self.total is None:
                        match = re.fullmatch(r"bytes \d+-\d+/(\d+)", r.headers.get("Content-Range", ""))
                        if not match:
                            raise ZjuError("录播范围探测失败")
                        self.total = int(match[1])
                    self.validator = mp4_validator(r.headers, self.validator)
                    maximum = sum(b - a + 1 for a, b in ranges) + len(ranges) * 1024 + 8192
                    body = bytearray()
                    async for block in r.aiter_bytes():
                        self.received += len(block)
                        if len(body) + len(block) > maximum:
                            raise ZjuError("音频响应超过请求范围")
                        body.extend(block)
                    expected = dict(ranges)
                    mime = r.headers.get("Content-Type", "")
                    if len(ranges) == 1 and not mime.lower().startswith("multipart/byteranges"):
                        a, b = ranges[0]
                        if r.headers.get("Content-Range") != f"bytes {a}-{b}/{self.total}" or len(body) != b - a + 1:
                            raise ZjuError("音频响应范围或长度错误")
                        return bytes(body)
                    match = re.search(r'boundary="?([^";\s]+)', mime, re.I)
                    if not mime.lower().startswith("multipart/byteranges") or not match:
                        raise ZjuError("服务器不支持音频多范围请求")
                    boundary = match[1].encode()
                    data = bytes(body)
                    if data.startswith(b"\r\n"):
                        data = data[2:]
                    parts = data.split(b"\r\n--" + boundary)
                    if not parts[0].startswith(b"--" + boundary + b"\r\n") or parts[-1].strip() != b"--":
                        raise ZjuError("音频多范围响应边界错误")
                    parts[0] = parts[0][len(boundary) + 4:]
                    parsed = {}
                    for part in parts[:-1]:
                        if part.startswith(b"\r\n"):
                            part = part[2:]
                        head, separator, payload = part.partition(b"\r\n\r\n")
                        match = re.search(br"Content-Range: bytes (\d+)-(\d+)/(\d+)\r?$", head, re.I | re.M)
                        if not separator or not match:
                            raise ZjuError("音频多范围响应头错误")
                        a, b, total = map(int, match.groups())
                        if a in parsed or expected.get(a) != b or total != self.total or len(payload) != b - a + 1:
                            raise ZjuError("音频多范围响应不完整或越界")
                        parsed[a] = payload
                    if parsed.keys() != expected.keys():
                        raise ZjuError("音频多范围响应缺少分片")
                    return b"".join(parsed[a] for a, _ in ranges)
            except httpx.HTTPError as e:
                if attempt == 2:
                    raise ZjuError(f"音频网络请求失败（{type(e).__name__}）") from e
                await asyncio.sleep(0.5 * 2**attempt)


async def download_audio_ranges(url: str, raw: Path, jobs: int, limit: int | None, *,
                                index_cache_dir=_DEFAULT_INDEX_CACHE,
                                index_cache_bytes=256 * 2**20) -> AudioIndex:
    # 不读取环境代理，也不回退；DNS 辅助线程最多两个，音频请求全部由协程处理。
    asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(max_workers=2))
    async with AsyncExitStack() as stack:
        client = await stack.enter_async_context(httpx.AsyncClient(
            http2=False, trust_env=False, proxy=None, follow_redirects=False,
            headers={"User-Agent": UA}, timeout=httpx.Timeout(60, connect=6),
            limits=httpx.Limits(max_connections=jobs, max_keepalive_connections=jobs)))
        source = AudioRanges(client, url)
        first = await source.get([(0, 15)])
        source.index_cache = open_mp4_index_cache(
            index_cache_dir, index_cache_bytes, mp4_identity(url, source.total, source.validator))
        if source.index_cache:
            stack.callback(source.index_cache.close)
        offset, ftyp, moov, chunks = 0, None, None, None
        for _ in range(128):
            header = first if offset == 0 else await source.get([(offset, min(offset + 15, source.total - 1))])
            if len(header) < 8:
                raise ZjuError("录播文件头截断")
            size, kind = struct.unpack_from(">I4s", header)
            width = 8
            if size == 1:
                if len(header) < 16:
                    raise ZjuError("录播扩展文件头截断")
                size, = struct.unpack_from(">Q", header, 8)
                width = 16
            elif size == 0:
                size = source.total - offset
            if size < width or offset + size > source.total:
                raise ZjuError("录播文件结构错误")
            if kind == b"ftyp":
                if size > 2**20:
                    raise ZjuError("MP4 文件类型索引过大")
                ftyp = await source.get([(offset, offset + size - 1)])
            elif kind == b"moov":
                if size > 128 * 2**20:
                    raise ZjuError("MP4 索引超过 128MiB")
                count = min(jobs, 8, max(1, size // 16))
                step = (size + count - 1) // count
                chunks = [None] * count
                if source.index_cache:
                    source.index_cache.capture((offset, offset + size), offset, header)

                async def grab_index(i):
                    start = offset + i * step
                    chunks[i] = await source.get([(start, min(start + step - 1, offset + size - 1))])
                    if source.index_cache:
                        source.index_cache.capture((offset, offset + size), start, chunks[i])

                await audio_parallel(range(count), count, grab_index)
                moov = b"".join(chunks)[width:]
            if ftyp is not None and moov is not None:
                break
            offset += size
            if offset >= source.total:
                break
        if ftyp is None or moov is None:
            raise ZjuError("录播不是可索引的 MP4（不支持 HLS）")
        index = AudioIndex(ftyp, moov, source.total)
        del moov, chunks
        if limit and index.size > limit:
            raise TooBig(f"音频数据 {index.size / 2**20:.1f}MB")
        completed = 0
        started, next_report = time.monotonic(), 0
        received_before = source.received
        with raw.open("w+b") as f:
            f.truncate(index.size)

            async def grab_audio(group):
                nonlocal completed, next_report
                first, last = group
                ranges = []
                for i in range(first, last):
                    a, b = index.starts[i], index.starts[i] + index.lengths[i] - 1
                    if ranges and a == ranges[-1][1] + 1:
                        ranges[-1] = ranges[-1][0], b
                    else:
                        ranges.append((a, b))
                data = await source.get(ranges)
                f.seek(index.positions[first])
                f.write(data)
                completed += len(data)
                percent = completed * 100 // index.size
                if percent >= next_report or completed == index.size:
                    speed = (source.received - received_before) / 2**20 / max(time.monotonic() - started, 0.001)
                    log(f"[音频进度] {percent}%  {completed / 2**20:.1f}/{index.size / 2**20:.1f}MB  {speed:.1f}MB/s")
                    next_report = percent + 10

            await audio_parallel(index.groups(), jobs, grab_audio)
        return index


def extract_audio(source: Path, dest: Path, raw: Path, limit: int | None = None):
    """跳过本地视频轨，直接复制音频样本，不调用外部工具。"""
    with source.open("rb") as video:
        total = os.fstat(video.fileno()).st_size
        offset, ftyp, moov = 0, None, None
        for _ in range(128):
            video.seek(offset)
            header = video.read(16)
            if len(header) < 8:
                raise ZjuError("本地录播文件头截断")
            size, kind = struct.unpack_from(">I4s", header)
            width = 8
            if size == 1:
                if len(header) < 16:
                    raise ZjuError("本地录播扩展文件头截断")
                size, = struct.unpack_from(">Q", header, 8)
                width = 16
            elif size == 0:
                size = total - offset
            if size < width or offset + size > total:
                raise ZjuError("本地录播文件结构错误")
            if kind in (b"ftyp", b"moov"):
                maximum = 2**20 if kind == b"ftyp" else 128 * 2**20
                if size > maximum:
                    raise ZjuError("本地 MP4 索引过大")
                video.seek(offset)
                data = video.read(size)
                if len(data) != size:
                    raise ZjuError("本地录播索引截断")
                if kind == b"ftyp":
                    ftyp = data
                else:
                    moov = data[width:]
            if ftyp is not None and moov is not None:
                break
            offset += size
            if offset >= total:
                break
        if ftyp is None or moov is None:
            raise ZjuError("本地录播不是可索引的 MP4")
        index = AudioIndex(ftyp, moov, total)
        del moov, data
        if limit and index.size > limit:
            raise TooBig(f"音频数据 {index.size / 2**20:.1f}MB")
        # 连续读小窗口，避免数十万次小 seek/read；内存不随视频大小增加。
        window, window_start = memoryview(b""), 0
        with raw.open("wb") as audio:
            for start, length in zip(index.starts, index.lengths):
                while length:
                    if not (window_start <= start < window_start + len(window)):
                        video.seek(start)
                        window = memoryview(video.read(min(8 * 2**20, total - start)))
                        window_start = start
                        if not window:
                            raise ZjuError("本地录播音频数据截断")
                    count = min(length, window_start + len(window) - start)
                    audio.write(window[start - window_start:start - window_start + count])
                    start += count
                    length -= count
    index.assemble(raw, dest)


def make_audio(url: str | None, video: Path | None, dest: Path, jobs: int = 32,
               limit: int | None = None, *, index_cache_dir=_DEFAULT_INDEX_CACHE,
               index_cache_bytes=256 * 2**20):
    with download_lock(dest, "音频"), tempfile.TemporaryDirectory(prefix=".zju-audio-", dir=dest.parent) as temp:
        temp = Path(temp)
        raw, output = temp / "payload.bin", temp / "output.m4a"
        if video is None:
            if not url:
                raise ZjuError("没有可下载的录播")
            index = asyncio.run(download_audio_ranges(url, raw, jobs, limit,
                                index_cache_dir=index_cache_dir, index_cache_bytes=index_cache_bytes))
            index.assemble(raw, output)
        else:
            extract_audio(video, output, raw, limit)
        if limit and output.stat().st_size > limit:
            raise TooBig(f"音频 {output.stat().st_size / 2**20:.1f}MB")
        os.replace(output, dest)


def current_year(courses: list[dict]) -> list[dict]:
    """is_closed 学校常不关，靠 academic_year_id 取最新学年。"""
    latest = max((c.get("academic_year_id") or 0 for c in courses), default=0)
    return [c for c in courses if (c.get("academic_year_id") or 0) == latest and not c.get("is_closed")]


def local_time(iso: str | None, fmt: str = "%m-%d %H:%M") -> str:
    if not iso:
        return "时间未定"
    try:
        d = dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return iso
    if d.tzinfo is None:
        d = d.replace(tzinfo=CST)
    return d.astimezone().strftime(fmt)


def match_courses(courses: list[dict], keys: list[str], include_all: bool) -> list[dict]:
    if not keys:
        return courses if include_all else current_year(courses)
    out = []
    for c in courses:
        for k in keys:
            if str(c["id"]) == k or k.lower() in c["name"].lower():
                out.append(c)
                break
    return out


ACT_TYPES = {
    "material": "课件", "online_video": "视频", "homework": "作业", "forum": "讨论", "exam": "测验",
    "page": "网页", "web_link": "连结", "questionnaire": "问卷", "classroom": "课堂互动",
    "lesson": "直播", "vocabulary": "单字", "survey": "调查", "chatroom": "聊天室",
}


def html_to_text(s: str | None) -> str:
    def link(m):  # 連結文字 ≠ 網址時兩個都留；網址前後補空白，免得黏在中文上
        href, text = html.unescape(m.group(1)), re.sub(r"<[^>]+>", "", m.group(2)).strip()
        return f" {href} " if not text or html.unescape(text) == href else f"{text} ({href})"
    s = re.sub(r"(?is)<a\b[^>]*?href=\"([^\"]+)\"[^>]*>(.*?)</a>", link, s or "")
    s = re.sub(r"(?i)<img\b[^>]*?src=\"([^\"]+)\"[^>]*>", lambda m: f"[图 {urljoin(LMS, html.unescape(m.group(1)))}]", s)
    s = re.sub(r"(?i)<li\b[^>]*>", "- ", s)
    s = re.sub(r"(?i)</t[dh]>", "\t", s)
    s = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>|</tr>|</h\d>", "\n", s)
    s = html.unescape(re.sub(r"<[^>]+>", "", s))
    s = re.sub(r"[ \t]+\n", "\n", re.sub(r"(?m)^- +", "- ", s))
    return re.sub(r"\n{3,}", "\n\n", s).strip()


def doc_to_text(data: bytes) -> str | None:
    """附件位元組 → 純文字；依檔頭判格式（預覽來源可能把 docx 換成 PDF）。不支援回 None。"""
    if data[:4] == b"PK\x03\x04":
        import zipfile, io
        z = zipfile.ZipFile(io.BytesIO(data))
        names = z.namelist()
        if "word/document.xml" in names:
            parts = ["word/document.xml"]
            para, tag = r"</w:p>", r"<(?:w|m):t[^>]*>([^<]*)</(?:w|m):t>"  # m:t = 公式裡的字
        else:
            parts = sorted((n for n in names if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)),
                           key=lambda n: int(re.search(r"\d+", n.rsplit("/", 1)[1]).group()))
            para, tag = r"</a:p>", r"<a:t>([^<]*)</a:t>"
        if not parts:
            return None
        out = []
        for i, n in enumerate(parts):
            xml = z.read(n).decode("utf-8", "replace")
            lines = ["".join(re.findall(tag, p_)) + " [图]" * len(re.findall(r"<w:drawing>|<w:pict>", p_))
                     for p_ in re.split(para, xml)]  # 公式常是貼圖，標出來免得以為漏字
            txt = "\n".join(l for l in lines if l.strip())
            out.append(f"--- 第 {i + 1} 页 ---\n{txt}" if len(parts) > 1 else txt)
        return html.unescape("\n".join(out)).strip()
    tool = None
    if data[:5] == b"%PDF-" and shutil.which("pdftotext"):
        tool = ["pdftotext", "-layout", "{f}", "-"]
    elif data[:4] == b"\xd0\xcf\x11\xe0" and shutil.which("textutil"):  # 舊版 .doc，macOS 內建
        tool = ["textutil", "-convert", "txt", "-stdout", "{f}"]
    elif data[:5] == b"%PDF-" or data[:4] == b"\xd0\xcf\x11\xe0":
        return None
    else:
        try:
            return data.decode("utf-8").strip()
        except UnicodeDecodeError:
            return None
    with tempfile.NamedTemporaryFile(suffix=".bin") as f:
        f.write(data)
        f.flush()
        r = subprocess.run([f.name if x == "{f}" else x for x in tool], capture_output=True)
    return r.stdout.decode("utf-8", "replace").strip() if r.returncode == 0 else None


def text_to_html(s: str) -> str:
    """纯文字 → 段落 HTML（空行分段、单换行 <br>），网页编辑器存的也是这种格式。"""
    paras = [p for p in re.split(r"\n\s*\n", s.strip()) if p.strip()]
    return "".join("<p>" + html.escape(p.strip()).replace("\n", "<br>") + "</p>" for p in paras)


def act_status(a: dict) -> str:
    if a.get("is_closed"):
        return "已关闭"
    if a.get("is_started") is False:
        return "未开始"
    end = a.get("end_time")
    if end and dt.datetime.fromisoformat(end.replace("Z", "+00:00")) < dt.datetime.now(dt.timezone.utc):
        return "已截止"
    return "进行中"


def read_body(a) -> str:
    """--body 文字或 --body-file 文件（- = stdin）。"""
    if a.body_file:
        return sys.stdin.read() if a.body_file == "-" else Path(a.body_file).read_text(encoding="utf-8")
    return a.body or ""


def upload_all(z: "Zju", files: list[str] | None) -> list[int]:
    ids = []
    for f in files or []:
        p = Path(f).expanduser()
        if not p.is_file():
            raise ZjuError(f"找不到文件：{p}")
        u = z.upload_file(p)
        log(f"[上传] {p.name} → upload {u['id']}")
        ids.append(u["id"])
    return ids


def fmt_ts(sec: float, srt=True) -> str:
    # 先统一量化为整数毫秒再进位，避免 61.12 被二进制浮点截成 119 毫秒。
    milliseconds = int((Decimal(str(sec)) * 1000).to_integral_value(rounding=ROUND_HALF_UP)) if srt else 0
    whole, ms = divmod(milliseconds, 1000) if srt else (int(float(sec)), 0)
    h, rem = divmod(whole, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}" if srt else f"{h:02d}:{m:02d}:{s:02d}"


def render_transcript(items: list[dict], fmt: str, title: str) -> str:
    lines = []
    if fmt == "srt":
        for i, c in enumerate(items, 1):
            lines += [str(i), f"{fmt_ts(c.get('BeginSec', 0))} --> {fmt_ts(c.get('EndSec', 0))}", c.get("Text", ""), ""]
    elif fmt == "md":
        lines.append(f"# {title}\n")
        for c in items:
            begin = fmt_ts(c.get("BeginSec", 0)).replace(",", ".")
            end = fmt_ts(c.get("EndSec", 0)).replace(",", ".")
            lines.append(f"**[{begin} → {end}]** {c.get('Text', '')}  ")
    else:
        for c in items:
            lines.append(f"[{fmt_ts(c.get('BeginSec', 0), False)}] {c.get('Text', '')}")
    return "\n".join(lines) + "\n"


def parse_video_catalogue(items: list[dict]) -> dict[int, list[str]]:
    """依 sub_id 取回放网址；url 可能是字串或多段录影的列表。"""
    result: dict[int, list[str]] = {}
    for item in items:
        if not isinstance(item, dict):
            raise ZjuError("录播目录项目不是物件")
        try:
            sid = int(item["sub_id"])
            content = item.get("content") or {}
            if isinstance(content, str):
                content = json.loads(content)
            if not isinstance(content, dict):
                raise ValueError("content 不是物件")
            playback = content.get("playback") or {}
            raw = (playback.get("url") if isinstance(playback, dict) else None) or content.get("url") or []
            urls = [raw] if isinstance(raw, str) else raw
            if not isinstance(urls, list):
                raise ValueError("url 不是字串或列表")
            valid = []
            for u in urls:
                if not isinstance(u, str) or not u.strip():
                    continue
                u = secure_url(u.strip())
                if urlparse(u).scheme not in ("http", "https") or not urlparse(u).hostname:
                    raise ValueError("url 不是 HTTP(S) 网址")
                if u not in valid:
                    valid.append(u)
            existing = result.setdefault(sid, [])
            existing.extend(u for u in valid if u not in existing)
        except (KeyError, TypeError, ValueError) as e:
            raise ZjuError(f"录播目录项目解析失败 sub_id={item.get('sub_id', '?')}：{e}") from e
    return result


def images_to_pdf(paths: list[Path], pdf: Path):
    import img2pdf
    from PIL import Image

    tmp = pdf.with_name(".part-" + pdf.name)

    def write(srcs):
        with open(tmp, "wb") as f:  # 直接串流进文件，不在记忆体组整份 PDF
            img2pdf.convert([str(x) for x in srcs], outputstream=f)

    try:
        try:
            write(paths)  # 智云截图几乎都是 JPEG：直接嵌入，不重新编码
        except Exception:
            fixed = []  # 有 alpha / 特殊格式的才转 JPEG
            for p in paths:
                with Image.open(p) as im:
                    if im.format == "JPEG" and im.mode in ("RGB", "L", "CMYK"):
                        fixed.append(p)
                        continue
                    q = p.with_suffix(".conv.jpg")
                    im.convert("RGB").save(q, quality=92)
                    fixed.append(q)
            write(fixed)
        os.replace(tmp, pdf)
    finally:
        tmp.unlink(missing_ok=True)


def dedup_slides(paths: list[Path], max_lost_cells: int = 2) -> tuple[list[Path], list[int | None]]:
    """智云截图去重。智云是对投影画面定时截图，同一页会因动画逐步出现、老师边讲边写、
    翻回前面而被截很多次。规则只有一条：一页的笔画若全都还在后面那页（或之前留下的某页）里，
    它就是多余的——所以连续的一串只留最后、最完整的一张，注记不会丢。

    「笔画」= 跟 15×15 邻域中位数差很多的像素，大片纯色（白底、黑底、视频画面）不算；
    八成以上的页都有的（底图纹理、黑边、页脚）也不算。「全都还在」= 消失的笔画没有聚成块：
    有 4 个以上笔画像素消失的 8×8 格不超过 max_lost_cells 个（JPEG 杂讯零星，真的少了东西会成块；
    再高就抓不到视频里又细又淡的线）。
    在三堂课（白底英文、底图＋手写、黑底教学视频混文件总管）逐页核对过，没有误删。
    """
    import numpy as np
    from PIL import Image, ImageFilter

    T, W, H = 40, 512, 288  # 灰阶门槛；解析度再低，细的手写笔迹就糊掉看不见了
    if len(paths) < 2:
        return list(paths), list(range(1, len(paths) + 1))

    def prep(p):
        with Image.open(p) as im:
            g = im.convert("L").resize((W, H), Image.BOX)
        f = np.asarray(g, dtype=np.int16)
        return f, strokes(f)

    def strokes(f):
        bg = Image.fromarray(f.astype(np.uint8)).filter(ImageFilter.MedianFilter(15))
        return np.abs(f - np.asarray(bg, dtype=np.int16)) > T

    frames, raw = zip(*map(prep, paths))
    frames = np.stack(frames)
    med = np.median(frames, axis=0).astype(np.int16)
    # 版面 = 八成以上的页在那里都一样的笔画；只看中位数的话，一张讲了半堂课的投视频会被当成版面
    layout = strokes(med) & ((np.abs(frames - med) <= T).sum(axis=0) >= 0.8 * len(frames))
    masks = [m & ~(layout & (np.abs(f - med) <= T)) for f, m in zip(frames, raw)]

    def lost(i, js):  # i 的笔画在 js 各页消失成块的格数
        gone = masks[i] & (np.abs(frames[i] - frames[js]) > T)
        return (gone.reshape(len(js), H // 8, 8, W // 8, 8).sum(axis=(2, 4)) >= 4).sum(axis=(1, 2))

    keep: list[int] = []
    mapping: list[int | None] = [None] * len(paths)
    for j in range(len(frames)):
        if frames[j].std() < 3:  # 全黑 / 全白过场
            continue
        if keep and lost(keep[-1], [j])[0] <= max_lost_cells:
            mapping[j] = len(keep)
            keep[-1] = j  # 前一张是这张的子集（动画没跑完、还没写完）→ 换成较完整的这张
            continue
        # 翻回讲过的页、擦掉注记的干净版；近乎空白的页什么都「包含得住」，不拿来比
        if keep and masks[j].sum() >= 400:
            matches = np.flatnonzero(lost(j, keep) <= max_lost_cells)
            if len(matches):
                mapping[j] = int(matches[0]) + 1
                continue
        keep.append(j)
        mapping[j] = len(keep)
    # 全为空白时沿用原有 PDF 行为：保留所有截图，因此它们仍有对应页面。
    if not keep:
        return list(paths), list(range(1, len(paths) + 1))
    return [paths[k] for k in keep], mapping


def tracked_courses() -> dict[str, str]:
    """config.json 的 tracked_courses：{智雲 course_id: 顯示名}。
    申請聽課核准的課不在 get-my-course-day（只回課表），智雲也沒有「我的申請」清單 API，只能自己記。"""
    return load_config().get("tracked_courses") or {}


def range_subs(z: Zju, end: dt.date, days: int) -> list[tuple[dt.date, dict]]:
    """end 往前 days 天：課表的課＋追蹤課程當天的堂次，依 sub_id 去重。"""
    out, seen = [], set()
    for i in range(days):
        d = end - dt.timedelta(days=i)
        for s in z.day_subs(d):
            if s["sub_id"] not in seen:
                seen.add(s["sub_id"])
                out.append((d, s))
    start = end - dt.timedelta(days=days - 1)
    for cid, name in tracked_courses().items():
        try:
            subs = z.course_subs(int(cid))
        except Exception as e:  # 一門壞掉不拖垮課表的課
            log(f"[失败] 追踪课程 {cid} {name}: {e}")
            continue
        for s in subs:
            if s["day"] and start <= s["day"] <= end and s["sub_id"] not in seen:
                seen.add(s["sub_id"])
                out.append((s["day"], s))
    out.sort(key=lambda x: x[0], reverse=True)
    return out


def resolve_subs(z: Zju, a) -> list[dict]:
    if a.course:
        subs = z.course_subs(a.course)
        if a.sub:
            missing = set(a.sub) - {s["sub_id"] for s in subs}
            if missing:
                raise ZjuError(f"课程中没有堂次：{sorted(missing)}")
            subs = [s for s in subs if s["sub_id"] in a.sub]
        return subs
    return [s for _, s in range_subs(z, dt.date.today(), a.days or 1)]


# ---------------- commands ----------------

def cmd_login(a):
    cfg = load_config()
    user = a.username or input(f"学号 [{cfg.get('username', '')}]: ").strip() or cfg.get("username")
    if not user:
        raise ZjuError("没有学号")
    if not keychain_get(user) or a.reset:
        print("输入统一身份认证密码（存进系统凭据库）：")
        keychain_set_interactive(user)
    cfg["username"] = user
    save_config(cfg)
    if a.reset:
        COOKIE_FILE.unlink(missing_ok=True)
    z = Zju()
    z.login(*get_credentials())
    ok = z._token(silent=True) is not None
    print(f"登录成功：{user}；智云课堂 token {'OK' if ok else '缺（classroom 指令可能失败）'}")


def cmd_courses(a):
    if getattr(a, "local", False):
        return cmd_local_courses(a)
    z = Zju()
    cs = z.courses()
    cs = cs if a.all else current_year(cs)
    if a.json:
        print(json.dumps(cs, ensure_ascii=False, indent=1))
        return
    sem = z.semesters()
    for c in cs:
        teachers = ",".join(i["name"] for i in c.get("instructors") or [])
        print(f"{c['id']}\t{sem.get(c.get('semester_id'), '-')}\t{c['name']}\t{teachers}")


def cmd_sync(a):
    ids = sync_ids(a, "lms")
    z = Zju()
    available = z.courses()
    by_id = {c["id"]: c for c in available}
    if ids and any(cid not in by_id for cid in ids):
        raise ZjuError("没有符合的学在浙大课程；请用 lms courses --all 查看 ID")
    courses = available if a.all else [by_id[cid] for cid in ids]
    failed = 0
    for course in courses:
        try:
            with course_writer(a, "lms", course["id"], course["name"]) as directory:
                failed += sync_lms_course(z, a, directory, course)
        except (OSError, ValueError, ZjuError) as error:
            failed += 1
            log(f"[失败] {course['name']}: {error}")
    log(f"{'预览' if a.dry_run else '同步完成'}：{len(courses)} 门课程、{failed} 项失败")
    if failed:
        sys.exit(2)


def sync_lms_course(z, a, directory, course):
    state = DownloadState(directory)
    jobs, seen = [], set()
    failed = 0
    limit = a.max_size * 2**20 if a.max_size else None
    for activity, upload in z.uploads(course["id"]):
        uid, rid = upload["id"], upload.get("reference_id") or upload["id"]
        key = f"upload:{uid}"
        if key in seen:
            continue
        seen.add(key)
        rec = state.get(key)
        name = safe_name(upload.get("name") or str(uid))
        filename = f"{Path(name).stem[:110]} ({uid}){Path(name).suffix}"
        dest = context_path(directory, rec["path"]) if rec else directory / "附件" / filename
        if rec and rec["status"] == "complete" and dest.is_file() and dest.stat().st_size == rec["size"]:
            log(f"[跳过] {dest}")
            continue
        if not a.videos and Path(name).suffix.lower() in MEDIA_EXT:
            log(f"[跳过音视频] {name}（使用 --videos）")
            continue
        if limit and (upload.get("size") or 0) > limit:
            log(f"[太大跳过] {name}")
            continue
        if a.dry_run:
            print(f"[会下载] {dest}")
            continue
        state.claim(key, dest, kind="attachment", upload_id=uid, reference_id=rid,
                    activity=activity.get("title", ""))
        jobs.append((key, uid, rid, dest, activity))

    def fetch(job):
        key, uid, rid, dest, activity = job
        response, source = z.upload_response(uid, rid, activity)
        # 存储路径固定；远端 Content-Disposition 不参与本地身份。
        response.headers.pop("Content-Disposition", None)
        def prepare(target):
            state.claim(key, target, kind="attachment", upload_id=uid, reference_id=rid,
                        activity=activity.get("title", ""))
        return stream_to(response, dest, limit, prepare=prepare), source

    with ThreadPoolExecutor(max_workers=a.jobs) as pool:
        futures = {pool.submit(fetch, job): job for job in jobs}
        for future in as_completed(futures):
            key, uid, rid, dest, activity = futures[future]
            try:
                path, source = future.result()
                state.finish(key, path, source=source)
                print(f"[下载] {path}")
            except TooBig as error:
                log(f"[太大跳过] {dest.name}: {error}")
            except Exception as error:
                if activity.get("is_started") is False and 403 in getattr(error, "codes", ()):
                    log(f"[未开放] {dest.name}")
                else:
                    failed += 1
                    log(f"[失败] {dest.name}: {error}")
    if not a.dry_run:
        data = read_course(directory, "lms", course["id"])
        data.update(name=course["name"], teacher=[i.get("name") for i in course.get("instructors", [])],
                    semester_id=course.get("semester_id"), academic_year_id=course.get("academic_year_id"),
                    updated_at=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"))
        data["files"] = []
        for rec in state.data.values():
            path = context_path(directory, rec["path"])
            if rec["status"] == "complete" and path.is_file() and path.stat().st_size == rec["size"]:
                data["files"].append({k: rec[k] for k in ("path", "upload_id", "reference_id", "activity") if k in rec})
        atomic_text(directory / "course.json", json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    return failed


def cmd_todo(a):
    z = Zju()
    ts = z.todos()
    if a.json:
        print(json.dumps(ts, ensure_ascii=False, indent=1))
        return
    for t in sorted(ts, key=lambda t: t.get("end_time") or ""):
        end = local_time(t.get("end_time"), "%Y-%m-%d %H:%M")  # API 给 UTC
        print(f"{end}\t{t.get('course_name', '')}\t{t.get('title', '')}\t{t.get('type', '')}")


def cmd_activities(a):
    z = Zju()
    courses = match_courses(z.courses(), a.course, a.all)
    if not courses:
        raise ZjuError("没有符合的课程（用 courses --all 看 id）")
    rows = []
    for c in courses:
        for x in z.activities(c["id"]):
            if a.type and x.get("type") not in a.type:
                continue
            rows.append((c, x))
    if a.json:
        print(json.dumps([dict(x, course_name=c["name"]) for c, x in rows], ensure_ascii=False, indent=1))
        return
    for c, x in rows:
        t = x.get("type", "")
        end = local_time(x.get("end_time"), "%Y-%m-%d %H:%M") if x.get("end_time") else "-"
        print(f"{x['id']}\t{c['name']}\t{ACT_TYPES.get(t, t)}\t{act_status(x)}\t{end}\t{x.get('title', '')}")


def cmd_show(a):
    z = Zju()
    x = z.activity(a.activity)
    t = x.get("type", "")
    d = x.get("data") or {}
    print(f"[{ACT_TYPES.get(t, t)}] {x.get('title')}  (id {x['id']}, 课程 {x.get('course_id')})")
    print(f"状态：{act_status(x)}　开始 {local_time(x.get('start_time'), '%Y-%m-%d %H:%M')}"
          f"　截止 {local_time(x.get('end_time'), '%Y-%m-%d %H:%M') if x.get('end_time') else '无'}")
    if x.get("completion_criterion"):
        print(f"完成条件：{x['completion_criterion']}")
    desc = html_to_text(d.get("description") or x.get("description"))
    if desc:
        print(f"\n{desc}\n")
    for u in x.get("uploads") or []:
        print(f"附件：{u.get('name')}  (upload {u.get('id')})")
        if a.read:
            try:
                r, _ = z.upload_response(u["id"], u.get("reference_id") or u["id"], x)
                txt = doc_to_text(r.content)
            except ZjuError as e:
                txt = f"（{e}）"
            print(f"\n{txt or '（这种格式读不出文字，用 sync 下载后打开）'}\n")
    if x.get("uploads") and not a.read and not desc:
        print("（说明在附件里：加 --read 直接输出附件内容）")
    if t == "web_link" and d.get("link"):
        print(f"连结：{d['link']}")
    if t == "homework":
        s = z.my_submission(x["id"])
        if s.get("created_at"):
            kind = "草稿" if s.get("is_draft") else "已提交"
            print(f"我的提交：{kind} {local_time(s.get('created_at'), '%Y-%m-%d %H:%M')}"
                  f"　分数 {s.get('score') if s.get('score') is not None else '未评'}")
            for u in s.get("uploads") or []:
                print(f"  - {u.get('name')}")
            if s.get("comment"):
                print("  " + html_to_text(s["comment"]).replace("\n", "\n  "))
        else:
            print("我的提交：尚未提交")
    elif t == "forum":
        ts = z.topics(z.forum_category(x["id"]))
        print(f"讨论帖 {len(ts)} 则（forum list {x['id']} 看全部）")
        mine, replies = z.my_forum_posts(x["id"])
        if not mine and not replies:
            print("我的参与：尚未发帖或回帖")
        else:
            print(f"我的参与：发帖 {len(mine)}、回帖 {len(replies)}")
            for p in mine:
                print(f"  - 发帖 {local_time(p.get('created_at'), '%Y-%m-%d %H:%M')}"
                      f"　{p.get('title', '')}  (topic {p['id']})")
            for tp, r in replies:
                print(f"  - 回帖 {local_time(r.get('created_at'), '%Y-%m-%d %H:%M')}"
                      f"　→ {tp.get('title', '')}  (topic {tp['id']})")


def cmd_forum(a):
    z = Zju()
    if a.action == "list":
        if a.mine:
            ts, replies = z.my_forum_posts(a.id)
        else:
            ts, replies = z.topics(z.forum_category(a.id)), []
        for t in ts:
            by = t.get("created_by") or {}
            print(f"{t['id']}\t{local_time(t.get('created_at'))}\t{by.get('name', '')}\t"
                  f"回复 {t.get('reply_count', 0)}\t{t.get('title', '')}")
            if a.full:
                print("  " + html_to_text(t.get("content")).replace("\n", "\n  "))
        for tp, r in replies:
            print(f"{tp['id']}\t{local_time(r.get('created_at'))}\t(回帖)\t\t→ {tp.get('title', '')}")
            if a.full:
                print("  " + html_to_text(r.get("content")).replace("\n", "\n  "))
    elif a.action == "read":
        t = z.topic(a.id)
        by = t.get("created_by") or {}
        print(f"# {t.get('title')}\n{by.get('name', '')}  {local_time(t.get('created_at'), '%Y-%m-%d %H:%M')}\n")
        print(html_to_text(t.get("content")))
        for u in t.get("uploads") or []:
            print(f"附件：{u.get('name')}")

        def show(rs, depth):
            for r in rs or []:
                rb = r.get("created_by") or {}
                pad = "  " * depth
                print(f"\n{pad}↳ {rb.get('name', '')}  {local_time(r.get('created_at'))}")
                print(pad + html_to_text(r.get("content")).replace("\n", "\n" + pad))
                show(r.get("replies"), depth + 1)
        show(t.get("replies"), 1)
    else:
        body = read_body(a)
        if not body.strip():
            raise ZjuError("内容是空的：用 --body 或 --body-file")
        content = body if a.html else text_to_html(body)
        if a.action == "post":
            if not a.title:
                raise ZjuError("发帖要 --title")
            cat = z.forum_category(a.id)
            ids = upload_all(z, a.attach)
            t = z.create_topic(cat, a.title, content, ids)
            print(f"[已发帖] topic {t['id']}：{t.get('title')}")
        else:
            ids = upload_all(z, a.attach)
            r = z.reply_topic(a.id, content, ids)
            print(f"[已回帖] reply {r.get('id')} → topic {a.id}")


def cmd_upload(a):
    z = Zju()
    for f in a.files:
        p = Path(f).expanduser()
        if not p.is_file():
            raise ZjuError(f"找不到文件：{p}")
        u = z.upload_file(p)
        print(f"{u['id']}\t{p.name}")


def cmd_submit(a):
    z = Zju()
    x = z.activity(a.activity)
    if x.get("type") != "homework":
        raise ZjuError(f"活动 {a.activity} 是 {x.get('type')}，不是作业")
    status = act_status(x)
    if status != "进行中" and not x.get("is_resubmit_open"):
        raise ZjuError(f"作业「{x.get('title')}」{status}，网页上也交不了")
    comment = read_body(a)
    if not comment.strip() and not a.file and not a.upload_id:
        raise ZjuError("没有东西可交：给 --file、--upload-id 或 --body")
    prev = z.my_submission(x["id"])
    draft_id = prev.get("id") if prev.get("is_draft") else None
    kind = "存草稿" if a.draft else "正式提交"
    print(f"{kind}「{x.get('title')}」（截止 {local_time(x.get('end_time'), '%Y-%m-%d %H:%M')}）")
    for f in a.file or []:
        print(f"  文件：{f}")
    if comment.strip():
        print(f"  文字：{comment.strip()[:80]}{'…' if len(comment.strip()) > 80 else ''}")
    if prev.get("created_at") and not prev.get("is_draft"):
        print("  注意：已经交过一次，这次会新增一份提交")
    if not a.yes:
        if not sys.stdin.isatty():
            raise ZjuError("非互动环境要加 --yes 才会真的送出")
        try:
            ok = input("确定送出？[y/N] ").strip().lower() == "y"
        except EOFError:  # Windows 的 NUL 也算 tty，读不到就当取消
            ok = False
        if not ok:
            log("已取消（非互动环境加 --yes）")
            return
    ids = upload_all(z, a.file) + (a.upload_id or [])
    content = comment if a.html else text_to_html(comment) if comment.strip() else ""
    s = z.submit(x["id"], content, ids, a.draft, (x.get("data") or {}).get("mode") or "normal", draft_id)
    print(f"[{kind}] submission {s.get('id', '')} ✓")


def context_json(path: Path):
    def invalid_constant(_):
        raise ValueError("非有限 JSON 数值")
    return json.loads(path.read_text(encoding="utf-8"), parse_constant=invalid_constant)


def context_path(directory: Path, value) -> Path:
    """元数据只允许课程目录内的相对文件，包括解析后的符号链接目标。"""
    if (not isinstance(value, str) or not value or any(ord(c) < 32 for c in value)
            or PureWindowsPath(value).drive or value.startswith(("/", "\\"))
            or re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", value)):
        raise ValueError("资料路径必须是课程内相对路径")
    path = Path(value.replace("\\", "/"))
    if ".." in path.parts:
        raise ValueError("资料路径越界")
    target = directory / path
    target.resolve().relative_to(directory.resolve())
    return target


def context_course(directory: Path, cid: int, sid: int):
    warnings = []
    course = read_course(directory, "classroom", cid)
    lessons = course.get("lessons")
    if not isinstance(lessons, list) or any(not isinstance(s, dict) or type(s.get("sub_id")) is not int for s in lessons):
        raise ZjuError("course.json 的 lessons 格式无效")
    selected = [s for s in lessons if s["sub_id"] == sid]
    if len(selected) != 1:
        raise ZjuError(f"course.json 中堂次 {sid} {'不存在' if not selected else '重复，无法确定身份'}")
    if not isinstance(selected[0].get("files"), dict):
        raise ZjuError("course.json 的堂次 files 格式无效")
    if course.get("lessons_fetch_status") == "error":
        warnings.append(dict(code="stale_course_metadata", message="上次堂次查询失败，保留的本地信息可能不完整"))
    return directory, course, selected[0], warnings


def context_files(directory, lesson, kind, cid, sid):
    entries = lesson["files"].get(kind, [])
    if not isinstance(entries, list):
        return [dict(path=None, status="invalid", reason="files 中的资料列表格式无效")]
    refs = []
    for entry in entries:
        ref = dict(path=None, status="invalid")
        refs.append(ref)
        if not isinstance(entry, dict):
            ref["reason"] = "文件引用不是对象"
            continue
        try:
            path = context_path(directory, entry.get("path"))
        except (OSError, ValueError, RuntimeError):
            # 不回显不可信路径，尤其是 URL 和越界路径。
            ref.update(status="unsafe_path", reason="路径无效或超出课程目录，未访问")
            continue
        ref["path"] = path.relative_to(directory).as_posix()
        if kind in ("audio", "recordings"):
            ref["part"] = entry.get("part") if type(entry.get("part")) is int and entry["part"] > 0 else None
            if ref["part"] is None:
                ref["reason"] = "缺少有效分段编号"
                continue
        elif kind == "transcripts":
            ref["format"] = entry.get("format") if entry.get("format") in ("md", "txt", "srt") else path.suffix[1:].lower()
        extension = {"ppt_events": ".json", "ppt_pdfs": ".pdf", "audio": ".m4a", "recordings": ".mp4"}.get(kind)
        if (path.name.startswith(".") or (extension and path.suffix.lower() != extension)
                or (kind == "transcripts" and path.suffix.lower() not in (".md", ".txt", ".srt"))):
            ref.update(status="invalid", reason="不是支持的最终资料文件")
            continue
        try:
            if not path.exists():
                ref.update(status="missing", reason="元数据有引用，但本地文件缺失")
            elif not path.is_file():
                ref.update(status="unavailable", reason="不是普通文件")
            elif not valid_course_material(path, kind, cid, sid, None):
                ref.update(status="invalid", reason="空文件、格式损坏或身份不符")
            else:
                ref["status"] = "ok"
        except (OSError, ValueError):
            ref.update(status="unavailable", reason="文件不可读或格式无效")
    return refs


def context_status(refs, found, empty="no_matches"):
    if not refs:
        return "missing"
    good = any(r["status"] == "ok" for r in refs)
    if any(r["status"] != "ok" for r in refs):
        return "partial" if good else "unavailable"
    return "ok" if found else empty


def context_transcript(directory, refs, start, end, warnings):
    segments = []
    for ref in refs:
        if ref["status"] != "ok":
            continue
        if ref["format"] != "md":
            ref.update(search_status="unsupported_format", reason="保留文件引用；上下文检索仅解析 Markdown")
            continue
        issues, rows = {}, []
        def issue(code, line):
            count, lines = issues.setdefault(code, [0, []])
            issues[code][0] = count + 1
            if len(lines) < 5:
                lines.append(line)
        try:
            text = (directory / ref["path"]).read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            ref.update(status="unavailable", reason="Markdown 不可读")
            continue
        # 只解析行首时间标记，正文中的 Markdown、箭头、方括号均保持原样。
        for number, line in enumerate(text.splitlines(), 1):
            match = re.match(r"^\*\*\[([^\]]+)\]\*\*[ \t]?(.*)$", line)
            if not match:
                if rows:
                    rows[-1]["text"] += "\n" + line
                continue
            try:
                begin, sep, finish = match[1].partition("→")
                begin = parse_frame_time(begin.strip())
                finish = parse_frame_time(finish.strip()) if sep else None
            except argparse.ArgumentTypeError:
                issue("invalid_transcript_time", number)
                # 分隔符不能导致后续正文粘到上一条有效转写。
                rows.append(dict(line=number, invalid=True, text=""))
                continue
            flags = []
            if finish is None:
                flags.append("end_time_unknown")
            elif finish < begin:
                flags.append("reversed_interval")
            elif finish == begin:
                flags.append("zero_duration")
            elif finish - begin > 300:
                flags.append("long_interval")
            rows.append(dict(file=ref["path"], line=number, begin_sec=begin, end_sec=finish,
                             text=match[2].removesuffix("  "), flags=flags))
        valid = [r for r in rows if not r.get("invalid")]
        maximum, previous = -1, None
        for row in sorted(valid, key=lambda r: (r["begin_sec"], r["line"])):
            finish = row["end_sec"]
            if row["begin_sec"] < maximum:
                row["flags"].append("overlap")
                if "overlap" not in previous["flags"]:
                    previous["flags"].append("overlap")
            if finish is not None and finish > maximum and finish >= row["begin_sec"]:
                maximum, previous = finish, row
        for row in valid:
            row["text"] = row["text"].rstrip("\n")
            for code in row["flags"]:
                issue(code, row["line"])
            finish = row["end_sec"]
            # 反向区间或缺少结束时间只按开始点匹配，不擅自修正区间。
            bound = finish if finish is not None and finish >= row["begin_sec"] else row["begin_sec"]
            if row["begin_sec"] <= end and bound >= start:
                segments.append(row)
        if not valid:
            ref.update(status="invalid", reason="没有可解析的 Markdown 时间段")
        for code, (count, lines) in issues.items():
            warnings.append(dict(code=code, file=ref["path"], count=count, lines=lines,
                                 message="转写保留原始时间；异常区间不代表整段持续讲话"))
    status = context_status(refs, segments)
    if refs and all(r.get("search_status") == "unsupported_format" for r in refs):
        status = "unsupported_format"
    return dict(status=status, files=refs, segments=segments)


def context_ppt(directory, event_refs, pdf_refs, cid, sid, start, end, warnings):
    candidates, untimed = [], []
    pdfs = {r["path"]: r for r in pdf_refs if r["path"] is not None}
    for ref in event_refs:
        if ref["status"] != "ok":
            continue
        try:
            data = context_json(directory / ref["path"])
            pdf = context_path(directory, (Path(ref["path"]).parent / data["pdf_file"]).as_posix())
            pdf_ref = pdfs.get(pdf.relative_to(directory).as_posix())
            if pdf_ref is None:
                raise ValueError("PDF 未在 course.json 中登记")
            pages = {}
            for page in data["pages"]:
                n, representative, indices = page["pdf_page"], page["representative_event_index"], page["event_indices"]
                if (type(n) is not int or n < 1 or n in pages or type(representative) is not int
                        or representative < 0 or not isinstance(indices, list)
                        or any(type(i) is not int or i < 0 for i in indices) or representative not in indices):
                    raise ValueError("无效 PDF 页映射")
                pages[n] = page
            events, seen = [], set()
            for raw in data["events"]:
                if not isinstance(raw, dict) or type(raw.get("event_index")) is not int or raw["event_index"] < 0 or raw["event_index"] in seen:
                    raise ValueError("无效或重复事件编号")
                index = raw["event_index"]
                seen.add(index)
                event = {k: raw.get(k) for k in ("event_index", "pdf_page", "representative_event_index", "relationship")}
                event.update(file=ref["path"], pdf_path=pdf_ref["path"], pdf_status=pdf_ref["status"], flags=[])
                sec = raw.get("created_sec")
                try:
                    if isinstance(sec, bool) or sec is None:
                        raise ValueError("时间未知")
                    sec = parse_frame_time(str(sec))
                except (ValueError, argparse.ArgumentTypeError):
                    sec = None
                    event["flags"].append("unknown_event_time")
                event["created_sec"] = sec
                page, rep, relation = event["pdf_page"], event["representative_event_index"], event["relationship"]
                mapping = pages.get(page) if type(page) is int else None
                if not ((page is None and rep is None and relation == "blank") or
                        (mapping and type(rep) is int and rep == mapping["representative_event_index"]
                         and index in mapping["event_indices"]
                         and relation == ("retained" if index == rep else "represented"))):
                    # 保留事件时间，但不能把不一致的页码交给调用者当成有效映射。
                    event.update(pdf_page=None, representative_event_index=None, relationship="invalid_mapping")
                    event["flags"].append("invalid_ppt_mapping")
                events.append(event)
            if any(not set(page["event_indices"]) <= seen for page in pages.values()):
                raise ValueError("PDF 页引用了不存在的事件")
        except (OSError, ValueError, TypeError, KeyError, RuntimeError):
            ref.update(status="invalid", reason="事件 JSON 或 PDF 映射无效，未使用")
            continue
        timed = [e for e in events if e["created_sec"] is not None]
        before = max((e["created_sec"] for e in timed if e["created_sec"] < start), default=None)
        after = min((e["created_sec"] for e in timed if e["created_sec"] > end), default=None)
        for event in events:
            sec = event["created_sec"]
            if sec is None:
                event["selection"] = "untimed"
                untimed.append(event)
            elif start <= sec <= end or sec == before or sec == after:
                event["selection"] = "window" if start <= sec <= end else "previous" if sec < start else "next"
                candidates.append(event)
        for flag in ("unknown_event_time", "invalid_ppt_mapping"):
            count = sum(flag in e["flags"] for e in events)
            if count:
                warnings.append(dict(code=flag, file=ref["path"], count=count,
                                     message="部分 PPT 事件时间或页码未知，未推断页面显示区间"))
    candidates.sort(key=lambda e: (e["created_sec"], e["file"], e["event_index"]))
    window_count = sum(e["selection"] == "window" for e in candidates)
    status = context_status(event_refs + pdf_refs, window_count,
                            "neighbors_only" if candidates else "untimed_only" if untimed else "no_matches")
    if not event_refs and pdf_refs and all(r["status"] == "ok" for r in pdf_refs):
        status = "missing_events"
    return dict(status=status, event_files=event_refs, pdf_files=pdf_refs,
                window_match_count=window_count, events=candidates, untimed_events=untimed)


def classroom_context(a):
    directory, a.course, _ = resolve_course(a, "classroom", a.course)
    if a.course < 1 or a.sub < 1:
        raise ZjuError("--course 和 --sub 必须是正整数")
    if not all(math.isfinite(x) and x >= 0 for x in (a.at, a.window)) or not math.isfinite(a.at + a.window):
        raise ZjuError("查询时间及窗口必须是有限非负秒数，且总和不能溢出")
    directory, course, lesson, warnings = context_course(directory, a.course, a.sub)
    # 避免 0.7 + 0.1 的浮点误差把恰好位于闭区间边界的事件排除。
    start = max(0., float(Decimal(str(a.at)) - Decimal(str(a.window))))
    end = float(Decimal(str(a.at)) + Decimal(str(a.window)))
    refs = {kind: context_files(directory, lesson, kind, a.course, a.sub) for kind in
            ("transcripts", "ppt_events", "ppt_pdfs", "audio", "recordings")}
    transcript = context_transcript(directory, refs["transcripts"], start, end, warnings)
    ppt = context_ppt(directory, refs["ppt_events"], refs["ppt_pdfs"], a.course, a.sub, start, end, warnings)
    media = {}
    for kind, target in (("audio", "audio"), ("recordings", "video")):
        for ref in refs[kind]:
            ref.update(duration_sec=None, time_range_sec=None)
        parts = [r.get("part") for r in refs[kind] if r.get("part") is not None]
        media[target] = dict(status=context_status(refs[kind], bool(parts)), parts=refs[kind],
                             classroom_time_mapping="unknown", duration_status="not_recorded")
        if len(parts) != len(set(parts)):
            warnings.append(dict(code="duplicate_media_part", media=target, message="同一分段有多个文件，未自动选择"))
    if any(r["status"] != "ok" for entries in refs.values() for r in entries):
        warnings.append(dict(code="materials_unavailable", message="部分资料缺失、不可用或格式不支持；详见各文件 status"))
    warnings.append(dict(code="uncalibrated_timeline", message="保留原始时间；转写、PPT 与媒体可能相差数秒或更多，候选窗口不保证覆盖真实内容"))
    warnings.append(dict(code="ppt_observations", message="PPT 事件是观测点，不是页面精确显示起止区间"))
    parts = sorted({r["part"] for kind in ("audio", "recordings") for r in refs[kind]
                    if r["status"] == "ok" and r.get("part") is not None})
    if len(parts) > 1:
        warnings.append(dict(code="multipart_timeline_unknown", message="存在多段媒体，不能由课堂时间或累计时长推定所属分段"))
    def identity(data, fields):
        return {k: data.get(k) if isinstance(data.get(k), (str, int)) and not isinstance(data.get(k), bool) else None for k in fields}
    return dict(schema_version=1, source="zju-classroom-local",
                course=identity(course, ("course_id", "name", "teacher", "term")),
                lesson=identity(lesson, ("sub_id", "name", "date", "lecturer", "availability")),
                location=dict(course_dir=str(directory), metadata="course.json", paths_relative_to="course_dir"),
                query=dict(at_sec=a.at, window_sec=a.window, start_sec=start, end_sec=end, alignment="unverified"),
                transcript=transcript, ppt=ppt, **media,
                recording_frame_hint=dict(course_id=a.course, sub_id=a.sub, known_local_parts=parts,
                    part_selection_required=len(parts) > 1, candidate_window_sec=[start, end],
                    alignment="unverified", remote_availability="unchecked", executed=False), warnings=warnings)


def cmd_classroom_context(a):
    result = classroom_context(a)
    if a.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        return
    print(f"[本地上下文] {result['course']['name']} ({a.course}) / {result['lesson']['name']} ({a.sub})")
    print(f"目录：{result['location']['course_dir']}")
    print(f"窗口：{result['query']['start_sec']:g}–{result['query']['end_sec']:g} 秒（时间轴未校准）")
    print(f"转写：{result['transcript']['status']}")
    for row in result["transcript"]["segments"]:
        print(f"  [{row['begin_sec']:g} → {row['end_sec']}] {row['text']}")
        if row["flags"]:
            print(f"    提示：{', '.join(row['flags'])}")
    print(f"PPT：{result['ppt']['status']}，另有 {len(result['ppt']['untimed_events'])} 个无时间事件")
    for event in result["ppt"]["events"]:
        print(f"  {event['created_sec']:g} 秒 / 事件 {event['event_index']} / PDF 页 {event['pdf_page']} / {event['relationship']} / {event['selection']}")
    for section, key in ((result["transcript"], "files"), (result["ppt"], "pdf_files"),
                         (result["ppt"], "event_files"), (result["audio"], "parts"), (result["video"], "parts")):
        for ref in section[key]:
            print(f"  [{ref['status']}] {ref['path'] or '无效路径'}" + (f" / 分段 {ref['part']}" if ref.get("part") else ""))
    for kind in ("audio", "video"):
        print(f"{kind}：{result[kind]['status']}；时长及课堂时间映射未知")
    for warning in result["warnings"]:
        print(f"[提示] {warning['code']}：{warning['message']}")


def cmd_classroom(a):
    if a.action == "courses" and getattr(a, "local", False):
        return cmd_local_courses(a)
    z = Zju() if a.action not in ("tracked", "rm") else None
    if a.action == "courses":
        courses = z.classroom_courses()
        if a.has_tasks:
            courses = [c for c in courses if c["task_count"] > 0]
        if a.json:
            print(json.dumps(courses, ensure_ascii=False, indent=1))
            return
        print("课程ID\t学期\t课程名称\t教师\t任务数")
        for c in courses:
            print(f"{c['course_id']}\t{c['term']}\t{c['title']}\t{c['teacher']}\t{c['task_count']}")
    elif a.action == "search":
        for c in z.classroom_search(a.arg or "", a.teacher or ""):
            print(f"{c.get('course_id')}\t{c.get('title')}\t{c.get('realname')}\t{c.get('term_name', '')}")
    elif a.action == "day":
        start = dt.date.fromisoformat(a.arg) if a.arg else dt.date.today()
        for d, s in range_subs(z, start, a.days or 1):
            print(f"{d}\t{s['course_id']}\t{s['sub_id']}\t{s['course_name']}\t{s['sub_name']}\t{s['lecturer']}")
    elif a.action in ("add", "rm"):
        ids = [a.arg, *a.more] if a.arg else []
        if not ids or not all(v.isdigit() for v in ids):
            raise ZjuError(f"用法：classroom {a.action} <course_id>...（course_id 用 classroom search 查）")
        cfg = load_config()
        tracked = cfg.setdefault("tracked_courses", {})
        for cid in ids:
            if a.action == "rm":
                name = tracked.pop(cid, None)
                print(f"[移除] {cid} {name}" if name else f"[未追踪] {cid}")
                continue
            subs = z.course_subs(int(cid))  # 順便驗證 id 存在
            name = f"{subs[0]['course_name']} {subs[0]['lecturer']}" if subs else cid
            tracked[cid] = name
            print(f"[追踪] {cid} {name}（{len(subs)} 堂）")
        save_config(cfg)
    elif a.action == "tracked":
        for cid, name in tracked_courses().items():
            print(f"{cid}\t{name}")


@contextmanager
def course_writer(a, platform, cid, name):
    if a.dry_run:
        directory, _, _ = resolve_course(a, platform, cid, name=name, writing=True)
        yield directory
        return
    # 资料库锁只保护定位及首次登记；不同课程的媒体传输不共用锁。
    with ExitStack() as locks:
        with ExitStack() as allocation:
            if not getattr(a, "course_dir", None):
                allocation.enter_context(download_lock(library_path(a, platform) / ".courses", "资料库登记", timeout=30))
            directory, _, _ = resolve_course(a, platform, cid, name=name, writing=True)
            context_path(directory, ".course.json.download.lock")
            locks.enter_context(download_lock(directory / "course.json", "课程", timeout=30))
            initialize_course(directory, platform, cid, name)
        yield directory


def sync_ids(a, platform):
    ids = list(dict.fromkeys(a.course))
    if a.all and (ids or a.course_dir):
        raise ZjuError("--all 不能与课程 ID 或 --course-dir 混用")
    if a.course_dir:
        if len(ids) > 1:
            raise ZjuError("--course-dir 只能用于一门课程")
        _, cid, _ = resolve_course(a, platform, ids[0] if ids else None, writing=True)
        return [cid]
    if not ids and not a.all:
        raise ZjuError("请选择课程 ID、已有 --course-dir，或显式 --all")
    return ids


def run_classroom_downloads(z, a, directory, subs, kinds):
    failed = 0
    with ThreadPoolExecutor(max_workers=a.jobs) as pool:
        if "transcript" in kinds:
            futures = {pool.submit(transcript_one, z, a, directory, s): s for s in subs}
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception as error:
                    failed += 1
                    log(f"[失败] 转录 {futures[future]['sub_name']}: {error}")
        if "ppt" in kinds:
            for sub in subs:
                try:
                    ppt_one(z, a, directory, sub, pool=pool)
                except Exception as error:
                    failed += 1
                    log(f"[失败] PPT {sub['sub_name']}: {error}")
        if "recording" in kinds:
            failed += recording_subs(z, a, directory, subs, pool=pool)
    if "recording-audio" in kinds:
        failed += audio_subs(z, a, directory, subs)
    return failed


def cmd_classroom_sync(a):
    ids = sync_ids(a, "classroom")
    z = Zju()
    rows = z.classroom_courses()
    by_id = {row["course_id"]: row for row in rows}
    courses = rows if a.all else [by_id.get(cid, dict(course_id=cid, title=str(cid), type="multi")) for cid in ids]
    failed = count = 0
    kinds = {"ppt", "transcript"}
    if a.recording:
        kinds.add("recording")
    if a.recording_audio:
        kinds.add("recording-audio")
    for course in courses:
        if course["type"] != "multi":
            log(f"[跳过] {course['title']}：不支持的课程类型 {course['type']}")
            continue
        subs = None
        try:
            subs = z.course_subs(course["course_id"])
            if subs:
                course = dict(course, title=subs[0]["course_name"])
            count += len(subs)
        except Exception as error:
            failed += 1
            log(f"[失败] 堂次查询 {course['course_id']}: {error}")
        try:
            with course_writer(a, "classroom", course["course_id"], course["title"]) as directory:
                if subs is not None:
                    failed += run_classroom_downloads(z, a, directory, subs, kinds)
                if not a.dry_run:
                    write_course_metadata(directory, course, subs)
        except (ZjuError, OSError, ValueError) as error:
            failed += 1
            log(f"[失败] 课程 {course['course_id']}: {error}")
    log(f"{'预览' if a.dry_run else '同步完成'}：{len(courses)} 门课程、{count} 堂课、{failed} 项失败")
    if failed:
        sys.exit(2)


def cmd_classroom_material(a):
    if a.course_dir:
        _, a.course, _ = resolve_course(a, "classroom", a.course, writing=True)
    z = Zju()
    subs = resolve_subs(z, a)
    groups = {}
    for sub in subs:
        groups.setdefault(sub["course_id"], []).append(sub)
    if not groups and a.course:
        groups[a.course] = []
    failed = 0
    for cid, lessons in groups.items():
        name = lessons[0]["course_name"] if lessons else str(cid)
        try:
            with course_writer(a, "classroom", cid, name) as directory:
                failed += run_classroom_downloads(z, a, directory, lessons, {a.action})
                if not a.dry_run:
                    old = read_course(directory)
                    course = dict(course_id=cid, title=lessons[0]["course_name"] if lessons else old.get("name"))
                    write_course_metadata(directory, course, lessons)
        except (ZjuError, OSError, ValueError) as error:
            failed += 1
            log(f"[失败] 课程 {cid}: {error}")
    if failed:
        sys.exit(2)


def cmd_local_courses(a):
    rows = [dict({k: data.get(k) for k in ("platform", "course_id", "name", "teacher", "term", "updated_at")},
                 course_dir=str(path)) for path, data in local_courses(library_path(a, a.platform), a.platform)]
    if a.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2, allow_nan=False))
    else:
        for row in rows:
            print(f"{row['course_id']}\t{row.get('name', '')}\t{row['course_dir']}")


def cmd_classroom_subs(a):
    if a.local or a.course_dir or (a.course is None and (Path.cwd() / "course.json").exists()):
        _, _, data = resolve_course(a, "classroom", a.course)
        rows = data.get("lessons", [])
    else:
        if a.course is None:
            raise ZjuError("远端堂次查询需要 --course；本地查询使用 --local 或 --course-dir")
        rows = Zju().course_subs(a.course)
    if a.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2, default=str, allow_nan=False))
    else:
        for row in rows:
            print(f"{row['sub_id']}\t{row.get('name', row.get('sub_name', ''))}\t{row.get('lecturer', '')}")


def ppt_one(z: Zju, a, root: Path, s: dict, pool: ThreadPoolExecutor | None = None):
    pdf = classroom_material_path(root, s, "智云PPT", "pdf")
    cdir = pdf.parent
    mapping_path = pdf.with_suffix(".json")
    state = DownloadState(root)
    pdf_key, mapping_key = (material_key(s, "智云PPT", ext) for ext in ("pdf", "json"))
    if material_complete(root, pdf_key) and material_complete(root, mapping_key) and not a.force:
        log(f"[跳过] {pdf.relative_to(root)}")
        return
    if getattr(a, "dry_run", False):
        print(f"[待检查并下载] {pdf.relative_to(root)}")
        return
    state.claim(pdf_key, pdf, **material_details(s, "智云PPT", "pdf"))
    state.claim(mapping_key, mapping_path, **material_details(s, "智云PPT", "json"))
    if pdf.exists() and not mapping_path.exists():
        log(f"[补建事件映射] {pdf.relative_to(root)}（重新生成 PDF 以保证页码对应）")
    events = z.ppt_events(s["course_id"], s["sub_id"])
    if not events:
        log(f"[无PPT] {s['course_name']} {s['sub_name']}")
        return
    tmpdir = Path(tempfile.mkdtemp(prefix="zju-ppt-"))
    try:
        def grab(iu):
            i, u = iu
            p = tmpdir / f"{i:04d}{Path(urlparse(u).path).suffix[:5] or '.jpg'}"
            for attempt in range(5):
                r = z.get(secure_url(u))
                if r.ok and r.content:
                    p.write_bytes(r.content)
                    return p
                time.sleep(0.2 * 2 ** attempt)
            raise ZjuError(f"PPT 图下载失败：{u}")

        with (nullcontext(pool) if pool is not None else ThreadPoolExecutor(max_workers=8)) as pool:
            futures = [pool.submit(grab, iu) for iu in enumerate(event["url"] for event in events)]
            try:
                paths = [future.result() for future in futures]  # 保序 = 页序
            finally:
                # 共享线程池仍在运行；删除临时目录前等所有截图任务结束。
                wait(futures)
        pages, mapping = (dedup_slides(paths) if a.dedup
                          else (paths, list(range(1, len(paths) + 1))))
        indices = {path: i for i, path in enumerate(paths)}
        representatives = [indices[path] for path in pages]
        timeline = {
            "schema_version": 1, "course_id": s["course_id"], "sub_id": s["sub_id"],
            "pdf_file": pdf.name, "deduplicated": bool(a.dedup),
            "time_field": "created_sec", "time_unit": "seconds",
            "audio_alignment": "unverified", "timestamps_are": "observations",
            "events": [], "pages": [],
        }
        for i, (event, path, page) in enumerate(zip(events, paths, mapping)):
            representative = representatives[page - 1] if page is not None else None
            timeline["events"].append({
                "event_index": i, "created_sec": event["created_sec"], "source": event["source"],
                "image_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "image_file": f"{pdf.stem}/{path.name}" if a.keep_images else None,
                "pdf_page": page, "representative_event_index": representative,
                "relationship": "blank" if page is None else "retained" if i == representative else "represented",
            })
        for page, representative in enumerate(representatives, 1):
            timeline["pages"].append({"pdf_page": page, "representative_event_index": representative,
                                      "event_indices": [i for i, p in enumerate(mapping) if p == page]})
        cdir.mkdir(parents=True, exist_ok=True)
        # 移除旧映射，避免 PDF 更新后仍留下旧页码；失败时下次运行会重建。
        mapping_path.unlink(missing_ok=True)
        images_to_pdf(pages, pdf)
        if a.keep_images:  # 原图目录也要登记，不能合并到用户自有目录。
            images = cdir / pdf.stem
            image_key = f"ppt-images:{s['course_id']}:{s['sub_id']}"
            state.claim(image_key, images, kind="ppt_images", sub_id=s["sub_id"])
            images.mkdir(exist_ok=True)
            for source in paths:
                target = images / source.name
                key = f"{image_key}:{source.name}"
                state.claim(key, target, kind="ppt_image", sub_id=s["sub_id"])
                fd, temporary = tempfile.mkstemp(prefix=".part-", dir=images)
                try:
                    with os.fdopen(fd, "wb") as output:
                        output.write(source.read_bytes())
                    os.replace(temporary, target)
                finally:
                    Path(temporary).unlink(missing_ok=True)
                state.finish(key, target)
        mapping_tmp = mapping_path.with_name(f".part-{mapping_path.name}")
        try:
            mapping_tmp.write_text(json.dumps(timeline, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            os.replace(mapping_tmp, mapping_path)
        finally:
            mapping_tmp.unlink(missing_ok=True)
        state.finish(pdf_key, pdf)
        state.finish(mapping_key, mapping_path)
        missing_times = sum(event["created_sec"] is None for event in events)
        if missing_times:
            log(f"[注意] {missing_times} 个截图事件无有效时间，映射中保留为 null")
        note = f"，去重前 {len(paths)}" if len(pages) != len(paths) else ""
        print(f"[PDF] {pdf.relative_to(root)}（{len(pages)} 页{note}）")
        print(f"[PPT事件映射] {mapping_path.relative_to(root)}（{len(events)} 个事件）")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def transcript_one(z: Zju, a, root: Path, s: dict):
    out = classroom_material_path(root, s, "转录", a.format)
    state = DownloadState(root)
    key = material_key(s, "转录", a.format)
    if material_complete(root, key) and not a.force:
        log(f"[跳过] {out.relative_to(root)}")
        return
    if getattr(a, "dry_run", False):
        print(f"[待检查并下载] {out.relative_to(root)}")
        return
    state.claim(key, out, **material_details(s, "转录", a.format))
    items = z.subtitle(s["sub_id"])
    if not items:
        log(f"[无转录] {s['course_name']} {s['sub_name']}")
        return
    out.parent.mkdir(parents=True, exist_ok=True)
    atomic_text(out, render_transcript(items, a.format, f"{s['course_name']} {s['sub_name']}"))
    state.finish(key, out)
    print(f"[转录] {out.relative_to(root)}（{len(items)} 段）")


def audio_subs(z: Zju, a, root: Path, subs: list[dict]) -> int:
    if not subs:
        log("没有课堂")
        return 0
    man, catalogues = DownloadState(root), {}
    failed = completed = skipped = planned = unavailable = 0
    limit = a.max_size * 2**20 if a.max_size else None

    def complete(key):
        return material_complete(root, key, man)

    for s in subs:
        if s.get("show") == "no":
            skipped += 1
            log(f"[已下架，跳过] 录播音轨 {s['course_name']} {s['sub_name']}")
            continue
        cid, sid = s["course_id"], s["sub_id"]
        single = classroom_material_path(root, s, "音频", "m4a")
        audio_record = man.get(f"audio:{cid}:{sid}:1")
        if not a.force and audio_record and audio_record.get("part_count") == 1 and complete(f"audio:{cid}:{sid}:1"):
            skipped += 1
            log(f"[跳过] {single.relative_to(root)}")
            continue
        local_videos = {r["part"]: r for key, r in man.data.items()
                        if r.get("kind") == "recordings" and r.get("sub_id") == sid and material_complete(root, key, man)}
        total = local_videos.get(1, {}).get("part_count", 0)
        if total and set(local_videos) == set(range(1, total + 1)):
            sources = [(i, f" - {i:02d}" if total > 1 else "", None, context_path(root, local_videos[i]["path"]))
                       for i in range(1, total + 1)]
        else:
            try:
                if cid not in catalogues:
                    try:
                        catalogues[cid] = z.video_catalogue(cid)
                    except ZjuError as e:
                        catalogues[cid] = e
                catalogue = catalogues[cid]
                if isinstance(catalogue, Exception):
                    raise catalogue
                if sid not in catalogue:
                    raise ZjuError(f"录播目录缺少堂次 {sid}")
                urls = catalogue[sid]
                if not urls:
                    unavailable += 1
                    log(f"[无回放] {s['course_name']} {s['sub_name']}")
                    continue
                sources = []
                for i, url in enumerate(urls, 1):
                    suffix = f" - {i:02d}" if len(urls) > 1 else ""
                    path = classroom_material_path(root, s, "录播", "mp4", suffix)
                    sources.append((i, suffix, url, path if complete(f"video:{cid}:{sid}:{i}") else None))
            except ZjuError as e:
                failed += 1
                log(f"[失败] 音频 {s['course_name']} {s['sub_name']}: {e}")
                continue
        for i, suffix, url, video in sources:
            dest = classroom_material_path(root, s, "音频", "m4a", suffix)
            key = f"audio:{cid}:{sid}:{i}"
            if not a.force and complete(key):
                skipped += 1
                log(f"[跳过] {dest.relative_to(root)}")
                continue
            action = "从录播提取" if video is not None else "下载音频"
            if a.dry_run:
                planned += 1
                print(f"[会{action}] {dest.relative_to(root)}")
                continue
            try:
                log(f"[{action}] {dest.relative_to(root)}")
                man.claim(key, dest, **material_details(s, "音频", "m4a", suffix))
                make_audio(url, video, dest, a.jobs, limit, **index_cache_options(a))
                man.finish(key, dest, source="local" if video is not None else "remote", part_count=len(sources))
                completed += 1
                print(f"[音频] {dest.relative_to(root)}（{dest.stat().st_size / 2**20:.1f}MB）")
            except TooBig as e:
                skipped += 1
                log(f"[太大跳过] {dest.name}: {e}（--max-size 0 不限）")
            except Exception as e:
                failed += 1
                log(f"[失败] {dest.name}: {e}")
    count = f"待处理 {planned}" if a.dry_run else f"处理 {completed}"
    log(f"{'预览' if a.dry_run else '音频完成'}：{count}、跳过 {skipped}、无回放 {unavailable}、失败 {failed}")
    return failed


def recording_subs(z: Zju, a, root: Path, subs: list[dict], pool: ThreadPoolExecutor | None = None) -> int:
    if not subs:
        log("没有课堂")
        return 0
    # 同一课程只读一次目录；完整下载成功后才记入清单。
    catalogues = {}
    man = DownloadState(root)
    failed = downloaded = skipped = unavailable = planned = 0
    limit = a.max_size * 2**20 if a.max_size else None
    for s in subs:
        if s.get("show") == "no":
            skipped += 1
            log(f"[已下架，跳过] 录播 {s['course_name']} {s['sub_name']}")
            continue
        cid, sid = s["course_id"], s["sub_id"]
        try:
            if cid not in catalogues:
                try:
                    catalogues[cid] = z.video_catalogue(cid)
                except ZjuError as e:
                    catalogues[cid] = e
            catalogue = catalogues[cid]
            if isinstance(catalogue, Exception):
                raise catalogue
            if sid not in catalogue:
                raise ZjuError(f"录播目录缺少堂次 {sid}")
            urls = catalogue[sid]
            if not urls:
                unavailable += 1
                log(f"[无回放] {s['course_name']} {s['sub_name']}")
                continue
        except ZjuError as e:
            failed += 1
            log(f"[失败] {s['course_name']} {s['sub_name']}: {e}")
            continue
        for i, url in enumerate(urls, 1):
            key = f"video:{cid}:{sid}:{i}"
            part = f" - {i:02d}" if len(urls) > 1 else ""
            dest = classroom_material_path(root, s, "录播", "mp4", part)
            if not a.force and material_complete(root, key, man):
                skipped += 1
                log(f"[跳过] {dest.relative_to(root)}")
                continue
            if a.dry_run:
                planned += 1
                print(f"[会下载] {dest.relative_to(root)}")
                continue
            try:
                log(f"[下载] {dest.relative_to(root)}")
                kwargs = {"pool": pool} if pool is not None else {}
                man.claim(key, dest, **material_details(s, "录播", "mp4", part))
                download_video(z, url, dest, limit, a.jobs, restart=a.force, **kwargs)
                man.finish(key, dest, part_count=len(urls))
                downloaded += 1
                print(f"[录播] {dest.relative_to(root)}（{dest.stat().st_size / 2**20:.1f}MB）")
            except TooBig as e:
                skipped += 1
                log(f"[太大跳过] {dest.name}: {e}（--max-size 0 不限）")
            except Exception as e:
                failed += 1
                log(f"[失败] {dest.name}: {e}")
    count = f"待下载 {planned}" if a.dry_run else f"下载 {downloaded}"
    log(f"{'预览' if a.dry_run else '完成'}：{count}、跳过 {skipped}、无回放 {unavailable}、失败 {failed}")
    return failed


# 按需录播帧：参数及 IO 保持纯 Python，PyAV 仅在 extract_recording_frames() 中延迟导入。
class FrameError(RuntimeError):
    pass


def parse_frame_time(value: str) -> float:
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
    return parse_frame_time(stamp), path


def add_recording_frame_arguments(sp, fn):
    p = sp.add_parser("recording-frame", help="按需提取智云录播真实视频帧（本地优先，远端限量 Range 读取）",
                      description="时间相对所选 MP4 分段；默认保存在独立系统临时目录，退出后保留。无需系统 FFmpeg。")
    p.add_argument("--course", type=positive_int, help="智云课程 ID；已有课程目录可省略")
    p.add_argument("--sub", type=int, required=True, help="智云 sub_id（单堂课）")
    p.add_argument("--part", type=int, help="录播分段，从 1 开始；多段时必须指定")
    course_path_arguments(p)
    p.add_argument("--offline", action="store_true", help="仅用本地完整录播，不认证、不访问远端")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--at", type=parse_frame_time, action="append", help="目标时间 HH:MM:SS.sss 或秒数，可重复")
    g.add_argument("--frame", type=frame_arg, action="append", metavar="TIME=PATH", help="每帧时间与完整路径，可重复；不能与 --output / --output-dir 混用")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--output", help="单帧完整图片路径（含文件名）")
    g.add_argument("--output-dir", help="图片输出目录；省略时创建系统临时目录")
    p.add_argument("--around", type=float, help="每个 --at 前后窗口秒数；超出分段的部分裁剪")
    p.add_argument("--interval", type=float, help="窗口截图间隔秒数，须搭配 --around")
    p.add_argument("--quality", type=int, default=95, help="JPEG 质量 1–100（默认 95）")
    p.add_argument("--format", choices=("jpg", "png"), default="jpg", help="自动命名的图片格式；完整路径按扩展名选择")
    p.add_argument("--max-fetch-mb", type=float, default=64, help="远端实际响应体读取上限 MiB（默认 64，必须 > 0）")
    p.add_argument("--max-frames", type=int, default=100, help="请求输出帧数上限（默认 100，正整数）；窗口重叠去重，视频尾裁剪前计数")
    add_index_cache_arguments(p)
    p.add_argument("--force", action="store_true", help="覆盖已有图片及元数据，忽略图片缓存（已验证索引仍可用）")
    p.add_argument("--json", action="store_true", help="stdout 只输出一个 JSON 对象，诊断信息在 stderr")
    p.set_defaults(fn=fn)


def validate_frame_args(a):
    if a.course < 1 or a.sub < 1 or (a.part is not None and a.part < 1):
        raise FrameError("--course、--sub、--part 必须 >= 1")
    if not 1 <= a.quality <= 100:
        raise FrameError("--quality 必须在 1–100 之间")
    if not math.isfinite(a.max_fetch_mb) or a.max_fetch_mb <= 0:
        raise FrameError("--max-fetch-mb 必须是有限正数")
    if isinstance(a.max_frames, bool) or not isinstance(a.max_frames, int) or a.max_frames < 1:
        raise FrameError("--max-frames 必须是正整数")
    try:
        index_cache_options(a)
    except ZjuError as error:
        raise FrameError(str(error)) from None
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
    recording_frame_times(a)  # 在任何认证、媒体请求或输出文件创建前限制请求规模。
    # 提前发现同一次调用的路径冲突，包括 Windows 上的大小写别名。
    paths = {}
    for sec, path in a.frame or []:
        key = os.path.normcase(str(Path(path).expanduser().resolve()))
        if key in paths:
            raise FrameError("同一次请求不能重复指定同一个完整输出路径")
        paths[key] = sec
    for path in [p for _, p in a.frame or []] + ([a.output] if a.output else []):
        image_format(Path(path))


def recording_frame_times(a):
    """按输出计数：普通模式保留重复项；窗口按纳秒精度取并集。

    窗口先裁剪负时间，计数不依赖视频时长；尾部裁剪只会减少输出。
    每次最多保存 max_frames+1 项，跳过舍入到相同时间的密集采样。
    """
    def check(count):
        if count > a.max_frames:
            raise FrameError(f"请求截图数量超过 --max-frames {a.max_frames}；请缩小请求或显式提高上限")
    if a.around is None:
        check(len(a.frame or a.at))
        return a.frame if a.frame else [(t, a.output) for t in a.at]
    points = set()
    scale = 10**9
    radius = Fraction(str(a.around)) * scale
    step = Fraction(str(a.interval)) * scale
    for center in a.at:
        points.add(center)
        check(len(points))
        start = Fraction(str(center)) * scale - radius
        last = (2 * radius) // step
        index = max(0, math.ceil(-start / step))
        while index <= last:
            tick = round(start + index * step)
            point = tick / scale
            points.add(point)
            check(len(points))
            # 同一舍入纳秒内可能有海量采样点；直接跳到下一个时间格。
            following = math.nextafter(point, math.inf)
            if not math.isfinite(following):
                raise FrameError("窗口时间超出可表示范围")
            threshold = max(Fraction(2 * tick + 1, 2),
                            Fraction.from_float(following) * scale - Fraction(1, 2))
            index = max(index + 1, math.ceil((threshold - start) / step))
    return [(t, None) for t in sorted(points)]


class MP4IndexCache:
    """只存完整 moov；固定头 + 按 MiB 校验的索引正文，原子发布，写者非阻塞互斥。

    读者不持锁等待网络。写者从首次索引数据到发布期间持锁，其他写者直接跳过；
    OS 自动释放异常退出进程的锁，下次写入清理未发布的 .pending。
    """
    header_size = 64 * 1024
    chunk_size = 1024 * 1024
    max_index = 64 * 1024 * 1024
    magic = b"ZJUIDX1\n"

    def __init__(self, directory, limit, identity):
        self.root = Path(directory).expanduser().resolve() / "zju-frame-index-v1"
        self.limit = limit
        self.key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        self.total = identity["size"]
        self.path = self.root / (self.key + ".idx")
        self.entry = self.writer = self.lock = None
        self.coverage = []
        self.write_attempted = self.warned = False
        try:
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                with frame_output_lock(self.root / "index-cache"):
                    (self.root / ".pending").unlink(missing_ok=True)
                    self._prune(0)
            except (OSError, FrameError):
                pass
            if self.path.is_symlink():
                return
            with self.path.open("rb") as f:
                if f.read(8) != self.magic:
                    return
                count = int.from_bytes(f.read(4), "big")
                if not 0 < count <= self.header_size - 44:
                    return
                digest = f.read(32)
                encoded = f.read(count)
                if hashlib.sha256(encoded).digest() != digest:
                    return
                m = json.loads(encoded)
                start, size = m["start"], m["size"]
                if (m["key"] != self.key or m["total"] != self.total
                        or type(start) is not int or type(size) is not int
                        or not 0 <= start < self.total or not 8 <= size < self.total
                        or start + size > self.total or size > self.max_index
                        or self.header_size + size + 1 > limit
                        or os.fstat(f.fileno()).st_size != self.header_size + size
                        or not isinstance(m["sha256"], list)
                        or len(m["sha256"]) != (size + self.chunk_size - 1) // self.chunk_size
                        or any(not isinstance(h, str) or not re.fullmatch(r"[0-9a-f]{64}", h)
                               for h in m["sha256"])):
                    return
                f.seek(self.header_size)
                box = f.read(16)
                box_size = int.from_bytes(box[:4], "big")
                box_size = int.from_bytes(box[8:16], "big") if box_size == 1 else box_size or self.total - start
                if box[4:8] != b"moov" or box_size != size:
                    return
                self.entry = m
            os.utime(self.path, None)
        except FileNotFoundError:
            pass
        except (OSError, ValueError, KeyError, TypeError):
            self.entry = None

    def _prune(self, needed):
        """调用者持写锁；只管理本命名空间的索引文件，总量包含元数据及暂存。"""
        entries = [p for p in self.root.glob("*.idx")
                   if re.fullmatch(r"[0-9a-f]{64}\.idx", p.name) and not p.is_symlink()]
        used = sum(p.stat().st_size for p in entries)
        for p in sorted(entries, key=lambda p: p.stat().st_mtime_ns):
            if used + needed + 1 <= self.limit:
                break
            length = p.stat().st_size
            try:
                p.unlink()
            except OSError:
                continue  # Windows 正在读取的文件可能暂时不能删除。
            used -= length
        return used + needed + 1 <= self.limit

    def _warn(self, error):
        if not self.warned:
            log(f"[索引缓存] 不可用（{type(error).__name__}），继续正常 Range 读取")
            self.warned = True

    def read(self, start, end):
        """读取缓存范围内的闭区间，每次校验所用块；损坏/淘汰时回退网络。"""
        m = self.entry
        if not m or not m["start"] <= start <= end < m["start"] + m["size"]:
            return None
        result = bytearray()
        try:
            if self.path.is_symlink():
                raise ValueError("symlink")
            with self.path.open("rb") as f:
                if os.fstat(f.fileno()).st_size != self.header_size + m["size"]:
                    raise ValueError("size")
                first, last = start - m["start"], end - m["start"]
                for i in range(first // self.chunk_size, last // self.chunk_size + 1):
                    offset = i * self.chunk_size
                    f.seek(self.header_size + offset)
                    data = f.read(min(self.chunk_size, m["size"] - offset))
                    if hashlib.sha256(data).hexdigest() != m["sha256"][i]:
                        raise ValueError("checksum")
                    result.extend(data[max(0, first - offset):min(len(data), last - offset + 1)])
            return bytes(result)
        except (OSError, ValueError):
            self.entry = None
            try:
                with frame_output_lock(self.root / "index-cache"):
                    self.path.unlink(missing_ok=True)
            except (OSError, FrameError):
                pass
            return None

    def capture(self, moov, base, data):
        """只收集已通过 Range 校验的数据；不会为填充缓存额外发起请求。"""
        if self.entry:
            return
        start, end = moov
        lo, hi = max(start, base), min(end, base + len(data))
        if lo >= hi:
            return
        try:
            if not self.write_attempted:
                self.write_attempted = True
                size = end - start
                if size >= self.total or size > self.max_index or size + self.header_size + 1 > self.limit:
                    return
                lock = ExitStack()
                try:
                    lock.enter_context(frame_output_lock(self.root / "index-cache"))
                except (OSError, FrameError):
                    lock.close()
                    return  # 另一个写者在填充；不阻塞截图，也不重复创建暂存数据。
                self.lock = lock
                pending = self.root / ".pending"
                pending.unlink(missing_ok=True)
                needed = self.header_size + size
                if not self._prune(needed):
                    self.close()
                    return
                self.writer = pending.open("x+b")
                self.writer.truncate(needed)  # 总容量也包含尚未发布的暂存文件。
                self.writing = {"key": self.key, "total": self.total, "start": start, "size": size}
            if not self.writer:
                return
            self.writer.seek(self.header_size + lo - start)
            self.writer.write(data[lo - base:hi - base])
            merged = []
            for left, right in sorted(self.coverage + [(lo - start, hi - start)]):
                if merged and left <= merged[-1][1]:
                    merged[-1] = (merged[-1][0], max(right, merged[-1][1]))
                else:
                    merged.append((left, right))
            self.coverage = merged
            if merged != [(0, end - start)]:
                return
            self.writer.seek(self.header_size)
            hashes = []
            remaining = end - start
            while remaining:
                chunk = self.writer.read(min(self.chunk_size, remaining))
                if not chunk:
                    raise OSError("incomplete index")
                hashes.append(hashlib.sha256(chunk).hexdigest())
                remaining -= len(chunk)
            metadata = dict(self.writing, sha256=hashes)
            encoded = json.dumps(metadata, separators=(",", ":")).encode()
            self.writer.seek(0)
            self.writer.write(self.magic + len(encoded).to_bytes(4, "big")
                              + hashlib.sha256(encoded).digest() + encoded)
            self.writer.flush()
            os.fsync(self.writer.fileno())
            self.writer.close()
            self.writer = None
            os.replace(self.root / ".pending", self.path)
            self.entry = metadata
            self.close()
        except (OSError, ValueError) as error:
            self._warn(error)
            self.close()

    def close(self):
        try:
            if self.writer:
                self.writer.close()
        except OSError:
            pass
        finally:
            self.writer = None
            if self.lock:
                try:
                    (self.root / ".pending").unlink(missing_ok=True)
                except OSError:
                    pass
                self.lock.close()
                self.lock = None


class FrameRangeReader(io.RawIOBase):
    """PyAV 自定义 IO：1MiB 块，已确认的 moov 索引最多合并 4 块，缓存 8MiB。

    独立无凭据客户端，直连，不继承学校 Cookie、认证或环境代理。
    不跟随重定向，避免把签名查询或未来加入的凭据转发到其他主机。
    """
    block_size = 1024 * 1024
    cache_size = 8 * 1024 * 1024

    def __init__(self, url, max_bytes=64 * 2**20, *, client=None,
                 index_cache_dir=_DEFAULT_INDEX_CACHE, index_cache_bytes=256 * 2**20):
        super().__init__()
        try:
            mp4_resource(url)
        except ZjuError as error:
            raise FrameError(str(error)) from None
        self.url = url
        self.client = client or httpx.Client(timeout=httpx.Timeout(30, connect=15), trust_env=False,
                                             follow_redirects=False, headers={"Accept-Encoding": "identity"})
        self.own_client = client is None
        self.max_bytes = int(max_bytes)
        self.requests = self.bytes_received = self.position = 0
        self.total = None
        self.validator = None
        self.cache = OrderedDict()
        self.index_cache = None
        self.last_error = None
        try:
            self._fetch(0, 15)
            self.identity = mp4_identity(url, self.total, self.validator)
            self.cache_reusable = self.validator[0] == "ETag"
            self.index_cache = open_mp4_index_cache(index_cache_dir, index_cache_bytes, self.identity)
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
                    try:
                        validator = mp4_validator(r.headers, self.validator)
                    except ZjuError as error:
                        raise FrameError(str(error)) from None
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
                    self._cache_blocks(base)
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

    def _cache_blocks(self, base):
        end = min(base + self.block_size, self.total)
        moov = getattr(self, "_moov_range", None)
        if moov and moov[0] <= base < moov[1]:
            # 仅合并已知索引的连续块，媒体仍按 1MiB 随机读取；不越过
            # 原本逐块读取的对齐边界。预算不足时退回单块，避免提前耗尽预算。
            blocks = min(4, max(1, self.cache_size // self.block_size))
            upper = min(base + blocks * self.block_size, self.total,
                        ((moov[1] + self.block_size - 1) // self.block_size) * self.block_size)
            for cached_base in range(base + self.block_size, upper, self.block_size):
                if cached_base in self.cache:
                    upper = cached_base
                    break
            if self.bytes_received + upper - base <= self.max_bytes:
                end = upper
        data = self._range_data(base, end - 1)
        # 从已确认的顶层 box 边界行走，不在媒体中搜索 "moov" 字符串。
        # mdat 后的 moov 也可识别；头部不足或损坏时保持原逐块读取行为。
        offset = getattr(self, "_next_box", 0)
        while base <= offset and offset + 8 <= base + len(data):
            pos = offset - base
            size, kind = int.from_bytes(data[pos:pos + 4], "big"), data[pos + 4:pos + 8]
            header = 8
            if size == 1:
                if pos + 16 > len(data):
                    break
                size, header = int.from_bytes(data[pos + 8:pos + 16], "big"), 16
            if size == 0:
                size = self.total - offset
            if size < header or offset + size > self.total:
                break
            if kind == b"moov":
                self._moov_range = offset, offset + size
                break
            offset += size
            self._next_box = offset
        if self.index_cache and getattr(self, "_moov_range", None):
            self.index_cache.capture(self._moov_range, base, data)
        for pos in range(0, len(data), self.block_size):
            self.cache[base + pos] = data[pos:pos + self.block_size]
            while len(self.cache) > self.cache_size // self.block_size:
                self.cache.popitem(last=False)

    def _range_data(self, start, end):
        cache = self.index_cache
        if cache and cache.entry:
            m = cache.entry
            lo, hi = max(start, m["start"]), min(end, m["start"] + m["size"] - 1)
            if lo <= hi:
                middle = cache.read(lo, hi)
                if middle is not None:
                    # 索引外的字节仍经过原 Range 校验与流量计数，不持久保存媒体。
                    prefix = self._fetch(start, lo - 1) if start < lo else b""
                    suffix = self._fetch(hi + 1, end) if hi < end else b""
                    return prefix + middle + suffix
        return self._fetch(start, end)

    def close(self):
        if not self.closed:
            if getattr(self, "index_cache", None):
                self.index_cache.close()
            if getattr(self, "own_client", False):
                self.client.close()
            if hasattr(self, "cache"):
                self.cache.clear()
        super().close()


def select_recording_part(urls, part):
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
                and all(m["frame"][field] == key[field] for field in
                        ("course_id", "sub_id", "part", "requested_time_sec"))
                and math.isfinite(m["frame"]["actual_time_sec"])
                and path.is_file() and m["image_sha256"] == file_hash(path)):
            return dict(m["frame"], path=str(path), output_path=str(path), cache_hit=True)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        pass
    return None


@contextmanager
def frame_output_lock(path):
    """所有图片/sidecar 发布者使用同一系统锁；保留锁 inode 避免并发竞态。"""
    fd = os.open(path.with_name(f".{path.name}.frame.lock"), os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(fd, "r+b") as lock:
        try:
            if os.name == "nt":
                import msvcrt
                if os.fstat(lock.fileno()).st_size == 0:
                    lock.write(b"\0")
                    lock.flush()
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise FrameError(f"另一进程正在写入图片：{path}") from None
        try:
            yield
        finally:
            if os.name == "nt":
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


def write_frame_image(frame, path, quality):
    """仅编码到私有暂存目录；图片及 sidecar 的发布统一由 publish_frame_pair 处理。"""
    with path.open("xb") as f:
        opts = {"quality": quality, "subsampling": 0} if image_format(path) == "JPEG" else {}
        frame.to_image().save(f, format=image_format(path), **opts)
        f.flush()
        os.fsync(f.fileno())
    return file_hash(path)


def atomic_json(path, data):
    fd, name = tempfile.mkstemp(prefix=".zju-frame-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, allow_nan=False)
            f.write("\n")
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def publish_frame_pair(staged, path, record, overwrite):
    """先完成两份文件，再在锁内发布；异常时仅回滚本进程已替换的文件。

    同一文件系统中保留旧文件备份，避免 --force 写入 sidecar 失败时丢失旧缓存。
    两文件无法同时 rename；哈希校验拒绝短暂的不一致，锁防止并发写入/回滚互相覆盖。
    """
    meta = sidecar(path)
    staged_meta = sidecar(staged)
    atomic_json(staged_meta, record)  # JSON 编码/写入失败时尚未触及用户文件。
    with frame_output_lock(path):
        if not overwrite and (path.exists() or meta.exists()):
            raise FrameError(f"图片或元数据已存在，未覆盖：{path}（使用 --force）")
        backups = {}
        for dest in (path, meta):
            if dest.exists():
                backup = staged.parent / ("original-" + dest.name)
                shutil.copy2(dest, backup)
                backups[dest] = backup
        replaced = []
        try:
            for src, dest in ((staged, path), (staged_meta, meta)):
                os.replace(src, dest)
                replaced.append(dest)
        except BaseException:
            for dest in reversed(replaced):
                if dest in backups:
                    os.replace(backups[dest], dest)
                else:
                    dest.unlink()
            raise


class FrameDecoder:
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

    def decode_after_seek(self):
        # 保留落点包的 DTS；解码帧的 DTS 在 B 帧重排/多线程缓冲后不可用于回退。
        self.seek_dts = None
        first = True
        for packet in self.container.demux(self.stream):
            if first:
                if packet.dts is not None:
                    self.seek_dts = math.floor(packet.dts * packet.time_base / self.timebase)
                first = False
            yield from packet.decode()

    def at(self, target):
        point = Fraction(str(target))
        if self.iterator is None or target < self.last_target or target - self.last_target > 10:
            offset = math.floor((point + self.origin) / self.timebase)
            for attempt in range(2):
                self.container.seek(offset, stream=self.stream, backward=True, any_frame=False)
                self.seeks += 1
                self.iterator = self.decode_after_seek()
                self.previous = None
                self.next_frame = self.advance()
                if self.next_frame is None or self.timestamp(self.next_frame) <= point:
                    break
                # MP4 按 DTS 索引 seek，固定的 PTS 偏移在 B 帧 + VFR 时可能落到未来。
                # 仅纠正一次：退到落点包 DTS 前一 tick 的关键帧，再按实际 PTS 解码。
                # 不猜帧率、不按秒扩大扫描；异常索引仍然失败，避免无界回退。
                if attempt or self.seek_dts is None:
                    break
                offset = min(offset - 1, self.seek_dts - 1)
        # 解码器按显示顺序输出（包含 B 帧重排），不使用 DTS 或平均帧率。
        while self.next_frame is not None and self.timestamp(self.next_frame) <= point:
            self.previous = self.next_frame
            self.next_frame = self.advance()
        self.last_target = target
        if self.previous is None:
            raise FrameError(f"时间 {target:g} 秒没有可显示的视频帧")
        return self.previous, float(self.timestamp(self.previous))


def frame_output_plans(a, part, identity, times, directory):
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
    return plans


def cached_frame_duration(plans):
    """全部图片命中时，从匹配来源的请求记录恢复时长；旧/缺失记录回退到 MP4。"""
    if not plans or not all(hit for _, _, _, hit in plans):
        return None
    duration = None
    try:
        for sec, path, key, hit in plans:
            record = json.loads(sidecar(path).read_text(encoding="utf-8"))
            # 再次核对 key，避免两次 sidecar 读取之间被另一进程替换。
            request = record["request"]
            value = request["duration_sec"]
            if (record["cache_key"] != key or isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or not 0 <= sec < value
                    or any(request[field] != key[field] for field in ("course_id", "sub_id", "part"))
                    or request["source_size"] != key["source"]["size"]
                    or request["source_type"] != hit["source_type"]
                    or (duration is not None and duration != value)):
                return None
            duration = value
    except (OSError, ValueError, KeyError, TypeError, OverflowError):
        return None
    return duration


def extract_recording_frames(a, source, part, *, client=None):
    """source 是完整本地 Path 或远端 URL；返回稳定的请求级 JSON 对象。"""
    validate_frame_args(a)
    started = time.monotonic()
    reader = None
    temporary = not (a.frame or a.output or a.output_dir)
    directory = Path(a.output_dir).expanduser().resolve() if a.output_dir else None

    def result_for(frames, duration, seeks=0, decoded=0):
        return {"course_id": a.course, "sub_id": a.sub, "part": part, "temporary": temporary,
                "output_dir": str(directory) if directory else None, "frames": frames,
                "frame_count": len(frames), "source_type": source_type,
                "source_size": identity["size"], "duration_sec": duration,
                "network": {"requests": reader.requests if reader else 0,
                            "bytes_received": reader.bytes_received if reader else 0},
                "decode": {"seeks": seeks, "frames": decoded},
                "elapsed_sec": time.monotonic() - started}

    def check_local_source():
        if source_type == "local":
            after = source.stat()
            if (after.st_size, after.st_mtime_ns, after.st_ctime_ns, after.st_ino) != (
                    before.st_size, before.st_mtime_ns, before.st_ctime_ns, before.st_ino):
                raise FrameError("本地视频在提取期间发生变化，输出不可复用；请重新运行")

    try:
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
                reader = stack.enter_context(FrameRangeReader(
                    source, int(a.max_fetch_mb * 2**20), client=client,
                    **index_cache_options(a)))
                handle, source_type, identity = reader, "remote", reader.identity
            plans = None
            # 已有输出且所有精确时间均命中时，只验证来源/图片，不打开 MP4 索引。
            # 远端仍先发 Range 探测，只有强 ETag 身份才能跨次命中。
            if not a.force and not temporary and a.around is None:
                plans = frame_output_plans(a, part, identity, recording_frame_times(a), directory)
                duration = cached_frame_duration(plans)
                if duration is not None:
                    check_local_source()
                    return result_for([hit for _, _, _, hit in plans], duration)
            # 运行其他命令或全部缓存命中时均不初始化原生视频库。
            import av
            container = stack.enter_context(av.open(handle, mode="r", format="mp4"))
            if reader and reader.last_error:
                raise reader.last_error
            decoder = FrameDecoder(container)
            for sec, _ in a.frame or [(t, a.output) for t in a.at]:
                if sec >= decoder.duration:
                    raise FrameError(f"请求时间 {sec:g} 秒超出视频范围 [0, {decoder.duration:g}) 秒")
            times = [(sec, path) for sec, path in recording_frame_times(a) if sec < decoder.duration]
            if temporary:
                directory = Path(tempfile.mkdtemp(prefix="zju-frames-")).resolve()
            if plans is None:
                plans = frame_output_plans(a, part, identity, times, directory)
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
                path.parent.mkdir(parents=True, exist_ok=True)
                staging = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix=".zju-frame-", dir=path.parent)))
                staged = staging / path.name
                digest = write_frame_image(frame, staged, a.quality)
                record = {"cache_key": key, "image_sha256": digest, "frame": metadata}
                written.append((staged, path, record))
                results[index] = metadata
            check_local_source()
            frames = [results[i] for i in range(len(plans))]
            result = result_for(frames, decoder.duration, decoder.seeks, decoder.decoded_frames)
            request = {key: value for key, value in result.items() if key != "frames"}
            for staged, path, record in written:
                publish_frame_pair(staged, path, dict(record, request=request), a.force)
            result["elapsed_sec"] = time.monotonic() - started
            return result
    except Exception as e:
        if reader and reader.last_error:
            raise reader.last_error from None
        if isinstance(e, FrameError):
            raise
        # 原始 HTTP/FFmpeg 异常可能携带签名 URL；只返回异常类型。
        raise FrameError(f"视频帧提取失败（{type(e).__name__}）；请检查视频、输出权限和解码支持") from None


def recording_frame_source(z, a, root):
    """复用课程查询、标准录播路径及完成清单；不把 .part 当作完整视频。"""
    local = {}
    if root is not None:
        read_course(root, "classroom", a.course)
        state = DownloadState(root)
        for key, rec in state.data.items():
            if rec.get("kind") == "recordings" and rec.get("sub_id") == a.sub and material_complete(root, key, state):
                if rec["part"] in local:
                    raise FrameError("同一分段有多个已登记视频，无法选择")
                local[rec["part"]] = context_path(root, rec["path"])
    if a.part in local:
        return local[a.part], a.part
    if a.part is None and local:
        if set(local) == {1} and state.get(f"video:{a.course}:{a.sub}:1").get("part_count") == 1:
            return local[1], 1
        raise FrameError("本地有分段录播，请指定 --part（编号与 recording 相同）")
    # 有完整本地来源时不需要认证/API，允许离线读取；否则复用智云认证与查询。
    if getattr(a, "offline", False):
        raise FrameError("没有可确认完整的本地录播；--offline 禁止远端读取")
    z = z or Zju(classroom_first=True)
    subs = z.course_subs(a.course)
    sub = next((s for s in subs if s["sub_id"] == a.sub), None)
    if sub is None:
        raise FrameError(f"课程 {a.course} 中没有堂次 {a.sub}")
    if sub.get("show") == "no":
        raise FrameError("该堂次课程已下架，且没有可确认完整的本地录播")
    catalogue = z.video_catalogue(a.course)
    if a.sub not in catalogue:
        raise FrameError(f"录播目录缺少堂次 {a.sub}")
    urls = catalogue[a.sub]
    part, url = select_recording_part(urls, a.part)
    return url, part


def cmd_recording_frame(a):
    try:
        directory, a.course, _ = resolve_course(a, "classroom", a.course, optional=True)
        validate_frame_args(a)
        # 认证过程中若有交互提示，也不能污染 --json 的 stdout。
        from contextlib import redirect_stdout
        with redirect_stdout(sys.stderr):
            source, part = recording_frame_source(None, a, directory)
            result = extract_recording_frames(a, source, part)
        if a.json:
            # JSON 转义保留真实路径，且兼容 Windows 重定向时的非 UTF-8 代码页。
            print(json.dumps(result, ensure_ascii=True, allow_nan=False))
        else:
            for frame in result["frames"]:
                state = "缓存" if frame["cache_hit"] else "截图"
                print(f"[{state}] {frame['requested_time_sec']:g}s → {frame['actual_time_sec']:g}s  {frame['path']}")
            log(f"完成：{result['frame_count']} 帧，读取 {result['network']['bytes_received'] / 2**20:.2f} MiB，"
                f"{result['network']['requests']} 次媒体请求，{result['elapsed_sec']:.2f} 秒")
    except FrameError as e:
        raise ZjuError(str(e)) from None
    except (OSError, ValueError, TypeError) as e:
        raise ZjuError(f"录播帧参数或本地清单读取失败（{type(e).__name__}），请检查课程目录及 .zju/downloads.json") from None


def positive_int(value):
    value = int(value)
    if value < 1:
        raise argparse.ArgumentTypeError("必须是正整数")
    return value


def nonnegative_int(value):
    value = int(value)
    if value < 0:
        raise argparse.ArgumentTypeError("必须是非负整数")
    return value


def course_path_arguments(parser):
    paths = parser.add_mutually_exclusive_group()
    paths.add_argument("--library", help="包含多门课程目录的资料库；显式路径不追加父级")
    paths.add_argument("--course-dir", help="直接包含 course.json 的单门课程目录")


def sync_arguments(parser):
    course_path_arguments(parser)
    parser.add_argument("course", type=positive_int, nargs="*", help="课程 ID；已有 --course-dir 可省略")
    parser.add_argument("--all", action="store_true", help="显式同步全部可访问个人课程")
    parser.add_argument("--dry-run", action="store_true", help="预览，不写课程资料；认证仍可能更新会话")
    parser.add_argument("-j", "--jobs", type=positive_int, default=4)


def main():
    p = argparse.ArgumentParser(prog="zju.py", description="学在浙大 / 智云课堂单文件 CLI")
    sp = p.add_subparsers(dest="cmd", required=True)
    x = sp.add_parser("login", help="登录，密码保存在系统凭据库；保留其他配置")
    x.add_argument("username", nargs="?")
    x.add_argument("--reset", action="store_true", help="重新设置凭据与会话，不删除其他设置")
    x.set_defaults(fn=cmd_login)
    x = sp.add_parser("lms", help="学在浙大：课件、活动、待办和提交")
    lp = x.add_subparsers(dest="action", required=True)

    x = lp.add_parser("courses", help="列出学在浙大课程")
    x.add_argument("--all", action="store_true", help="含往年课程（默认只列最新学年）")
    x.add_argument("--json", action="store_true")
    x.add_argument("--local", action="store_true", help="完全离线列出资料库课程")
    x.add_argument("--library", help="资料库，仅与 --local 一起使用")
    x.set_defaults(fn=cmd_courses, action="courses", platform="lms")

    x = lp.add_parser("sync", help="同步指定课程或显式 --all")
    sync_arguments(x)
    x.add_argument("--videos", action="store_true", help="包括活动中的音视频附件")
    x.add_argument("--max-size", type=nonnegative_int, default=200, metavar="MiB")
    x.set_defaults(fn=cmd_sync)

    x = lp.add_parser("todo", help="待办事项")
    x.add_argument("--json", action="store_true")
    x.set_defaults(fn=cmd_todo)

    x = lp.add_parser("activities", help="列出课程活动（课件／视频／作业／讨论／测验…）")
    x.add_argument("course", nargs="*", help="课程 id 或名称片段；省略 = 最新学年所有课程")
    x.add_argument("--all", action="store_true", help="没指定课程时含往年课程")
    x.add_argument("--type", nargs="*", metavar="T", help=f"只列这些类型：{', '.join(ACT_TYPES)}")
    x.add_argument("--json", action="store_true")
    x.set_defaults(fn=cmd_activities)

    x = lp.add_parser("show", help="单一活动详情（说明、附件、作业提交状态、讨论帖数）")
    x.add_argument("activity", type=int)
    x.add_argument("--read", action="store_true", help="下载附件并输出文字（docx/pptx/pdf/doc/txt）")
    x.set_defaults(fn=cmd_show)

    def body_args(x):
        x.add_argument("--body", help="内容（纯文字，空行分段）")
        x.add_argument("--body-file", help="从文件读内容；- = stdin")
        x.add_argument("--html", action="store_true", help="内容已经是 HTML，不转换")
        x.add_argument("--attach", nargs="*", metavar="FILE", help="附件")

    x = lp.add_parser("forum", help="讨论区：list / read / post / reply")
    fp = x.add_subparsers(dest="action", required=True)
    y = fp.add_parser("list", help="列出讨论帖（给讨论活动 id）")
    y.add_argument("id", type=int, help="讨论活动 id（activities --type forum 查）")
    y.add_argument("--mine", action="store_true", help="只看自己的发帖与回帖")
    y.add_argument("--full", action="store_true", help="连内文一起印")
    y = fp.add_parser("read", help="读一则帖子与回复")
    y.add_argument("id", type=int, help="topic id")
    y = fp.add_parser("post", help="发新帖")
    y.add_argument("id", type=int, help="讨论活动 id")
    y.add_argument("--title", required=True)
    body_args(y)
    y = fp.add_parser("reply", help="回帖")
    y.add_argument("id", type=int, help="topic id")
    body_args(y)
    x.set_defaults(fn=cmd_forum)

    x = lp.add_parser("upload", help="上传文件到学在浙大，输出 upload id")
    x.add_argument("files", nargs="+")
    x.set_defaults(fn=cmd_upload)

    x = lp.add_parser("submit", help="交作业（附件＋文字），默认送出前确认")
    x.add_argument("activity", type=int, help="作业活动 id（activities --type homework 查）")
    x.add_argument("--file", nargs="*", metavar="FILE", help="要交的文件")
    x.add_argument("--upload-id", type=int, nargs="*", help="已用 upload 指令传好的文件 id")
    x.add_argument("--body", help="作业文字内容")
    x.add_argument("--body-file", help="从文件读作业文字；- = stdin")
    x.add_argument("--html", action="store_true", help="文字已经是 HTML")
    x.add_argument("--draft", action="store_true", help="只存草稿不正式提交")
    x.add_argument("-y", "--yes", action="store_true", help="不确认直接送出")
    x.set_defaults(fn=cmd_submit)

    x = sp.add_parser("classroom", help="智云课堂：同步、资料获取、离线上下文")
    cp = x.add_subparsers(dest="action", required=True)
    y = cp.add_parser("courses", help="课程列表；--local 完全离线发现资料库课程")
    y.add_argument("--local", action="store_true")
    y.add_argument("--library", help="本地资料库，仅与 --local 一起使用")
    y.add_argument("--has-tasks", action="store_true", help="远端列表只列任务数大于 0 的课程")
    y.add_argument("--json", action="store_true")
    y.set_defaults(fn=cmd_classroom, platform="classroom")
    y = cp.add_parser("subs", help="堂次列表；--local 或 --course-dir 完全离线")
    course_path_arguments(y)
    y.add_argument("--course", type=positive_int)
    y.add_argument("--local", action="store_true")
    y.add_argument("--json", action="store_true")
    y.set_defaults(fn=cmd_classroom_subs)
    y = cp.add_parser("search", help="搜索远端课程")
    y.add_argument("arg", nargs="?")
    y.add_argument("--teacher")
    y.set_defaults(fn=cmd_classroom)
    y = cp.add_parser("day", help="某天或截至该日最近 N 天的课堂，含追踪课程")
    y.add_argument("arg", nargs="?", help="YYYY-MM-DD，默认今天")
    y.add_argument("--days", type=positive_int, default=1)
    y.set_defaults(fn=cmd_classroom)
    for action, help_text in (("add", "追踪课表外课程"), ("rm", "取消追踪课程")):
        y = cp.add_parser(action, help=help_text)
        y.add_argument("arg")
        y.add_argument("more", nargs="*")
        y.set_defaults(fn=cmd_classroom)
    cp.add_parser("tracked", help="列出本地追踪设置").set_defaults(fn=cmd_classroom)
    y = cp.add_parser("context", help="完全离线、只读定位课堂候选上下文；时间轴未校准")
    course_path_arguments(y)
    y.add_argument("--course", type=positive_int, help="课程 ID；课程目录已登记时可省略")
    y.add_argument("--sub", type=positive_int, required=True)
    y.add_argument("--at", type=parse_frame_time, required=True)
    y.add_argument("--window", type=parse_frame_time, default=20., help="前后窗口秒数，默认 20")
    y.add_argument("--json", action="store_true")
    y.set_defaults(fn=cmd_classroom_context)
    y = cp.add_parser("sync", help="同步指定课程，或显式 --all；默认 PPT 与 Markdown")
    sync_arguments(y)
    y.add_argument("--recording", action="store_true")
    y.add_argument("--recording-audio", action="store_true")
    y.add_argument("--force", action="store_true", help="重做已登记资料；不覆盖用户自有文件")
    y.add_argument("--format", choices=["md", "srt", "txt"], default="md")
    y.add_argument("--dedup", action="store_true")
    y.add_argument("--keep-images", action="store_true")
    y.add_argument("--max-size", type=nonnegative_int, default=0, metavar="MiB")
    add_index_cache_arguments(y)
    y.set_defaults(fn=cmd_classroom_sync)
    for name in ("ppt", "transcript", "recording", "recording-audio"):
        y = cp.add_parser(name, help={"ppt": "PPT 截图及事件映射", "transcript": "课堂转写（默认 Markdown）",
            "recording": "完整录播，支持断点续传", "recording-audio": "本地优先无损提取音频，否则只下载音轨"}[name])
        course_path_arguments(y)
        y.add_argument("--course", type=positive_int)
        y.add_argument("--sub", type=positive_int, nargs="+")
        y.add_argument("--days", type=positive_int, help="最近 N 天；未指定课程时默认今天")
        y.add_argument("--force", action="store_true", help="重做已登记资料，不覆盖用户文件")
        y.add_argument("--dry-run", action="store_true", help="只预览，不写课程文件或下载媒体")
        y.add_argument("-j", "--jobs", type=positive_int, default=32 if name == "recording-audio" else 4)
        if name == "ppt":
            y.add_argument("--dedup", action="store_true")
            y.add_argument("--keep-images", action="store_true")
        elif name == "transcript":
            y.add_argument("--format", choices=["md", "srt", "txt"], default="md")
        else:
            y.add_argument("--max-size", type=nonnegative_int, default=0, metavar="MiB")
            if name == "recording-audio":
                add_index_cache_arguments(y)
        y.set_defaults(fn=cmd_classroom_material)
    add_recording_frame_arguments(cp, cmd_recording_frame)
    a = p.parse_args()
    if a.cmd == "lms" and a.action == "activities" and a.course and a.all:
        p.error("activities 的课程选择不能与 --all 混用")
    if getattr(a, "local", False) and (getattr(a, "all", False) or getattr(a, "has_tasks", False)):
        p.error("本地课程发现不能与远端 --all / --has-tasks 混用")
    if getattr(a, "action", None) == "courses" and a.library is not None and not a.local:
        p.error("courses --library 需要 --local")
    if getattr(a, "action", None) == "subs" and a.library is not None and not a.local:
        p.error("subs --library 需要 --local")
    if getattr(a, "fn", None) == cmd_classroom_material:
        if a.days is not None and (a.course is not None or a.course_dir is not None):
            p.error("--days 不能与 --course / --course-dir 混用")
        if a.sub and a.course is None and a.course_dir is None:
            p.error("--sub 需要 --course 或 --course-dir")
    if hasattr(a, "index_cache_mb"):
        try:
            index_cache_options(a)
        except ZjuError as error:
            p.error(str(error))
    try:
        a.fn(a)
    except (ZjuError, OSError, ValueError) as e:
        log(f"错误：{e}")
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
