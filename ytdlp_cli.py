# -*- coding: utf-8 -*-
"""
ytdlp_cli.py —— 外部 yt-dlp.exe 命令行后端（纯 Python，不依赖 Qt，可安全放进后台线程）

用途：
  当用户在界面手动指定了 yt-dlp.exe 的完整路径时，downloader.parse_url /
  downloader.run_download_queue 会把任务路由到本模块：改用 subprocess 调用那个
  exe 完成“解析 / 下载”，从而能用上用户自己下载、可随时更新的 yt-dlp.exe，
  而不受打包进程序的内置 Python yt-dlp 版本限制。未指定外部 exe 时程序维持
  原“内置 Python 库”后端，本模块不会被调用。

与 downloader.py 的接口对应：
    downloader.parse_url(...)          -> ytdlp_cli.parse_url_external(...)
    downloader.run_download_queue(...) -> ytdlp_cli.run_download_queue_external(...)

设计要点：
  * 解析：--dump-single-json（-J）把结果以 JSON 输出到 stdout，复用
    downloader.describe_formats 生成 UI 清晰度下拉项；
  * 进度：--newline 让 yt-dlp 把进度逐行输出，这里逐行解析成与
    downloader._make_progress_hook 同构的 payload，上层 UI 逻辑无需改动；
  * 取消：downloader 的 cancel_event 置位后 terminate 当前子进程
    （配合 -c 断点续传，已下载完的分片不会丢失）；
  * 不闪控制台窗口：PyInstaller --windowed 打包后 subprocess 启动控制台 exe
    会闪现黑框，这里统一加 CREATE_NO_WINDOW。
"""
from __future__ import annotations

import json
import locale
import os
import re
import subprocess
import threading
from typing import Callable, Optional

# 回调类型别名（与 downloader.py 保持一致）
LogCB = Callable[[str, str], None]
ProgressCB = Callable[[dict], None]

# Windows 下“不创建控制台窗口”的创建标志（仅本平台存在该属性）
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# 清除 ANSI 颜色码
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

# [download] 进度行示例：
#   [download]   3.2% of    1.03GiB at    2.34MiB/s ETA 00:12
#   [download] 100% of 34.00MiB in 00:00:02 at 15.67MiB/s ETA 00:00
_PROGRESS_RE = re.compile(
    r"\[download\]\s+(?P<percent>\d+(?:\.\d+)?)%"
    r"(?:\s+of\s+(?P<total>[0-9.]+\s*(?:KiB|MiB|GiB|TiB)))?"
    r"(?:\s+in\s+(?P<elapsed>[0-9:]+))?"
    r"(?:\s+at\s+(?P<speed>[0-9.]+\s*(?:B|KiB|MiB|GiB|TiB)/s))?"
    r"(?:\s+ETA\s+(?P<eta>Unknown|[0-9:]+))?",
    re.IGNORECASE,
)

# 目标文件名行（下载中的中间文件 / 重封装 / 合并产物 / 已存在跳过）
_DEST_RE = re.compile(r"Destination:\s*(.+?)\s*$")
_MERGE_RE = re.compile(r'Merging formats into\s+"?(.+?)"?\s*$')
_ALREADY_RE = re.compile(r"\[download\]\s+(.+?)\s+has already been downloaded\s*$")


def _clean_message(message) -> str:
    """清理单行输出：去 ANSI 颜色码、回车符并去掉首尾空白。"""
    text = _ANSI_RE.sub("", str(message))
    return text.replace("\r", "").strip()


def _system_encoding() -> str:
    """Windows 中文系统默认返回 cp936(GBK) 等 ACP 编码，用于解码回退。"""
    try:
        return locale.getpreferredencoding(False) or "utf-8"
    except Exception:
        return "utf-8"


def _decode_text(raw) -> str:
    """把子进程输出 bytes 解码为 str：先按 UTF-8，失败回退系统编码(GBK)。

    yt-dlp 在 Windows 控制台按系统 ACP 输出，但程序可为子进程注入
    PYTHONIOENCODING=utf-8，因此两种编码都要兼容；中文标题与路径据此保真。
    """
    if isinstance(raw, str):
        return raw
    if not raw:
        return ""
    candidates = ["utf-8"]
    enc = _system_encoding()
    if enc.lower() not in candidates:
        candidates.append(enc)
    for codec in candidates:
        try:
            return raw.decode(codec)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def _decode_line(raw) -> str:
    """解码单行并去除行尾 \\r\\n（GBK / UTF-8 多字节均不含换行，按行切分安全）。"""
    return _decode_text(raw).rstrip("\r\n")


