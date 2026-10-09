#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["requests", "httpx", "img2pdf", "pillow", "keyring", "numpy"]
# ///
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 8eoyw
# Portions ported from PeiPei233/zju-learning-assistant, Copyright (c) 2023 PeiPei233 (MIT).
"""学在浙大 / 智云课堂 命令列工具。

API 逻辑移植自 PeiPei233/zju-learning-assistant (ZLA) 的 src-tauri/src/zju_assist.rs，
改成可脚本化、可排程、可被 AI agent 直接呼叫的单文件 CLI。

  zju.py login                         # 首次：存学号，密码进 macOS Keychain
  zju.py courses [--all]               # 学在浙大课程列表
  zju.py sync [课程...] [--dry-run]     # 增量同步课程附件（含排程中的活动）
  zju.py todo                          # 待办
  zju.py activities [课程...] [--type forum homework ...]  # 所有活动（含测验）
  zju.py show <活动id>                  # 活动详情；作业显示自己的提交状态
  zju.py forum list|read|post|reply ... # 讨论区
  zju.py upload 文件...                 # 上传，印 upload id
  zju.py submit <作业id> --file ... [--body ...] [--draft] [-y]  # 交作业
  zju.py classroom courses [--has-tasks] [--json]  # 智云个人课程与任务数
  zju.py classroom sync [课程...] [-j 4] [--recording] [--recording-audio]  # PPT、转写及可选录播和音频
  zju.py classroom search 关键字        # 智云课堂找课（id 与学在浙大不同）
  zju.py classroom subs <cid>          # 列出每堂课
  zju.py classroom day [日期] [--days N]
  zju.py ppt --course <cid> | --days N [--dedup]  # 智云 PPT 截图合并 PDF
  zju.py transcript --course <cid> | --days N [--format txt|srt|md]
  zju.py recording --course <cid> | --days N [--dry-run]  # 智云录播 MP4
  zju.py recording-audio --course <cid> | --days N [-j 32]  # 本地录播提取或仅下载音轨
"""
from __future__ import annotations

import argparse
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
from contextlib import contextmanager, nullcontext
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

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
DEFAULT_OUT = Path.home() / "ZJU-Courses"  # 可用 config.json 的 "out" 或环境变数 ZJU_OUT 覆盖
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


def classroom_material_path(root: Path, sub: dict, kind: str, extension: str, part: str = "") -> Path:
    """智云资料统一按课程和堂次 ID 命名。"""
    course = safe_name(f"{sub['course_name']} ({sub['course_id']})")
    stem = f"{safe_name(sub['sub_name'])} ({sub['sub_id']})"
    return root / course / kind / f"{stem}{part}.{extension}"


# ---------------- config / credentials ----------------

def state_dir() -> Path:
    STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(STATE_DIR, 0o700)
    return STATE_DIR


def load_config() -> dict:
    if not CONFIG_FILE.exists():
        return {}
    try:
        return json.loads(CONFIG_FILE.read_text())
    except ValueError as e:
        raise ZjuError(f"{CONFIG_FILE} 格式错误：{e}")


def save_config(cfg: dict):
    state_dir()
    CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2))


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
    def __init__(self):
        self.jar = requests.cookies.RequestsCookieJar()  # 各执行绪 session 共用（CookieJar 自带锁）
        self._tl = threading.local()
        self.logged_in = False
        self._load_cookies()

    def _load_cookies(self):
        (STATE_DIR / "cookies.pkl").unlink(missing_ok=True)  # 旧版 pickle 快取：不再读取
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
        if self.courses_alive() and (not need_classroom or self._token(silent=True)):
            self.logged_in = True
            return
        self.login(*get_credentials())

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
                        subs.append({"course_id": course_id, "course_name": data["title"],
                                     "sub_id": int(s["id"]), "sub_name": s["sub_title"],
                                     "lecturer": s.get("lecturer_name", ""), "show": s.get("show")})
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

class Manifest:
    """out/.zju_manifest.json：记录 upload id → 本地路径，换版（新 id）就重新下载。"""

    def __init__(self, root: Path):
        self.path = root / ".zju_manifest.json"
        self.data = json.loads(self.path.read_text()) if self.path.exists() else {}

    def get(self, key):
        return self.data.get(key)

    def put(self, key, val):
        self.data[key] = val
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1))
        os.replace(tmp, self.path)


