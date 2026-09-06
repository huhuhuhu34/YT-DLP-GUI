# -*- coding: utf-8 -*-
"""
downloader.py —— yt-dlp 下载核心封装（纯 Python，不依赖 Qt，可安全放进后台线程）

职责：
  1. 解析 URL，抽取可用的清晰度/格式列表（分辨率、编码、大小等）
  2. 依序下载“批量队列”中的多个 URL
  3. 把 yt-dlp 的日志与进度通过“回调函数”抛给上层（上层再经 Qt 信号回主线程）
  4. 检测系统 ffmpeg、组装 cookiesfrombrowser 参数、把常见异常翻译成友好提示

回调约定（全部在后台线程中被调用，这里绝不能刷新任何 UI）：
  log_cb(level: str, message: str)      level ∈ debug / info / warning / error
  progress_cb(payload: dict)
"""
from __future__ import annotations

import os
import re
import shutil
import sys
import threading
from typing import Callable, Optional

import yt_dlp
from yt_dlp.utils import DownloadCancelled  # yt-dlp 官方提供的“取消下载”异常

import ytdlp_cli  # 外部 yt-dlp.exe 命令行后端（界面手动指定 exe 时启用）

# 浏览器界面显示名 -> yt-dlp cookiesfrombrowser 内部名称
BROWSER_MAP = {
    "Chrome": "chrome",
    "Edge": "edge",
    "Firefox": "firefox",
    "Brave": "brave",
    "Opera": "opera",
    "Vivaldi": "vivaldi",
}

# 默认“最佳画质 + 音频自动合并”的 yt-dlp format 表达式
FORMAT_AUTO_BEST = "bestvideo*+bestaudio/best"
# 仅下载最佳音频
FORMAT_AUDIO_ONLY = "bestaudio/best"

# yt-dlp 输出文件名模板
OUTTMPL = "%(title)s [%(id)s].%(ext)s"

try:
    # 内置 yt_dlp 库版本号（界面“后端状态”需要显示）
    YTDLP_VERSION = yt_dlp.version.__version__
except Exception:
    YTDLP_VERSION = "?"

# 回调类型别名
LogCB = Callable[[str, str], None]
ProgressCB = Callable[[dict], None]

# 用于清除 yt-dlp 消息里的 ANSI 颜色码（否则会污染日志区显示）
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _clean_message(message) -> str:
    """清理 yt-dlp 消息：去掉 ANSI 颜色码与回车符。"""
    text = str(message)
    text = _ANSI_RE.sub("", text)
    return text.replace("\r", "")


def human_bytes(num) -> str:
    """字节数 -> 人类可读字符串（B / KiB / MiB / GiB）。"""
    num = float(num or 0)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(num) < 1024.0 or unit == "TiB":
            return f"{num:.0f}{unit}" if unit == "B" else f"{num:.1f}{unit}"
        num /= 1024.0
    return "0B"


def format_duration(seconds) -> str:
    """把秒数格式化成 3:25 或 1:02:03 形式；为空返回空串。"""
    if not seconds:
        return ""
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _find_ffmpeg_in_dir(base_dir: str) -> Optional[str]:
    """在某个目录下按常见结构寻找 ffmpeg.exe（深度受限，不做全盘扫描）。"""
    if not base_dir or not os.path.isdir(base_dir):
        return None
    for entry in os.listdir(base_dir):
        sub = os.path.join(base_dir, entry)
        if not os.path.isdir(sub):
            continue
        for sub2 in os.listdir(sub):
            exe = os.path.join(sub, sub2, "bin", "ffmpeg.exe")
            if os.path.isfile(exe):
                return exe
    return None