def _log_line(log_cb: Optional[LogCB], line: str) -> None:
    """把一行 yt-dlp 输出按级别转发到日志回调。"""
    if log_cb is None:
        return
    s = _clean_message(line)
    if not s:
        return
    low = s.lstrip().lower()
    if low.startswith(("error:",)):
        log_cb("error", s)
    elif low.startswith(("warning:",)):
        log_cb("warning", s)
    elif low.startswith(("[debug]", "debug:")):
        log_cb("debug", s)
    elif s.startswith("["):
        log_cb("info", s)          # [download] / [Merger] / [youtube] 等标签行
    else:
        log_cb("debug", s)


def _spawn_env() -> dict:
    """子进程环境：额外注入 PYTHONIOENCODING=utf-8（官方 yt-dlp.exe 若生效，
    输出即为 UTF-8，与程序解码优先序一致）。"""
    env = dict(os.environ)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return env


def _popen(args):
    """启动 yt-dlp 子进程（隐藏控制台窗口；stdout/stderr 分开取二进制管道）。"""
    return subprocess.Popen(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_spawn_env(),
        creationflags=_NO_WINDOW,
    )


def _popen_merged(args):
    """启动 yt-dlp 子进程：stderr 并入 stdout 单管道逐行读（避免双管道死锁）。"""
    return subprocess.Popen(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=_spawn_env(),
        bufsize=1,
        creationflags=_NO_WINDOW,
    )


def get_ytdlp_version(exe: str) -> str:
    """运行 `yt-dlp.exe --version` 探测版本号；执行失败返回空串。"""
    try:
        out = subprocess.run(
            [exe, "--version"],
            capture_output=True,
            timeout=60,
            env=_spawn_env(),
            creationflags=_NO_WINDOW,
        )
    except Exception:
        return ""
    text = (_decode_text(out.stdout) or _decode_text(out.stderr)).strip()
    return text.splitlines()[0] if text else ""


def _cookies_from_browser_arg(cookies_cfg) -> Optional[str]:
    """把 downloader.make_cookies_cfg 的四元组转成 CLI 的
    `--cookies-from-browser 浏览器[:Profile]` 参数；不适用时返回 None。"""
    if not cookies_cfg or not isinstance(cookies_cfg, (tuple, list)) or len(cookies_cfg) < 2:
        return None
    browser = str(cookies_cfg[0] or "").strip()
    if not browser:
        return None
    profile = str(cookies_cfg[1] or "").strip() if cookies_cfg[1] else ""
    return f"{browser}:{profile}" if profile else browser


# ---------------------------------------------------------------------------
# URL 解析（--dump-single-json）
# ---------------------------------------------------------------------------
def _parse_cli_args(exe: str, url: str, cookies_cfg, noplaylist: bool,
                    ffmpeg_exe: Optional[str]) -> list:
    """构造“只解析不下载”的外部 yt-dlp 参数列表。"""
    args = [exe, "--no-color", "--no-cache-dir"]
    cfa = _cookies_from_browser_arg(cookies_cfg)
    if cfa:
        args += ["--cookies-from-browser", cfa]
    # 与 downloader 行为对齐：默认单视频；链接疑似播放列表才可整表解析
    args += ["--no-playlist"] if noplaylist else ["--yes-playlist"]
    # 疑似播放列表时用 flat 模式，避免把整表完整信息（超大 JSON）拉回内存
    low = (url or "").lower()
    if "playlist" in low or "list=" in low:
        args.append("--flat-playlist")
    if ffmpeg_exe and os.path.isfile(ffmpeg_exe):
        args += ["--ffmpeg-location", ffmpeg_exe]
    args += ["--dump-single-json", url]
    return args


def _run_capture(proc, log_cb: Optional[LogCB]):
    """并行处理 -J 的子进程输出：
    stdout 按二进制块收集 JSON；stderr 逐行解码并实时转发日志。
    返回 (json_text, err_lines)（均已解码为 str）；调用方检查 proc.returncode。
    """
    json_chunks: list = []
    err_lines: list = []

    def _read_out():
        while True:
            chunk = proc.stdout.read(65536)
            if not chunk:
                break
            json_chunks.append(chunk)

    def _read_err():
        while True:
            line = proc.stderr.readline()
            if not line:
                break
            text = _decode_line(line)
            err_lines.append(text)
            if text:
                _log_line(log_cb, text)

    t_out = threading.Thread(target=_read_out, daemon=True)
    t_err = threading.Thread(target=_read_err, daemon=True)
    t_out.start()
    t_err.start()
    proc.wait()
    t_out.join()
    t_err.join()
    return _decode_text(b"".join(json_chunks)), err_lines