def stream_to(r: requests.Response, dest: Path, limit: int | None = None, *, mp4: bool = False) -> Path:
    """写暂存档再 rename；验证长度、拒收空档和错误页，免得坏档被记进 manifest 后永远不再重新下载。"""
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
        os.replace(tmp, dest)
        return dest
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    finally:
        r.close()


@contextmanager
def download_lock(dest: Path, kind: str):
    """同一输出文件只允许一个程序写入，锁文件保留以免 inode 更换。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    # 锁档保留以避免删除后新旧 inode 被不同程序同时锁定。
    lock_path = dest.with_name(f".{dest.name}.download.lock")
    with lock_path.open("a+b") as lock_file:
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
            raise ZjuError(f"另一个程序正在处理此{kind}") from e
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


class AudioRanges:
    def __init__(self, client, url):
        self.client, self.url = client, url
        self.total = None
        self.validator = None
        self.received = 0

    async def get(self, ranges):
        headers = {"Range": "bytes=" + ",".join(f"{a}-{b}" for a, b in ranges),
                   "Accept-Encoding": "identity"}
        if self.validator:
            headers["If-Range"] = self.validator
        for attempt in range(3):
            try:
                async with self.client.stream("GET", self.url, headers=headers) as r:
                    if r.status_code != 206:
                        raise ZjuError(f"音频范围请求失败 HTTP {r.status_code}（不下载整个视频）")
                    if r.headers.get("Content-Encoding", "identity") != "identity":
                        raise ZjuError("音频范围响应使用了压缩编码")
                    if self.total is None:
                        match = re.fullmatch(r"bytes \d+-\d+/(\d+)", r.headers.get("Content-Range", ""))
                        if not match:
                            raise ZjuError("录播范围探测失败")
                        self.total = int(match[1])
                        etag = r.headers.get("ETag", "")
                        self.validator = (etag if etag and not etag.startswith("W/")
                                          else r.headers.get("Last-Modified"))
                    elif self.validator:
                        current = (r.headers.get("ETag") if self.validator.startswith('"')
                                   else r.headers.get("Last-Modified"))
                        if current and current != self.validator:
                            raise ZjuError("录播在音频下载期间发生变化")
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
            except (httpx.HTTPError, ZjuError) as e:
                if attempt == 2:
                    if isinstance(e, ZjuError):
                        raise
                    raise ZjuError(f"音频网络请求失败（{type(e).__name__}）") from e
                await asyncio.sleep(0.5 * 2**attempt)


async def download_audio_ranges(url: str, raw: Path, jobs: int, limit: int | None) -> AudioIndex:
    # 不读取环境代理，也不回退；DNS 辅助线程最多两个，音频请求全部由协程处理。
    asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(max_workers=2))
    async with httpx.AsyncClient(http2=False, trust_env=False, proxy=None, follow_redirects=True,
                                 headers={"User-Agent": UA}, timeout=httpx.Timeout(60, connect=6),
                                 limits=httpx.Limits(max_connections=jobs, max_keepalive_connections=jobs)) as client:
        source = AudioRanges(client, url)
        first = await source.get([(0, 15)])
        offset, ftyp, moov = 0, None, None
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

                async def grab_index(i):
                    chunks[i] = await source.get([(offset + i * step, min(offset + (i + 1) * step - 1, offset + size - 1))])

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
               limit: int | None = None):
    with download_lock(dest, "音频"), tempfile.TemporaryDirectory(prefix=".zju-audio-", dir=dest.parent) as temp:
        temp = Path(temp)
        raw, output = temp / "payload.bin", temp / "output.m4a"
        if video is None:
            if not url:
                raise ZjuError("没有可下载的录播")
            index = asyncio.run(download_audio_ranges(url, raw, jobs, limit))
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
    s = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>", "\n", s or "")
    s = html.unescape(re.sub(r"<[^>]+>", "", s))
    return re.sub(r"\n{3,}", "\n\n", s).strip()


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
    sec = float(sec)
    h, rem = divmod(int(sec), 3600)
    m, s = divmod(rem, 60)
    ms = int((sec - int(sec)) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}" if srt else f"{h:02d}:{m:02d}:{s:02d}"


def render_transcript(items: list[dict], fmt: str, title: str) -> str:
    lines = []
    if fmt == "srt":
        for i, c in enumerate(items, 1):
            lines += [str(i), f"{fmt_ts(c.get('BeginSec', 0))} --> {fmt_ts(c.get('EndSec', 0))}", c.get("Text", ""), ""]
    elif fmt == "md":
        lines.append(f"# {title}\n")
        for c in items:
            lines.append(f"**[{fmt_ts(c.get('BeginSec', 0), False)} → "
                         f"{fmt_ts(c.get('EndSec', 0), False)}]** {c.get('Text', '')}  ")
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


def resolve_subs(z: Zju, a) -> list[dict]:
    if a.course:
        subs = z.course_subs(a.course)
        if a.sub:
            subs = [s for s in subs if s["sub_id"] in a.sub]
        return subs
    days = a.days or 1
    today = dt.date.today()
    subs = []
    for i in range(days):
        subs += z.day_subs(today - dt.timedelta(days=i))
    return subs


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
    z = Zju()
    z.login(*get_credentials())
    ok = z._token(silent=True) is not None
    print(f"登录成功：{user}；智云课堂 token {'OK' if ok else '缺（classroom 指令可能失败）'}")


def cmd_courses(a):
    z = Zju()
    cs = z.courses()
    if a.json:
        print(json.dumps(cs, ensure_ascii=False, indent=1))
        return
    sem = z.semesters()
    for c in (cs if a.all else current_year(cs)):
        teachers = ",".join(i["name"] for i in c.get("instructors") or [])
        print(f"{c['id']}\t{sem.get(c.get('semester_id'), '-')}\t{c['name']}\t{teachers}")


def cmd_sync(a):
    z = Zju()
    courses = match_courses(z.courses(), a.course, a.all)
    if not courses:
        raise ZjuError("没有符合的课程（用 courses --all 看 id）")
    root = Path(a.out).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    man = Manifest(root)
    new = skipped = failed = media = total = 0
    big: list[str] = []
    jobs: list[tuple] = []
    pending: list[str] = []
    dup = {n for n in (c["name"] for c in courses) if [x["name"] for x in courses].count(n) > 1}
    for c in courses:
        # 同名课程（不同班）分开放，免得同名档互盖
        cdir = root / safe_name(f"{c['name']} ({c['id']})" if c["name"] in dup else c["name"])
        items = z.uploads(c["id"])
        log(f"== {c['name']}（{len(items)} 个档）")
        # 同名不同档：全部加 id，命名不依 API 返回顺序（顺序变了也不会重新下载、不会互换）
        uids_by_name: dict[str, set] = {}
        for _, u in items:
            uids_by_name.setdefault(safe_name(u.get("name") or str(u["id"])), set()).add(u["id"])
        seen_keys: set[str] = set()
        for a_, u in items:
            act = a_.get("title", "")
            uid, rid = u["id"], u.get("reference_id") or u["id"]
            key = f"{c['id']}:{uid}"
            if key in seen_keys:  # 同一档同时挂在活动和作业
                continue
            seen_keys.add(key)
            name = safe_name(u.get("name") or str(uid))
            if len(uids_by_name[name]) > 1:
                stem, dot, ext = name.rpartition(".")
                name = f"{stem} ({uid}).{ext}" if dot else f"{name} ({uid})"
            rec = man.get(key)
            if rec and (root / rec["path"]).exists():
                skipped += 1
                continue
            size = u.get("size") or 0
            if not a.videos and Path(name).suffix.lower() in MEDIA_EXT:
                media += 1
                continue
            if a.max_size and size > a.max_size * 2**20:
                big.append(f"{c['name']}/{name}  {size / 2**20:.0f}MB")
                continue
            if a.dry_run:
                print(f"[会下载] {c['name']}/{name}  {size / 2**20:.1f}MB  ({act})")
                new += 1
                total += size
                continue
            jobs.append((key, uid, rid, cdir / name, a_))

    limit = a.max_size * 2**20 if a.max_size else None

    def fetch(job):
        key, uid, rid, dest, a_ = job
        r, src = z.upload_response(uid, rid, a_)
        return stream_to(r, dest, limit), src  # API 没给 size 的档，靠 Content-Length 把关

    # 多档并行：单条连接常被服务器限速，并行吃满频宽；manifest 只在主执行绪写
    with ThreadPoolExecutor(max_workers=max(1, a.jobs)) as pool:
        futs = {pool.submit(fetch, j): j for j in jobs}
        for f in as_completed(futs):
            key, uid, rid, dest0, a_ = futs[f]
            act = a_.get("title", "")
            try:
                dest, src = f.result()
                man.put(key, {"path": str(dest.relative_to(root)), "rid": rid, "source": src,
                              "activity": act, "at": dt.datetime.now().isoformat(timespec="seconds")})
                new += 1
                print(f"[{src}] {dest.relative_to(root)}", flush=True)
            except TooBig as e:
                big.append(f"{dest0.parent.name}/{dest0.name}  {e}")
            except Exception as e:
                if a_.get("is_started") is False and 403 in getattr(e, "codes", ()):
                    # 排程未开放且所有来源皆回 403：开放后下次 sync 自动抓
                    pending.append(f"{dest0.parent.name}/{dest0.name}（{local_time(a_.get('start_time'))} 开放）")
                    continue
                failed += 1
                log(f"[失败] {dest0.name}: {e}")
    for p_ in pending:
        log(f"[未开放] {p_}")
    for b in big:
        log(f"[太大跳过] {b}")
    if big:
        log(f"  → {len(big)} 个档超过 {a.max_size}MB，要抓就指定课程加 --max-size 0")
    extra = f"、未开放 {len(pending)}" if pending else ""
    extra += f"、影音跳过 {media}（加 --videos 才抓）" if media else ""
    size_s = f"（约 {total / 2**20:.0f}MB）" if a.dry_run else ""
    log(f"{'预览' if a.dry_run else '完成'}：{'待下载' if a.dry_run else '新增'} {new}{size_s}、已有 {skipped}、失败 {failed}{extra} → {root}")
    if failed:
        sys.exit(2)


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


def cmd_forum(a):
    z = Zju()
    if a.action == "list":
        uid = z.user_id() if a.mine else None
        for t in z.topics(z.forum_category(a.id)):
            by = t.get("created_by") or {}
            if uid and by.get("id") != uid:
                continue
            print(f"{t['id']}\t{local_time(t.get('created_at'))}\t{by.get('name', '')}\t"
                  f"回复 {t.get('reply_count', 0)}\t{t.get('title', '')}")
            if a.full:
                print("  " + html_to_text(t.get("content")).replace("\n", "\n  "))
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


def cmd_classroom(a):
    z = Zju()
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
            print(f"{c.get('course_id')}\t{c.get('title')}\t{c.get('realname')}")
    elif a.action == "subs":
        if not a.arg:
            raise ZjuError("用法：classroom subs <course_id>")
        for s in z.course_subs(int(a.arg)):
            print(f"{s['sub_id']}\t{s['sub_name']}\t{s['lecturer']}")
    elif a.action == "day":
        start = dt.date.fromisoformat(a.arg) if a.arg else dt.date.today()
        for i in range(a.days or 1):
            d = start - dt.timedelta(days=i)
            for s in z.day_subs(d):
                print(f"{d}\t{s['course_id']}\t{s['sub_id']}\t{s['course_name']}\t{s['sub_name']}\t{s['lecturer']}")


def cmd_classroom_sync(a):
    z = Zju()
    courses = z.classroom_courses()
    if a.course:
        selected = []
        for selector in a.course:
            matched = [c for c in courses if (str(c["course_id"]) == selector if selector.isdigit()
                                             else selector.casefold() in c["title"].casefold())]
            if not matched:
                raise ZjuError(f"没有符合的个人课程：{selector}（用 classroom courses 查看）")
            selected.extend(matched)
        courses = list({c["course_id"]: c for c in selected}.values())
    root = Path(a.out).expanduser()
    failed, subs, seen = 0, [], set()
    for c in courses:
        if c["type"] != "multi":
            log(f"[跳过] {c['title']}：课程类型 {c['type']} 不支持课堂资料同步")
            continue
        try:
            for s in z.course_subs(c["course_id"]):
                key = (s["course_id"], s["sub_id"])
                if key not in seen:
                    seen.add(key)
                    subs.append(s)
        except Exception as e:
            failed += 1
            log(f"[失败] {c['title']}: {e}")
    log(f"[同步] {len(courses)} 门课程、{len(subs)} 堂课，{a.jobs} 个 worker")
    if a.dry_run:
        for s in subs:
            for kind, suffix in (("智云PPT", "pdf"), ("转录", a.format)):
                dest = classroom_material_path(root, s, kind, suffix)
                complete = dest.exists() and (kind != "智云PPT" or dest.with_suffix(".json").exists())
                status = "跳过" if complete and not a.force else "待检查并下载"
                print(f"[{status}] {dest.relative_to(root)}")
        if a.recording:
            failed += recording_subs(z, a, root, subs)
        if getattr(a, "recording_audio", False):
            failed += audio_subs(z, a, root, subs)
    else:
        # 主线程负责组织材料，worker 只下载单个文件或录播分片；避免嵌套线程池与 -j 倍增。
        # 转写、PPT、录播依次处理，整个同步复用同一线程池。
        with ThreadPoolExecutor(max_workers=a.jobs) as pool:
            futures = {pool.submit(transcript_one, z, a, root, s): s for s in subs}
            for future in as_completed(futures):
                s = futures[future]
                try:
                    future.result()
                except Exception as e:
                    failed += 1
                    log(f"[失败] 转录 {s['course_name']} {s['sub_name']}: {e}")
            for s in subs:
                try:
                    ppt_one(z, a, root, s, pool=pool)
                except Exception as e:
                    failed += 1
                    log(f"[失败] PPT {s['course_name']} {s['sub_name']}: {e}")
            if a.recording:
                failed += recording_subs(z, a, root, subs, pool=pool)
        if getattr(a, "recording_audio", False):
            failed += audio_subs(z, a, root, subs)
    log(f"{'预览' if a.dry_run else '同步完成'}：{len(subs)} 堂课，{'有失败' if failed else '无失败'}")
    if failed:
        sys.exit(2)


def cmd_ppt(a):
    z = Zju()
    root = Path(a.out).expanduser()
    subs = resolve_subs(z, a)
    if not subs:
        log("没有课堂")
        return
    failed = 0
    for s in subs:
        try:
            ppt_one(z, a, root, s)
        except Exception as e:  # 一堂坏掉不拖垮其他堂
            failed += 1
            log(f"[失败] {s['course_name']} {s['sub_name']}: {e}")
    if failed:
        sys.exit(2)


def ppt_one(z: Zju, a, root: Path, s: dict, pool: ThreadPoolExecutor | None = None):
    pdf = classroom_material_path(root, s, "智云PPT", "pdf")
    cdir = pdf.parent
    mapping_path = pdf.with_suffix(".json")
    if pdf.exists() and mapping_path.exists() and not a.force:
        log(f"[跳过] {pdf.relative_to(root)}")
        return
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
        if a.keep_images:  # 留全部原图，去重只影响 PDF
            shutil.copytree(tmpdir, cdir / pdf.stem, dirs_exist_ok=True)
        mapping_tmp = mapping_path.with_name(f".part-{mapping_path.name}")
        try:
            mapping_tmp.write_text(json.dumps(timeline, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            os.replace(mapping_tmp, mapping_path)
        finally:
            mapping_tmp.unlink(missing_ok=True)
        missing_times = sum(event["created_sec"] is None for event in events)
        if missing_times:
            log(f"[注意] {missing_times} 个截图事件无有效时间，映射中保留为 null")
        note = f"，去重前 {len(paths)}" if len(pages) != len(paths) else ""
        print(f"[PDF] {pdf.relative_to(root)}（{len(pages)} 页{note}）")
        print(f"[PPT事件映射] {mapping_path.relative_to(root)}（{len(events)} 个事件）")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def cmd_transcript(a):
    z = Zju()
    root = Path(a.out).expanduser()
    failed = 0
    for s in resolve_subs(z, a):
        try:
            transcript_one(z, a, root, s)
        except Exception as e:
            failed += 1
            log(f"[失败] {s['course_name']} {s['sub_name']}: {e}")
            continue
    if failed:
        sys.exit(2)


def transcript_one(z: Zju, a, root: Path, s: dict):
    out = classroom_material_path(root, s, "转录", a.format)
    if out.exists() and not a.force:
        log(f"[跳过] {out.relative_to(root)}")
        return
    items = z.subtitle(s["sub_id"])
    if not items:
        log(f"[无转录] {s['course_name']} {s['sub_name']}")
        return
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_transcript(items, a.format, f"{s['course_name']} {s['sub_name']}"), encoding="utf-8")
    print(f"[转录] {out.relative_to(root)}（{len(items)} 段）")


def cmd_recording_audio(a):
    z = Zju()
    if audio_subs(z, a, Path(a.out).expanduser(), resolve_subs(z, a)):
        sys.exit(2)


def audio_subs(z: Zju, a, root: Path, subs: list[dict]) -> int:
    if not subs:
        log("没有课堂")
        return 0
    man, catalogues = Manifest(root), {}
    failed = completed = skipped = planned = unavailable = 0
    limit = a.max_size * 2**20 if a.max_size else None

    def complete(path, key):
        rec = man.get(key)
        return (path.is_file() and path.stat().st_size > 0
                and (not rec or path.stat().st_size == rec.get("size")))

    for s in subs:
        if s.get("show") == "no":
            skipped += 1
            log(f"[已下架，跳过] 录播音轨 {s['course_name']} {s['sub_name']}")
            continue
        cid, sid = s["course_id"], s["sub_id"]
        video = classroom_material_path(root, s, "录播", "mp4")
        single = classroom_material_path(root, s, "音频", "m4a")
        if not a.force and complete(single, f"audio:{cid}:{sid}:1"):
            skipped += 1
            log(f"[跳过] {single.relative_to(root)}")
            continue
        if complete(video, f"video:{cid}:{sid}:1"):
            sources = [(1, "", None, video)]
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
                    sources.append((i, suffix, url, path if complete(path, f"video:{cid}:{sid}:{i}") else None))
            except ZjuError as e:
                failed += 1
                log(f"[失败] 音频 {s['course_name']} {s['sub_name']}: {e}")
                continue
        for i, suffix, url, video in sources:
            dest = classroom_material_path(root, s, "音频", "m4a", suffix)
            key = f"audio:{cid}:{sid}:{i}"
            if not a.force and complete(dest, key):
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
                make_audio(url, video, dest, a.jobs, limit)
                man.put(key, {"path": str(dest.relative_to(root)), "size": dest.stat().st_size,
                              "course_id": cid, "sub_id": sid,
                              "source": "local" if video is not None else "remote",
                              "at": dt.datetime.now().isoformat(timespec="seconds")})
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


def cmd_recording(a):
    z = Zju()
    root = Path(a.out).expanduser()
    subs = resolve_subs(z, a)
    if recording_subs(z, a, root, subs):
        sys.exit(2)


def recording_subs(z: Zju, a, root: Path, subs: list[dict], pool: ThreadPoolExecutor | None = None) -> int:
    if not subs:
        log("没有课堂")
        return 0
    # 同一课程只读一次目录；完整下载成功后才记入清单。
    catalogues = {}
    man = Manifest(root)
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
            rec = man.get(key)
            if not a.force and rec and dest.is_file() and dest.stat().st_size == rec.get("size"):
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
                download_video(z, url, dest, limit, a.jobs, restart=a.force, **kwargs)
                man.put(key, {"path": str(dest.relative_to(root)), "size": dest.stat().st_size,
                              "course_id": cid, "sub_id": sid,
                              "at": dt.datetime.now().isoformat(timespec="seconds")})
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


def main():
    p = argparse.ArgumentParser(prog="zju.py", description="学在浙大 / 智云课堂 CLI")
    try:
        default_out = os.environ.get("ZJU_OUT") or load_config().get("out") or str(DEFAULT_OUT)
    except ZjuError as e:
        log(f"错误：{e}")
        sys.exit(1)
    p.add_argument("--out", default=default_out, help=f"输出根目录（目前 {default_out}；config.json 的 out 或 ZJU_OUT 可改）")
    sp = p.add_subparsers(dest="cmd", required=True)

    x = sp.add_parser("login", help="设置学号并把密码存进 Keychain")
    x.add_argument("username", nargs="?")
    x.add_argument("--reset", action="store_true", help="重设 Keychain 密码")
    x.set_defaults(fn=cmd_login)

    x = sp.add_parser("courses", help="列出学在浙大课程")
    x.add_argument("--all", action="store_true", help="含往年课程（默认只列最新学年）")
    x.add_argument("--json", action="store_true")
    x.set_defaults(fn=cmd_courses)

    x = sp.add_parser("sync", help="增量同步课件")
    x.add_argument("course", nargs="*", help="课程 id 或名称片段；省略 = 最新学年所有课程")
    x.add_argument("--all", action="store_true", help="没指定课程时抓全部学年")
    x.add_argument("--dry-run", action="store_true")
    x.add_argument("--videos", action="store_true", help="包括学在浙大的音视频附件（默认跳过）")
    x.add_argument("-j", "--jobs", type=int, default=4, help="并行下载数（默认 4）")
    x.add_argument("--max-size", type=int, default=200, metavar="MB", help="单文件上限，超过只列出（默认 200，0 = 不限）")
    x.set_defaults(fn=cmd_sync)

    x = sp.add_parser("todo", help="待办事项")
    x.add_argument("--json", action="store_true")
    x.set_defaults(fn=cmd_todo)

    x = sp.add_parser("activities", help="列出课程活动（课件／视频／作业／讨论／测验…）")
    x.add_argument("course", nargs="*", help="课程 id 或名称片段；省略 = 最新学年所有课程")
    x.add_argument("--all", action="store_true", help="没指定课程时含往年课程")
    x.add_argument("--type", nargs="*", metavar="T", help=f"只列这些类型：{', '.join(ACT_TYPES)}")
    x.add_argument("--json", action="store_true")
    x.set_defaults(fn=cmd_activities)

    x = sp.add_parser("show", help="单一活动详情（说明、附件、作业提交状态、讨论帖数）")
    x.add_argument("activity", type=int)
    x.set_defaults(fn=cmd_show)

    def body_args(x):
        x.add_argument("--body", help="内容（纯文字，空行分段）")
        x.add_argument("--body-file", help="从文件读内容；- = stdin")
        x.add_argument("--html", action="store_true", help="内容已经是 HTML，不转换")
        x.add_argument("--attach", nargs="*", metavar="FILE", help="附件")

    x = sp.add_parser("forum", help="讨论区：list / read / post / reply")
    fp = x.add_subparsers(dest="action", required=True)
    y = fp.add_parser("list", help="列出讨论帖（给讨论活动 id）")
    y.add_argument("id", type=int, help="讨论活动 id（activities --type forum 查）")
    y.add_argument("--mine", action="store_true", help="只看自己发的")
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

    x = sp.add_parser("upload", help="上传文件到学在浙大，输出 upload id")
    x.add_argument("files", nargs="+")
    x.set_defaults(fn=cmd_upload)

    x = sp.add_parser("submit", help="交作业（附件＋文字），默认送出前确认")
    x.add_argument("activity", type=int, help="作业活动 id（activities --type homework 查）")
    x.add_argument("--file", nargs="*", metavar="FILE", help="要交的文件")
    x.add_argument("--upload-id", type=int, nargs="*", help="已用 upload 指令传好的文件 id")
    x.add_argument("--body", help="作业文字内容")
    x.add_argument("--body-file", help="从文件读作业文字；- = stdin")
    x.add_argument("--html", action="store_true", help="文字已经是 HTML")
    x.add_argument("--draft", action="store_true", help="只存草稿不正式提交")
    x.add_argument("-y", "--yes", action="store_true", help="不确认直接送出")
    x.set_defaults(fn=cmd_submit)

    x = sp.add_parser("classroom", help="智云课堂：courses / sync / search / subs / day")
    x.set_defaults(fn=cmd_classroom)
    cp = x.add_subparsers(dest="action", required=True)
    y = cp.add_parser("courses", help="列出全部个人课程及任务数")
    y.add_argument("--has-tasks", action="store_true", help="只列任务数大于 0 的课程")
    y.add_argument("--json", action="store_true", help="输出 JSON")
    y = cp.add_parser("search", help="按关键词搜索智云课程")
    y.add_argument("arg", nargs="?", help="课程名称关键词")
    y.add_argument("--teacher", help="教师名称")
    y = cp.add_parser("subs", help="列出课程堂次")
    y.add_argument("arg", type=int, help="智云课程 ID")
    y = cp.add_parser("day", help="列出某天或最近 N 天的个人课堂")
    y.add_argument("arg", nargs="?", help="日期 YYYY-MM-DD，默认今天")
    y.add_argument("--days", type=int)
    y = cp.add_parser("sync", help="同步个人课程的 PPT、转写及可选录播、音频",
                      description="所有文件、录播分片和音频请求共用 -j 并发上限；默认下载 PPT 和 Markdown 转写。")
    y.add_argument("course", nargs="*", help="智云课程 ID 或名称片段；省略 = 全部个人课程")
    y.add_argument("-j", "--jobs", type=int, default=4, help="下载 worker 总数（默认 4）")
    y.add_argument("--recording", action="store_true", help="同时下载录播 MP4（支持断点续传）")
    y.add_argument("--recording-audio", action="store_true", help="同时获取 M4A 音频；优先从已下载录播提取")
    y.add_argument("--dry-run", action="store_true", help="预览资料路径，不下载、不写文件")
    y.add_argument("--force", action="store_true", help="重新下载已有资料，丢弃录播分片进度")
    y.add_argument("--format", choices=["md", "txt", "srt"], default="md", help="转写格式（默认 md）")
    y.add_argument("--dedup", action="store_true", help="合并 PPT 时去除重复截图")
    y.add_argument("--keep-images", action="store_true", help="保留全部 PPT 原始截图")
    y.add_argument("--max-size", type=int, default=0, metavar="MB", help="录播或音频单文件上限（默认 0 = 不限）")
    y.set_defaults(fn=cmd_classroom_sync)

    for name, fn in (("ppt", cmd_ppt), ("transcript", cmd_transcript), ("recording", cmd_recording), ("recording-audio", cmd_recording_audio)):
        x = sp.add_parser(name, help={"ppt": "智云 PPT → PDF", "transcript": "智云课堂语音转录",
                                      "recording": "智云录播 → MP4", "recording-audio": "智云录播音轨 → M4A"}[name])
        x.add_argument("--course", type=int, help="智云课堂 course_id（classroom courses / search 查）")
        x.add_argument("--sub", type=int, nargs="*", help="只抓这些 sub_id")
        x.add_argument("--days", type=int, help="不给 --course 时：最近 N 天的课（默认 1 = 今天）")
        x.add_argument("--force", action="store_true", help="已存在也重新下载")
        if name == "ppt":
            x.add_argument("--keep-images", action="store_true")
            x.add_argument("--dedup", action="store_true",
                           help="删掉重复截图（动画逐步出现、边讲边写、翻回前页），每页只留最完整的一张")
        elif name == "transcript":
            x.add_argument("--format", choices=["txt", "srt", "md"], default="txt")
        else:
            x.add_argument("--dry-run", action="store_true", help="预览待处理音频" if name == "recording-audio" else "只列出待下载录播")
            x.add_argument("--max-size", type=int, default=0, metavar="MB", help="单文件上限（默认 0 = 不限）")
            x.add_argument("-j", "--jobs", type=int, default=32 if name == "recording-audio" else 4,
                           help="音频并发请求数（默认 32 个协程）" if name == "recording-audio" else "每堂录播的并行分片连接数（默认 4）")
            x.description = ("优先从完整本地录播提取；否则只下载 MP4 音频范围。无需 ffmpeg，不重新编码。"
                             if name == "recording-audio" else "默认沿用已完成分片，重新执行即可续传；--force 丢弃分片并从头下载。")
        x.set_defaults(fn=fn)

    a = p.parse_args()
    if a.cmd == "classroom" and a.action == "sync" and (a.jobs < 1 or a.max_size < 0):
        p.error("--jobs 必须 >= 1，--max-size 必须 >= 0")
    if a.cmd in ("recording", "recording-audio") and (a.max_size < 0 or a.jobs < 1 or (a.days is not None and a.days < 1)):
        p.error("--max-size 必须 >= 0，--jobs 和 --days 必须 >= 1")
    if a.cmd in ("recording", "recording-audio") and a.sub is not None and not a.course:
        p.error("--sub 需要搭配 --course")
    try:
        a.fn(a)
    except ZjuError as e:
        log(f"错误：{e}")
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