def find_ffmpeg() -> Optional[str]:
    """检测 ffmpeg 可执行文件，返回其完整路径；找不到返回 None。

    检测顺序：
      1) PyInstaller 打包后随包自带的 ffmpeg.exe（_MEIPASS 解压目录 或 exe 同目录）
      2) 系统 PATH（shutil.which）
      3) winget 默认安装目录（%LOCALAPPDATA%/Microsoft/WinGet/Packages/Gyan.FFmpeg*）
         —— 解决用 winget 装的 ffmpeg 在当前会话 PATH 未刷新的情况
    """
    if getattr(sys, "frozen", False):  # 已被 PyInstaller 冻结运行
        bases = []
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            bases.append(meipass)                      # --onefile 的运行时解压目录
        bases.append(os.path.dirname(sys.executable))  # exe 所在目录
        for base in bases:
            exe = os.path.join(base, "ffmpeg.exe")
            if os.path.isfile(exe):
                return exe
    which = shutil.which("ffmpeg")
    if which:
        return which
    # winget 兜底：很多情况下 winget 装的 ffmpeg 需要新开会话 PATH 才生效
    winget_root = os.path.join(os.environ.get("LOCALAPPDATA", ""),
                               "Microsoft", "WinGet", "Packages")
    if os.path.isdir(winget_root):
        for vendor_dir in os.listdir(winget_root):
            if vendor_dir.lower().startswith("gyan.ffmpeg"):
                found = _find_ffmpeg_in_dir(os.path.join(winget_root, vendor_dir))
                if found:
                    return found
    return None


def resolve_ffmpeg(preferred: Optional[str] = None) -> Optional[str]:
    """定位 ffmpeg：手动指定的 exe 优先，其次才走自动检测。

    手动路径无效（文件不存在/被删）时静默回退自动检测，不抛异常。
    返回可执行文件完整路径，找不到返回 None。
    """
    if preferred and os.path.isfile(preferred):
        return os.path.abspath(preferred)
    return find_ffmpeg()


def browser_profile_roots(browser_display: str) -> list:
    """返回某浏览器存放 Profile 的根目录（Windows；目录存在才返回）。

    Chrome / Edge / Brave / Vivaldi 返回其 User Data 目录；
    Firefox 返回其 Profiles 目录；Opera 与未知名称返回空列表。
    """
    name = BROWSER_MAP.get(browser_display or "", "")
    if not name:
        return []
    local = os.environ.get("LOCALAPPDATA") or ""
    appdata = os.environ.get("APPDATA") or ""
    rel = {
        "chrome": (local, "Google", "Chrome", "User Data"),
        "edge": (local, "Microsoft", "Edge", "User Data"),
        "brave": (local, "BraveSoftware", "Brave-Browser", "User Data"),
        "vivaldi": (local, "Vivaldi", "User Data"),
        "firefox": (appdata, "Mozilla", "Firefox", "Profiles"),
    }.get(name)
    if not rel or not rel[0]:
        return []
    root = os.path.join(rel[0], *rel[1:])
    return [root] if os.path.isdir(root) else []


def list_browser_profiles(browser_display: str) -> list:
    """扫描已安装的浏览器 Profile 目录，返回存在的绝对路径列表。

    Chrome 系：User Data 下名为 Default 或以 "Profile " 开头的目录
    （排除 Guest Profile / System Profile 等系统目录）；
    Firefox：Profiles 目录下的所有子目录（形如 xxx.default-release）；
    Opera 不支持 Profile —— 恒返回空列表。
    """
    name = BROWSER_MAP.get(browser_display or "", "")
    if name == "opera":
        return []
    found: list = []
    for root in browser_profile_roots(browser_display):
        try:
            entries = sorted(os.listdir(root))
        except OSError:
            continue
        for entry in entries:
            if entry in ("Guest Profile", "System Profile", "Default Apps"):
                continue
            path = os.path.join(root, entry)
            if not os.path.isdir(path):
                continue
            if name == "firefox" or entry == "Default" or entry.startswith("Profile "):
                found.append(path)
    return found


def make_cookies_cfg(browser_display: str, profile: str) -> Optional[tuple]:
    """把界面选择转成 yt-dlp 的 cookiesfrombrowser 参数。

    yt-dlp 期望格式：(browser_name, profile, keyring, container)
    浏览器名必须是其内部名（chrome/edge/firefox/brave/opera/vivaldi）。
    当选择“不使用”或名字无效时返回 None —— 调用方此时**不要**传该参数。
    """
    name = BROWSER_MAP.get((browser_display or "").strip())
    if not name:
        return None
    profile = (profile or "").strip()
    # profile 留空 -> None，表示使用该浏览器的默认 Profile
    return (name, profile or None, None, None)