def parse_url_external(exe: str, url: str, cookies_cfg=None,
                       noplaylist: bool = False, ffmpeg_exe: Optional[str] = None,
                       log_cb: Optional[LogCB] = None) -> dict:
    """用外部 yt-dlp.exe 解析 URL，返回与 downloader.parse_url 同构的字典。

    返回字段：url / title / uploader / duration / webpage_url /
              is_playlist / playlist_count / formats(describe_formats 结果)
    """
    from downloader import describe_formats  # 延迟导入避免循环 import

    if log_cb:
        log_cb("info", f"[外部 yt-dlp] 开始解析链接：{url}")

    args = _parse_cli_args(exe, url, cookies_cfg, noplaylist, ffmpeg_exe)
    proc = _popen(args)
    text, err_lines = _run_capture(proc, log_cb)

    if proc.returncode != 0 or not text.strip():
        tail = _clean_message(err_lines[-1]) if err_lines else ""
        raise RuntimeError(tail or f"外部 yt-dlp 解析失败（退出码 {proc.returncode}）。")

    try:
        info = json.loads(text)
    except Exception as exc:
        raise RuntimeError(f"外部 yt-dlp 返回的数据无法解析：{exc}") from exc
    if not isinstance(info, dict):
        raise RuntimeError("外部 yt-dlp 返回的数据结构异常。")

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
# 队列下载（--newline 逐行进度）
# ---------------------------------------------------------------------------
def _download_cli_args(exe: str, url: str, output_dir: str,
                       fmt_expression: str, cookies_cfg, noplaylist: bool,
                       ffmpeg_exe: Optional[str]) -> list:
    """构造“下载单个 URL”的外部 yt-dlp 参数列表。"""
    from downloader import OUTTMPL  # 延迟导入避免循环 import

    outtmpl = os.path.join(output_dir, OUTTMPL)
    args = [
        exe,
        "--no-color",
        "--no-cache-dir",
        "--newline",                     # 进度逐行输出，方便解析
        "--continue",                    # 断点续传（-c）
        "--no-overwrites",
        "--retries", "10",
        "--fragment-retries", "10",
        "--concurrent-fragments", "4",   # HLS / DASH 分片多线程
        "-o", outtmpl,
        "-f", fmt_expression,
    ]
    cfa = _cookies_from_browser_arg(cookies_cfg)
    if cfa:
        args += ["--cookies-from-browser", cfa]
    args += ["--no-playlist"] if noplaylist else ["--yes-playlist"]
    if ffmpeg_exe and os.path.isfile(ffmpeg_exe):
        args += ["--ffmpeg-location", ffmpeg_exe]
    args.append(url)
    return args


def _handle_download_line(line: str, state: dict,
                          log_cb: Optional[LogCB],
                          progress_cb: Optional[ProgressCB]) -> None:
    """处理下载子进程的每一行输出：更新 state 并派发进度/日志。"""
    s = _clean_message(line)
    if not s:
        return
    low = s.lstrip().lower()

    # 1) 明确的错误 / 警告
    if low.startswith(("error:",)):
        state["last_error"] = s
        if log_cb:
            log_cb("error", s)
        return
    if low.startswith(("warning:",)):
        if log_cb:
            log_cb("warning", s)
        return

    # 2) [download] 进度行
    m = _PROGRESS_RE.search(s)
    if m:
        percent = float(m.group("percent"))
        # 多段流（视频+音频分别下载）时进度不倒退：取历史最高值
        state["percent"] = max(state.get("percent") or 0.0, percent)
        if progress_cb:
            progress_cb({
                "status": "downloading",
                "filename": os.path.basename(state["dest"]) if state.get("dest") else "",
                "downloaded_bytes": None,
                "total_bytes": None,
                "percent": state["percent"],
                "speed": None,
                "eta": None,
                "_percent_str": f"{m.group('percent')}%",
                "_speed_str": m.group("speed") or "",
                "_eta_str": m.group("eta") or "",
            })
        return

    # 3) 目标文件 / 合并产物 / 已存在跳过的行 —— 更新最终文件名
    dm = _DEST_RE.search(s)
    if dm:
        state["dest"] = dm.group(1).strip().strip('"')
        if log_cb:
            log_cb("info", s)
        return
    am = _ALREADY_RE.search(s)
    if am:
        state["dest"] = am.group(1).strip().strip('"')
        if log_cb:
            log_cb("info", s)
        return
    mg = _MERGE_RE.search(s)
    if mg:
        state["dest"] = mg.group(1).strip().strip('"')
        if log_cb:
            log_cb("info", s)
        return

    # 4) 其余行：带 [标签] 的走 info，无标签细节走 debug
    _log_line(log_cb, s)