def make_logger(log_cb: LogCB):
    """构造符合 yt-dlp logger 协议的对象（debug/info/warning/error 方法）。

    重要：yt-dlp 设置了 logger 后，普通过程提示(to_screen)会以 debug 级别
    发给 logger，所以 debug 也要显示到日志区，否则会漏掉大量下载过程信息。
    """
    class _YdlLogger:
        def _send(self, level: str, message) -> None:
            text = _clean_message(message)
            if not text:
                return
            head = text.lstrip().lower()
            # 部分消息仍以 ERROR:/WARNING: 前缀进入 debug 通道，这里再归一次级
            if level == "debug" and head.startswith(("error", "warning")):
                level = "error" if head.startswith("error") else "warning"
            log_cb(level, text)

        def debug(self, message):
            self._send("debug", message)

        def info(self, message):
            self._send("info", message)

        def warning(self, message):
            self._send("warning", message)

        def error(self, message):
            self._send("error", message)

    return _YdlLogger()

# ---------------------------------------------------------------------------
# 异常 -> 友好中文提示
# ---------------------------------------------------------------------------
def friendly_error(exc) -> str:
    """把异常翻译成面向用户的友好提示；无法归类的异常保留原文，避免丢信息。"""
    if isinstance(exc, DownloadCancelled):
        return "下载已被用户取消"
    text = (str(exc) or "").strip()
    low = text.lower()
    name = type(exc).__name__

    table = (
        (("unsupported url", "no suitable extractor", "not a valid url"),
         "无法识别的链接：该网站目前不受 yt-dlp 支持，或链接格式不正确。"),
        (("failed to load cookies", "could not find cookies", "couldn't find cookies",
          "cookie database", "cookie db", "browser is not installed", "browser not found"),
         "读取浏览器 Cookies 失败：可能原因有：浏览器未安装/版本过旧、Profile 名填错、"
         "或 Cookie 数据库正被浏览器占用。请换一个 Cookies 来源重试，或先彻底关闭浏览器再试。"),
        (("ffmpeg is not installed", "ffprobe was not found", "ffprobe not found",
          "ffmpeg not found", "no such file or directory: 'ffmpeg'"),
         "未找到 ffmpeg/ffprobe：当前选项需要合并音视频流，请先安装 ffmpeg"
         "（见程序界面顶部的下载链接），安装后重启本程序。"),
        (("sign in to confirm", "confirm you're not a bot", "recaptcha", "captcha"),
         "站点要求真人验证（常见于 IP 风控）：请稍后重试，或在 Cookies 来源中"
         "选择你已登录的浏览器后重试。"),
        (("private video", "this video is private", "members only", "premium content"),
         "该视频为私享 / 会员专属内容：请通过 Cookies 来源选择已登录的浏览器账号后重试。"),
        (("video unavailable", "this video is unavailable"),
         "该视频当前不可用（可能被删除、区域限制或版权下架）。"),
        (("http error 403",), "服务器返回 HTTP 403（拒绝访问）：可能需要登录 Cookies，或稍后重试。"),
        (("http error 404",), "服务器返回 HTTP 404（找不到资源）：链接可能已失效。"),
        (("timed out", "timeout"), "网络请求超时：请检查网络连接后重试。"),
        (("connection refused", "connection reset", "connection aborted", "connection error"),
         "网络连接被中断/拒绝：请检查网络与代理设置后重试。"),
        (("maximum number of retries", "gave up after"), "重试次数用尽仍失败：请稍后重试。"),
    )
    for keys, hint in table:
        if any(k in low for k in keys):
            return hint
    return f"{name}: {text}" if text else name

# ---------------------------------------------------------------------------
# 格式列表整理
# ---------------------------------------------------------------------------
def _short_codec(codec: Optional[str]) -> str:
    """把 avc1.640028 / mp4a.40.2 / vp09.00.10.08 压成短名 avc1 / mp4a / vp09。"""
    codec = codec or ""
    return codec.split(".")[0]


def describe_formats(info: dict) -> list:
    """把 extract_info 返回的 formats 整理成用户可读的下拉选项列表。

    每个元素（dict）：
      format_id      原始 format_id
      expression     真正传给 yt-dlp 的 format 表达式
      kind           both(音视频一体) / video(仅视频流) / audio(仅音频)
      requires_merge 是否需要 ffmpeg 合并
      height/ext/vcodec/acodec/filesize/abr/tbr 等展示字段
      label          下拉框显示文本
    """
    rows = []
    seen_ids = set()
    for f in info.get("formats") or []:
        vcodec = f.get("vcodec") or "none"
        acodec = f.get("acodec") or "none"
        if vcodec == "none" and acodec == "none":
            continue  # 跳过 storyboard/字幕等不可下载的流
        fmt_id = str(f.get("format_id") or "").strip()
        if not fmt_id or fmt_id in seen_ids:
            continue
        seen_ids.add(fmt_id)

        ext = f.get("ext") or "?"
        height = int(f.get("height") or 0)
        tbr = float(f.get("tbr") or 0)      # 总比特率（kbps）
        abr = float(f.get("abr") or 0)      # 音频比特率（kbps）
        filesize = f.get("filesize") or f.get("filesize_approx") or 0

        has_v = vcodec != "none"
        has_a = acodec != "none"
        kind = "both" if (has_v and has_a) else ("video" if has_v else "audio")

        # 分辨率文字：优先取 format_note（如 1080p60），否则用 height
        note = (f.get("format_note") or "").strip()
        if kind != "audio":
            if re.search(r"\d+p", note):
                res = note.split(" ")[0]
            elif height:
                res = f"{height}p"
            else:
                res = f.get("resolution") or "?"
        else:
            res = ""

        size_str = human_bytes(filesize) if filesize else "大小未知"
        if kind == "audio":
            rate = abr or tbr
            head = f"音频 {rate:.0f}kbps" if rate else "仅音频"
            label = f"{head} | {ext} | {_short_codec(acodec)} | {size_str}"
            expression = fmt_id
            requires_merge = False
        elif kind == "both":
            codec = f"{_short_codec(vcodec)}+{_short_codec(acodec)}"
            label = f"{res} | {ext} | {codec} | 单文件含音轨 | {size_str}"
            expression = fmt_id
            requires_merge = False
        else:  # kind == "video"：仅视频流，下载时自动附带最佳音频并合并
            codec = _short_codec(vcodec)
            label = f"{res} | {ext} | {codec} | 仅视频流(自动合并最佳音频) | {size_str}"
            expression = f"{fmt_id}+bestaudio/{fmt_id}"  # 无音频可并时回退到纯视频
            requires_merge = True

        rows.append({
            "format_id": fmt_id,
            "expression": expression,
            "kind": kind,
            "requires_merge": requires_merge,
            "height": height,
            "ext": ext,
            "vcodec": vcodec,
            "acodec": acodec,
            "filesize": filesize or None,
            "tbr": tbr,
            "abr": abr,
            "label": label,
        })

    def sort_key(row):
        # 视频类（含音视频一体与仅视频流）按分辨率/码率从高到低，音频类排最后
        if row["kind"] == "audio":
            return (1, 0, -(row["abr"] or row["tbr"]))
        return (0, -(row["height"] or 0), -row["tbr"])

    rows.sort(key=sort_key)
    return rows

# ---------------------------------------------------------------------------
# URL 解析（不下载）
# ---------------------------------------------------------------------------
def _base_ydl_opts(cookies_cfg, log_cb: Optional[LogCB]) -> dict:
    """公共的 YoutubeDL 参数。"""
    opts = {
        "noprogress": True,   # 关闭 yt-dlp 自带的控制台进度条（进度改由 progress_hooks 上报）
        "quiet": True,        # 设置 logger 后提示已进 logger；quiet 再保险一层
        "no_warnings": False,
        "cachedir": False,    # 不写缓存文件，绿色/便携运行
        "logger": make_logger(log_cb) if log_cb else None,
    }
    if cookies_cfg:
        # cookiesfrombrowser 参数格式：(browser, profile, keyring, container)
        opts["cookiesfrombrowser"] = cookies_cfg
    return opts