def _run_download_one(args: list, state: dict, cancel_event,
                      log_cb: Optional[LogCB],
                      progress_cb: Optional[ProgressCB]) -> int:
    """跑一次下载子进程；返回其退出码。期间每行检查 cancel_event。"""
    proc = _popen_merged(args)
    try:
        while True:
            raw = proc.stdout.readline()
            if not raw:   # 进程结束 / 管道关闭
                break
            if cancel_event is not None and cancel_event.is_set():
                state["cancelled"] = True
                proc.terminate()
                break
            line = _decode_line(raw)
            if line:
                _handle_download_line(line, state, log_cb, progress_cb)
    finally:
        try:
            proc.stdout.close()
        except Exception:
            pass
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    return proc.returncode


def run_download_queue_external(
    exe: str,
    urls: list,
    output_dir: str,
    fmt_expression: str,
    cookies_cfg=None,
    noplaylist: bool = False,
    ffmpeg_exe: Optional[str] = None,
    cancel_event: Optional[threading.Event] = None,
    log_cb: Optional[LogCB] = None,
    progress_cb: Optional[ProgressCB] = None,
) -> dict:
    """用外部 yt-dlp.exe 依次下载批量队列。

    返回统计字典 {'total','success','failed','cancelled'}，与
    downloader.run_download_queue 一致。注意：本函数在后台线程运行，
    主线程通过置位 cancel_event 请求取消。
    """
    from downloader import FORMAT_AUTO_BEST, friendly_error  # 延迟导入避免循环 import

    if not urls:
        raise ValueError("下载队列为空")
    try:
        os.makedirs(output_dir, exist_ok=True)
    except OSError as exc:
        raise OSError(f"无法创建输出目录：{output_dir}\n原因：{exc}") from exc

    fmt_expression = fmt_expression or FORMAT_AUTO_BEST
    if log_cb:
        log_cb("info", f"[外部 yt-dlp] 输出目录：{output_dir}")
        log_cb("info", f"[外部 yt-dlp] 清晰度/格式表达式：{fmt_expression}")

    total = len(urls)
    stats = {"total": total, "success": 0, "failed": 0, "cancelled": False}

    for index, url in enumerate(urls, start=1):
        if cancel_event is not None and cancel_event.is_set():
            stats["cancelled"] = True
            break
        if log_cb:
            log_cb("info", f"===== 第 {index}/{total} 个任务开始：{url}")
        if progress_cb:
            progress_cb({"status": "task_started", "index": index,
                         "total": total, "url": url})

        state = {"dest": None, "percent": None,
                 "last_error": None, "cancelled": False}
        args = _download_cli_args(exe, url, output_dir, fmt_expression,
                                  cookies_cfg, noplaylist, ffmpeg_exe)
        try:
            retcode = _run_download_one(args, state, cancel_event,
                                        log_cb, progress_cb)
        except Exception as exc:
            stats["failed"] += 1
            if log_cb:
                log_cb("error", f"第 {index}/{total} 个任务失败：{friendly_error(exc)}")
            if progress_cb:
                progress_cb({"status": "task_finished", "index": index,
                             "total": total, "url": url, "cancelled": False})
            continue

        if state.get("cancelled"):
            stats["cancelled"] = True
            if log_cb:
                log_cb("warning", f"任务已被用户取消：{url}")
            if progress_cb:
                progress_cb({"status": "task_finished", "index": index,
                             "total": total, "url": url, "cancelled": True})
            break  # 取消后不再继续队列中的下一个 URL
        if retcode == 0:
            stats["success"] += 1
            if log_cb:
                log_cb("info", f"第 {index}/{total} 个任务完成：{url}")
            # 结束事件：让 UI 进入“已完成/正在后处理”状态（文件名取最终产物）
            if progress_cb:
                progress_cb({
                    "status": "finished",
                    "filename": os.path.basename(state["dest"])
                    if state.get("dest") else "",
                    "downloaded_bytes": None,
                    "total_bytes": None,
                    "percent": 100.0,
                    "speed": None,
                    "eta": None,
                    "_percent_str": "100%",
                    "_speed_str": "",
                    "_eta_str": "",
                })
        else:
            stats["failed"] += 1
            if log_cb:
                log_cb("error", "第 {}/{} 个任务失败（详情见上一条错误）：{}".format(
                    index, total, url))
        if progress_cb:
            progress_cb({"status": "task_finished", "index": index,
                         "total": total, "url": url,
                         "cancelled": False})

    if log_cb:
        if stats["cancelled"]:
            log_cb("warning", f"队列已取消：成功 {stats['success']}，失败 {stats['failed']}。")
        else:
            log_cb("info", f"队列处理完毕：成功 {stats['success']}，失败 {stats['failed']}。")
    return stats