def parse_url(url: str, cookies_cfg=None, noplaylist: bool = False,
              log_cb: Optional[LogCB] = None,
              ytdlp_exe: Optional[str] = None) -> dict:
    """解析 URL（不下载任何文件），返回界面所需信息。

    返回字典：url / title / uploader / duration / webpage_url /
              is_playlist / playlist_count / formats(describe_formats 结果)
    当 ytdlp_exe 指向有效的 yt-dlp.exe 时，改用该外部可执行文件解析。
    """
    if ytdlp_exe and os.path.isfile(ytdlp_exe):
        return ytdlp_cli.parse_url_external(ytdlp_exe, url, cookies_cfg=cookies_cfg,
                                            noplaylist=noplaylist, log_cb=log_cb)
    if log_cb:
        log_cb("info", f"开始解析链接：{url}")
    opts = _base_ydl_opts(cookies_cfg, log_cb)
    opts.update({
        "skip_download": True,     # 只解析，不下载
        "noplaylist": bool(noplaylist),
    })
    with yt_dlp.YoutubeDL(opts) as ydl:   # 退出 with 自动调用 ydl.close()
        info = ydl.extract_info(url, download=False) or {}

    is_playlist = info.get("_type") not in (None, "video", "url")
    entries = info.get("entries")
    count = None
    if entries is not None:
        try:
            count = len(entries)
        except Exception:
            count = None  # 某些惰性条目无法直接取长度

    return {
        "url": url,
        "title": info.get("title") or info.get("id") or "(未知标题)",
        "uploader": info.get("uploader") or "",
        "duration": info.get("duration"),
        "webpage_url": info.get("webpage_url") or url,
        "is_playlist": is_playlist,
        "playlist_count": count,
        "formats": [] if is_playlist else describe_formats(info),
    }


# ---------------------------------------------------------------------------
# 队列下载
# ---------------------------------------------------------------------------
def _make_progress_hook(cancel_event: Optional[threading.Event],
                        progress_cb: Optional[ProgressCB],
                        log_cb: Optional[LogCB]):
    """构造 progress_hooks 回调：把 yt-dlp 的进度字典转成 UI 需要的轻量字典。"""
    def hook(d: dict) -> None:
        # 取消检测：置位后抛 DownloadCancelled，让 yt-dlp 干净地中断当前下载
        if cancel_event is not None and cancel_event.is_set():
            raise DownloadCancelled("用户点击了取消按钮")

        downloaded = d.get("downloaded_bytes") or 0
        total = d.get("total_bytes") or d.get("total_bytes_estimate")
        percent = (downloaded * 100.0 / total) if total else None
        info = d.get("info_dict") or {}
        filename = os.path.basename(d.get("filename") or info.get("_filename") or "")

        payload = {
            "status": d.get("status"),   # downloading / finished / error / processing
            "filename": filename,
            "downloaded_bytes": downloaded,
            "total_bytes": total,
            "percent": percent,
            "speed": d.get("speed"),
            "eta": d.get("eta"),
            "_percent_str": (d.get("_percent_str") or "").strip(),
            "_speed_str": (d.get("_speed_str") or "").strip(),
            "_eta_str": (d.get("_eta_str") or "").strip(),
        }
        if progress_cb:
            progress_cb(payload)
    return hook
def run_download_queue(
    urls: list,
    output_dir: str,
    fmt_expression: str,
    cookies_cfg=None,
    noplaylist: bool = False,
    cancel_event: Optional[threading.Event] = None,
    log_cb: Optional[LogCB] = None,
    progress_cb: Optional[ProgressCB] = None,
    ytdlp_exe: Optional[str] = None,
    ffmpeg_exe: Optional[str] = None,
) -> dict:
    """依次下载批量队列。返回统计结果字典 {'total','success','failed','cancelled'}。

    注意：本函数在后台线程运行；主线程通过置位 cancel_event 请求取消。
    当 ytdlp_exe 指向有效的 yt-dlp.exe 时，整条队列自动切换到外部命令行后端；
    ffmpeg_exe 为界面手动指定的 ffmpeg（优先于自动检测）。
    """
    if not urls:
        raise ValueError("下载队列为空")

    if ytdlp_exe and os.path.isfile(ytdlp_exe):
        # 手动指定外部 yt-dlp.exe → 整条队列交给命令行后端处理
        return ytdlp_cli.run_download_queue_external(
            ytdlp_exe, urls, output_dir, fmt_expression,
            cookies_cfg=cookies_cfg,
            noplaylist=noplaylist,
            ffmpeg_exe=ffmpeg_exe,
            cancel_event=cancel_event,
            log_cb=log_cb,
            progress_cb=progress_cb,
        )

    # 输出目录不存在则自动创建
    try:
        os.makedirs(output_dir, exist_ok=True)
    except OSError as exc:
        raise OSError(f"无法创建输出目录：{output_dir}\n原因：{exc}") from exc

    fmt_expression = fmt_expression or FORMAT_AUTO_BEST
    if log_cb:
        log_cb("info", f"输出目录：{output_dir}")
        log_cb("info", f"清晰度/格式表达式：{fmt_expression}")

    # ffmpeg：界面手动指定的优先，否则自动检测（PATH / winget / exe 同目录）
    ffmpeg_path = resolve_ffmpeg(ffmpeg_exe)
    if log_cb:
        if ffmpeg_path:
            src = "手动指定" if ffmpeg_exe else "自动检测"
            log_cb("info", f"已检测到 ffmpeg（{src}）：{ffmpeg_path}")
        else:
            log_cb("warning",
                   "未检测到 ffmpeg：需要“合并音视频”的清晰度将失败。请安装 ffmpeg："
                   "方法一：winget install Gyan.FFmpeg；方法二：https://www.gyan.dev/ffmpeg/builds/ "
                   "下载 essentials 版 zip，把 bin 目录加入系统 PATH 或放到本程序 exe 同目录。装好后重启本程序。")

    opts = _base_ydl_opts(cookies_cfg, log_cb)
    opts.update({
        "format": fmt_expression,
        "outtmpl": os.path.join(output_dir, OUTTMPL),   # 视频标题 [id].扩展名
        "noplaylist": bool(noplaylist),
        "continuedl": True,                  # 断点续传
        "retries": 10,                       # 网络重试次数
        "fragment_retries": 10,              # HLS/DASH 分片重试
        "concurrent_fragment_downloads": 4,  # 分片多线程下载
        "overwrites": False,                 # 已存在的文件自动跳过
        "progress_hooks": [_make_progress_hook(cancel_event, progress_cb, log_cb)],
    })
    if ffmpeg_path:
        # 显式告诉 yt-dlp 用哪个 ffmpeg（覆盖 PATH 找不到、打包自带的场景）
        opts["ffmpeg_location"] = ffmpeg_path

    total = len(urls)
    stats = {"total": total, "success": 0, "failed": 0, "cancelled": False}

    with yt_dlp.YoutubeDL(opts) as ydl:   # 退出 with 自动 close()
        for index, url in enumerate(urls, start=1):
            if cancel_event is not None and cancel_event.is_set():
                stats["cancelled"] = True
                break
            if log_cb:
                log_cb("info", f"===== 第 {index}/{total} 个任务开始：{url}")
            if progress_cb:
                progress_cb({"status": "task_started", "index": index,
                             "total": total, "url": url})
            try:
                # 单个 URL 下载（含内部提取、合并等后处理）。返回 0 = 成功。
                retcode = ydl.download([url])
                if cancel_event is not None and cancel_event.is_set():
                    # 下载过程中用户点击了取消
                    stats["cancelled"] = True
                    if log_cb:
                        log_cb("warning", f"任务已被用户取消：{url}")
                elif retcode == 0:
                    stats["success"] += 1
                    if log_cb:
                        log_cb("info", f"第 {index}/{total} 个任务完成：{url}")
                else:
                    stats["failed"] += 1
                    if log_cb:
                        log_cb("error", f"第 {index}/{total} 个任务失败（详情见上一条错误）：{url}")
            except DownloadCancelled:
                stats["cancelled"] = True
                if log_cb:
                    log_cb("warning", f"任务已被用户取消：{url}")
                break   # 取消后不再继续队列中的下一个 URL
            except Exception as exc:  # 网络错误 / 格式不存在 / cookies 失败等
                stats["failed"] += 1
                if log_cb:
                    log_cb("error", f"第 {index}/{total} 个任务失败：{friendly_error(exc)}")
            finally:
                if progress_cb:
                    progress_cb({"status": "task_finished", "index": index,
                                 "total": total, "url": url,
                                 "cancelled": stats["cancelled"]})

    if log_cb:
        if stats["cancelled"]:
            log_cb("warning", f"队列已取消：成功 {stats['success']}，失败 {stats['failed']}。")
        else:
            log_cb("info", f"队列处理完毕：成功 {stats['success']}，失败 {stats['failed']}。")
    return stats