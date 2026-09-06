# -*- coding: utf-8 -*-
"""
main.py —— YT-DLP GUI 程序入口与主界面（PySide6）

启动：python main.py
"""
from __future__ import annotations

import html
import os
import sys
import threading
from datetime import datetime

from PySide6.QtCore import QSettings, Qt, QThread
from PySide6.QtGui import QIcon, QTextCursor
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from downloader import (
    FORMAT_AUDIO_ONLY,
    FORMAT_AUTO_BEST,
    YTDLP_VERSION,
    browser_profile_roots,
    list_browser_profiles,
    format_duration,
    human_bytes,
    make_cookies_cfg,
    parse_url,
    resolve_ffmpeg,
    run_download_queue,
)
from worker import Worker
from ytdlp_cli import get_ytdlp_version

FFMPEG_DOWNLOAD_URL = "https://www.gyan.dev/ffmpeg/builds/"
BROWSER_CHOICES = ["不使用", "Chrome", "Edge", "Firefox", "Brave", "Opera", "Vivaldi"]

# 日志区各级别的颜色
LOG_COLORS = {
    "debug": "#6f7a86",
    "info": "#1c2333",
    "warning": "#b06a00",
    "error": "#c00000",
}

def icon_path() -> str:
    """返回应用图标文件的绝对路径（源码运行与打包后均可用）。"""
    # PyInstaller --add-data 会把 assets/app_icon.png 解压到 _MEIPASS/assets
    if getattr(sys, "frozen", False):
        base = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
        cand = os.path.join(base, "assets", "app_icon.png")
        if os.path.isfile(cand):
            return cand
    # 源码运行：项目根目录 assets/app_icon.png
    cand = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "app_icon.png")
    if os.path.isfile(cand):
        return cand
    return ""

APP_STYLE = """
QPushButton#primaryBtn {
    background: #2f6fed; color: white; font-weight: bold;
    border: none; border-radius: 4px; padding: 8px 18px; font-size: 12pt;
}
QPushButton#primaryBtn:hover { background: #1f5cd9; }
QPushButton#primaryBtn:disabled { background: #9ab6ee; }
QTextEdit#logEdit { font-family: Consolas, "Microsoft YaHei", monospace; font-size: 9pt; }
"""


class MainWindow(QMainWindow):
    """主窗口：负责 UI 布局、QThread 生命周期与 Qt 信号接线。"""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("YT-DLP 批量下载器")
        self.resize(920, 900)

        self.settings = QSettings("YT-DLP-GUI", "YT-DLP-GUI")
        self._thread: QThread | None = None      # 当前后台线程（同一时刻只允许一个任务）
        self._worker: Worker | None = None
        self._cancel_event: threading.Event | None = None
        self._busy = False
        self._ytdlp_ver_cache: dict = {}         # exe 路径 -> 已探测到的 yt-dlp 版本号

        # 窗口/任务栏图标（图标缺失时静默跳过，不影响启动）
        _ico = icon_path()
        if _ico:
            self.setWindowIcon(QIcon(_ico))

        self._build_ui()
        self._restore_settings()
        self._update_backend_status()
        self._update_ffmpeg_status()
        self._set_busy(False)
        self.append_log("info", "程序就绪：粘贴链接 → 解析 → 选清晰度 → 开始下载。")
    # ---------------------------------------------------------------- UI 布局
    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)

        # —— ffmpeg 状态横幅（醒目提示区，缺 ffmpeg 时高亮）——
        self.ffmpegLabel = QLabel()
        self.ffmpegLabel.setWordWrap(True)
        self.ffmpegLabel.setTextFormat(Qt.RichText)
        self.ffmpegLabel.setOpenExternalLinks(True)
        self.ffmpegLabel.setTextInteractionFlags(Qt.TextBrowserInteraction)
        root.addWidget(self.ffmpegLabel)

        # —— 第一行：链接 + 解析 + 加入队列 ——
        url_row = QHBoxLayout()
        self.urlEdit = QLineEdit()
        self.urlEdit.setPlaceholderText("粘贴视频 / 播放列表链接（支持 Ctrl+V）")
        self.urlEdit.setClearButtonEnabled(True)
        self.urlEdit.returnPressed.connect(self._on_parse_clicked)
        url_row.addWidget(QLabel("链接:"))
        url_row.addWidget(self.urlEdit, 1)

        self.parseBtn = QPushButton("解析")
        self.parseBtn.setToolTip("解析顶部输入框的链接；为空时解析队列中选中的那一项")
        self.parseBtn.clicked.connect(self._on_parse_clicked)
        url_row.addWidget(self.parseBtn)

        self.addBtn = QPushButton("加入队列")
        self.addBtn.setToolTip("把顶部输入框的链接加入批量队列")
        self.addBtn.clicked.connect(self._on_add_url)
        url_row.addWidget(self.addBtn)
        root.addLayout(url_row)

        # —— 批量队列 ——
        queue_group = QGroupBox("批量下载队列（按从上到下顺序依次下载）")
        qv = QVBoxLayout(queue_group)
        qrow = QHBoxLayout()
        self.queueList = QListWidget()
        self.queueList.setFixedHeight(110)
        self.queueList.itemDoubleClicked.connect(
            lambda item: self.urlEdit.setText(item.text()))
        qrow.addWidget(self.queueList, 1)

        col = QVBoxLayout()
        self.removeBtn = QPushButton("移除选中")
        self.removeBtn.clicked.connect(self._on_remove_url)
        col.addWidget(self.removeBtn)
        self.clearBtn = QPushButton("清空队列")
        self.clearBtn.clicked.connect(self._on_clear_queue)
        col.addWidget(self.clearBtn)
        col.addStretch(1)
        qrow.addLayout(col)
        qv.addLayout(qrow)
        root.addWidget(queue_group)

        # —— 输出目录 ——
        out_row = QHBoxLayout()
        self.outDirEdit = QLineEdit()
        self.outDirEdit.setPlaceholderText("选择保存目录（默认：系统“下载”文件夹）")
        out_row.addWidget(QLabel("保存到:"))
        out_row.addWidget(self.outDirEdit, 1)
        self.browseBtn = QPushButton("浏览…")
        self.browseBtn.clicked.connect(self._on_browse_dir)
        out_row.addWidget(self.browseBtn)
        root.addLayout(out_row)
        # —— 清晰度下拉框 ——
        fmt_row = QHBoxLayout()
        self.fmtCombo = QComboBox()
        self.fmtCombo.setMinimumWidth(600)
        fmt_row.addWidget(QLabel("清晰度:"))
        fmt_row.addWidget(self.fmtCombo, 1)
        root.addLayout(fmt_row)

        # —— No Playlist 选项 ——
        self.noPlaylistCheck = QCheckBox(
            "No Playlist：链接带播放列表参数时只下载当前这一个视频（取消勾选则整张播放列表依次下载）")
        self.noPlaylistCheck.setChecked(True)
        root.addWidget(self.noPlaylistCheck)

        # —— Cookies 来源 + Profile ——
        ck_row = QHBoxLayout()
        ck_row.addWidget(QLabel("Cookies 来源:"))
        self.cookieCombo = QComboBox()
        self.cookieCombo.addItems(BROWSER_CHOICES)
        ck_row.addWidget(self.cookieCombo)
        self.profileCombo = QComboBox()
        self.profileCombo.setEditable(True)
        self.profileCombo.setInsertPolicy(QComboBox.NoInsert)
        self.profileCombo.setMinimumContentsLength(26)
        self.profileCombo.setToolTip(
            "选择已检测到的浏览器 Profile 目录；也可直接输入 Profile 名，"
            "或选择“手动选择 Profile 目录…”。留空 = 自动使用默认 Profile。")
        ck_row.addWidget(QLabel("Profile:"))
        ck_row.addWidget(self.profileCombo, 1)
        root.addLayout(ck_row)
        self.cookieCombo.currentIndexChanged.connect(self._on_cookie_changed)
        self.profileCombo.activated.connect(self._on_profile_activated)

        # Profile 位置提示：选择 Cookies 来源后提示去哪找，其余时间隐藏
        self.profileHint = QLabel("")
        self.profileHint.setWordWrap(True)
        self.profileHint.setTextFormat(Qt.RichText)
        self.profileHint.setStyleSheet("color:#6b7280;font-size:9pt;")
        self.profileHint.hide()
        root.addWidget(self.profileHint)

        # —— 外部程序路径（可选）：留空 = 自动检测 / 使用内置 yt-dlp ——
        ext_group = QGroupBox("外部程序路径（可选，留空则自动）")
        ext_v = QVBoxLayout(ext_group)

        row_yd = QHBoxLayout()
        row_yd.addWidget(QLabel("yt-dlp.exe:"))
        self.ytdlpEdit = QLineEdit()
        self.ytdlpEdit.setReadOnly(True)
        self.ytdlpEdit.setPlaceholderText(
            "留空 = 使用打包内置的 yt-dlp 库；填此路径后解析/下载将调用该 exe（适合自备新版）")
        row_yd.addWidget(self.ytdlpEdit, 1)
        self.ytdlpBrowseBtn = QPushButton("浏览…")
        self.ytdlpBrowseBtn.setToolTip("选择官网下载的 yt-dlp.exe（独立可执行文件）")
        self.ytdlpBrowseBtn.clicked.connect(self._on_browse_ytdlp)
        row_yd.addWidget(self.ytdlpBrowseBtn)
        self.ytdlpClearBtn = QPushButton("恢复内置")
        self.ytdlpClearBtn.setToolTip("清空设置，回到打包内置的 yt-dlp 库")
        self.ytdlpClearBtn.clicked.connect(lambda: self._set_ytdlp_exe(""))
        row_yd.addWidget(self.ytdlpClearBtn)
        ext_v.addLayout(row_yd)

        row_ff = QHBoxLayout()
        row_ff.addWidget(QLabel("ffmpeg.exe:"))
        self.ffmpegEdit = QLineEdit()
        self.ffmpegEdit.setReadOnly(True)
        self.ffmpegEdit.setPlaceholderText(
            "留空 = 自动检测（系统 PATH / winget 安装目录 / 本程序 exe 同目录）")
        row_ff.addWidget(self.ffmpegEdit, 1)
        self.ffmpegBrowseBtn = QPushButton("浏览…")
        self.ffmpegBrowseBtn.setToolTip("选择 ffmpeg.exe（应与 ffprobe.exe 位于同一目录）")
        self.ffmpegBrowseBtn.clicked.connect(self._on_browse_ffmpeg)
        row_ff.addWidget(self.ffmpegBrowseBtn)
        self.ffmpegClearBtn = QPushButton("自动检测")
        self.ffmpegClearBtn.setToolTip("清空设置，回到自动检测")
        self.ffmpegClearBtn.clicked.connect(lambda: self._set_ffmpeg_exe(""))
        row_ff.addWidget(self.ffmpegClearBtn)
        ext_v.addLayout(row_ff)

        self.backendStatusLabel = QLabel("")
        self.backendStatusLabel.setWordWrap(True)
        self.backendStatusLabel.setTextFormat(Qt.RichText)
        self.backendStatusLabel.setTextInteractionFlags(Qt.TextBrowserInteraction)
        ext_v.addWidget(self.backendStatusLabel)
        root.addWidget(ext_group)

        # —— 进度条与状态文本 ——
        self.progressBar = QProgressBar()
        self.progressBar.setRange(0, 1000)
        self.progressBar.setValue(0)
        self.progressBar.setFormat("%p%")
        root.addWidget(self.progressBar)

        self.progressInfo = QLabel("尚未开始下载")
        self.progressInfo.setWordWrap(True)
        root.addWidget(self.progressInfo)

        # —— 开始 / 取消 ——
        act_row = QHBoxLayout()
        self.downloadBtn = QPushButton("开始批量下载")
        self.downloadBtn.setObjectName("primaryBtn")
        self.downloadBtn.clicked.connect(self._on_download_clicked)
        self.cancelBtn = QPushButton("取消")
        self.cancelBtn.clicked.connect(self._on_cancel_clicked)
        self.cancelBtn.setEnabled(False)
        act_row.addWidget(self.downloadBtn, 1)
        act_row.addWidget(self.cancelBtn)
        root.addLayout(act_row)

        # —— 日志区 ——
        log_header = QHBoxLayout()
        log_header.addWidget(QLabel("运行日志"))
        log_header.addStretch(1)
        self.clearLogBtn = QPushButton("清空日志")
        self.clearLogBtn.clicked.connect(lambda: self.logEdit.clear())
        log_header.addWidget(self.clearLogBtn)
        root.addLayout(log_header)

        self.logEdit = QTextEdit()
        self.logEdit.setObjectName("logEdit")
        self.logEdit.setReadOnly(True)          # 只读滚动日志
        self.logEdit.setAcceptRichText(True)
        root.addWidget(self.logEdit, 1)

        # 下拉框里始终保留两个默认项（每次解析后再追加具体格式）
        self._reset_format_options()

    # ----------------------------------------------------------- 设置持久化
    def _restore_settings(self):
        # 记住上次选择的输出目录
        last_dir = self.settings.value("output_dir", "")
        if last_dir and os.path.isdir(str(last_dir)):
            self.outDirEdit.setText(str(last_dir))
        else:
            default = os.path.join(os.path.expanduser("~"), "Downloads")
            self.outDirEdit.setText(
                default if os.path.isdir(default) else os.path.expanduser("~"))

        # 记住 cookies 选择与 profile
        try:
            idx = int(self.settings.value("cookie_index", 0))
        except (TypeError, ValueError):
            idx = 0
        if 0 <= idx < self.cookieCombo.count():
            self.cookieCombo.setCurrentIndex(idx)
        self._on_cookie_changed()   # 重建 Profile 候选并更新提示
        if self.cookieCombo.currentIndex() > 0:
            # 上一步刚被 _on_cookie_changed 清空，这里再恢复保存的 Profile 值
            self._apply_profile(str(self.settings.value("cookie_profile", "") or ""))

        # 外部程序路径（手动指定的 yt-dlp.exe / ffmpeg.exe）
        yt_exe = str(self.settings.value("ytdlp_exe_path", "") or "").strip()
        if yt_exe and not os.path.isfile(yt_exe):
            self.append_log("warning",
                            f"上次指定的 yt-dlp.exe 已不存在，已恢复内置后端：{yt_exe}")
            yt_exe = ""
        ff_exe = str(self.settings.value("ffmpeg_exe_path", "") or "").strip()
        if ff_exe and not os.path.isfile(ff_exe):
            self.append_log("warning",
                            f"上次指定的 ffmpeg.exe 已不存在，已恢复自动检测：{ff_exe}")
            ff_exe = ""
        self.ytdlpEdit.setText(yt_exe)
        self.ffmpegEdit.setText(ff_exe)

        # 恢复窗口大小
        geo = self.settings.value("geometry")
        if geo:
            self.restoreGeometry(geo)

    def closeEvent(self, event):
        """退出前：若任务仍在运行则确认并请求取消；同时保存设置。"""
        if self._busy and self._thread is not None:
            ret = QMessageBox.question(
                self, "确认退出",
                "后台任务仍在运行，退出将中断下载。确定退出吗？")
            if ret != QMessageBox.Yes:
                event.ignore()
                return
            if self._cancel_event is not None and not self._cancel_event.is_set():
                self._cancel_event.set()
            self._thread.quit()
            self._thread.wait(10_000)   # 给取消逻辑一点收尾时间
        self.settings.setValue("output_dir", self.outDirEdit.text().strip())
        self.settings.setValue("cookie_index", self.cookieCombo.currentIndex())
        self.settings.setValue("cookie_profile", self._profile_value())
        self.settings.setValue("ytdlp_exe_path", self.ytdlpEdit.text().strip())
        self.settings.setValue("ffmpeg_exe_path", self.ffmpegEdit.text().strip())
        self.settings.setValue("geometry", self.saveGeometry())
        event.accept()
    # ------------------------------------------------------------ UI 槽函数
    # ------------------------------------------- Cookies Profile（下拉枚举/手动选目录）
    def _refresh_profile_items(self):
        """重建 Profile 下拉：默认项 + 自动扫描到的该浏览器 Profile 目录 + 手动浏览项。"""
        browser = self.cookieCombo.currentText()
        self.profileCombo.clear()
        self.profileCombo.addItem("默认（留空 → 自动）", "")
        for path in list_browser_profiles(browser):
            self.profileCombo.addItem(path, path)
        self.profileCombo.addItem("手动选择 Profile 目录…", "__BROWSE__")
        self.profileCombo.setCurrentIndex(-1)

    def _apply_profile(self, value: str):
        """把保存的 Profile 值恢复到下拉框：能匹配候选则选中该项，否则按文本填入。"""
        value = (value or "").strip()
        if not value:
            self.profileCombo.setCurrentIndex(0)
            return
        for i in range(self.profileCombo.count()):
            if str(self.profileCombo.itemData(i) or "") == value:
                self.profileCombo.setCurrentIndex(i)
                return
        self.profileCombo.setEditText(value)

    def _profile_value(self) -> str:
        """当前生效的 Profile 值：''=默认 / Profile 名 / Profile 绝对路径。"""
        text = self.profileCombo.currentText().strip()
        idx = self.profileCombo.currentIndex()
        if 0 <= idx < self.profileCombo.count():
            if text == self.profileCombo.itemText(idx):
                data = self.profileCombo.itemData(idx)
                return "" if data in (None, "__BROWSE__") else str(data)
        return text

    def _on_profile_activated(self, index: int):
        """点击下拉中的“手动选择 Profile 目录…”时打开目录选择器。"""
        if index >= 0 and self.profileCombo.itemData(index) == "__BROWSE__":
            self._browse_profile_dir()

    def _browse_profile_dir(self):
        """打开目录选择器，直达该浏览器存放 Profile 的根目录。"""
        browser = self.cookieCombo.currentText()
        roots = browser_profile_roots(browser)
        start = roots[0] if roots else ""
        if browser == "Firefox":
            title = "选择 Firefox 的 Profile 目录（Profiles 下的 xxx.default-release 子目录）"
        else:
            title = ("选择 Profile 目录（Chrome/Edge/Brave/Vivaldi 为 "
                     "...\\User Data\\Default 这类含 Cookies 的目录）")
        path = QFileDialog.getExistingDirectory(self, title, start)
        if path:
            self.profileCombo.setEditText(os.path.normpath(path))
            self.profileCombo.setCurrentIndex(-1)
            self.append_log("info", f"Cookies Profile 使用目录：{os.path.normpath(path)}")

    def _update_profile_hint(self):
        """选择 Cookies 来源后，提示该浏览器去哪找 Profile。"""
        browser = self.cookieCombo.currentText()
        if self.cookieCombo.currentIndex() <= 0 or browser == "Opera":
            if browser == "Opera":
                self.profileHint.setText(
                    "<b>Opera</b> 不支持指定 Profile，此栏已禁用（可换 Chrome/Edge/Firefox 等）。")
                self.profileHint.show()
            else:
                self.profileHint.hide()
            return
        roots = browser_profile_roots(browser)
        if browser == "Firefox":
            if roots:
                tip = ("Firefox 的 Profile 目录通常在：%s"
                       "（子目录形如 xxx.default-release）。留空 = 自动使用最近使用的 Profile。"
                       % html.escape(roots[0]))
            else:
                tip = ("Firefox 的 Profile 目录默认在 %APPDATA%\\Mozilla\\Firefox\\Profiles"
                       "（形如 xxx.default-release），当前未检测到；可点下拉的"
                       "“手动选择 Profile 目录…”指定。")
        else:
            if roots:
                tip = ("通常位于 %s\\Default（多 Profile 时还有 Profile 1、Profile 2…）。"
                       "留空 = 自动使用默认 Profile。" % html.escape(roots[0]))
            else:
                tip = ("未检测到 %s 的 User Data 目录，可点下拉的“手动选择 Profile 目录…”"
                       "自行指定（需选到含 Cookies 的 Profile 目录）。" % html.escape(browser))
        self.profileHint.setText(tip)
        self.profileHint.show()

    def _on_cookie_changed(self):
        """切换 Cookies 来源：重建 Profile 候选、启用态与“去哪找”提示。"""
        idx = self.cookieCombo.currentIndex()
        browser = self.cookieCombo.currentText()
        can_profile = idx > 0 and browser != "Opera"
        self._refresh_profile_items()
        self.profileCombo.setEnabled(can_profile)
        if not can_profile:
            self.profileCombo.setEditText("")
        self._update_profile_hint()

    def _on_add_url(self):
        url = self.urlEdit.text().strip()
        if not url:
            self.append_log("warning", "请先粘贴链接再点击“加入队列”。")
            return
        existing = {self.queueList.item(i).text() for i in range(self.queueList.count())}
        if url in existing:
            self.append_log("info", f"队列中已有该链接：{url}")
        else:
            self.queueList.addItem(url)
            self.append_log("info", f"已加入队列（第 {self.queueList.count()} 项）：{url}")
        self.urlEdit.clear()

    def _on_remove_url(self):
        row = self.queueList.currentRow()
        if row >= 0:
            self.queueList.takeItem(row)

    def _on_clear_queue(self):
        self.queueList.clear()

    def _on_browse_dir(self):
        start = self.outDirEdit.text().strip()
        if not start or not os.path.isdir(start):
            start = os.path.expanduser("~")
        path = QFileDialog.getExistingDirectory(self, "选择下载保存目录", start)
        if path:
            self.outDirEdit.setText(path)
            self.settings.setValue("output_dir", path)

    # ------------------------------------------------------- 外部程序设置
    def _start_dir(self) -> str:
        """“浏览”对话框的起始目录：当前已填路径的目录，否则程序所在目录。"""
        for edit in (self.ytdlpEdit, self.ffmpegEdit):
            cur = edit.text().strip()
            if cur and os.path.isfile(cur):
                return os.path.dirname(os.path.abspath(cur))
        if getattr(sys, "frozen", False):
            return os.path.dirname(sys.executable)
        return os.path.dirname(os.path.abspath(__file__))

    def _on_browse_ytdlp(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "选择 yt-dlp.exe（官网下载的独立可执行文件）", self._start_dir(),
            "可执行文件 (*.exe)")
        if path:
            self._set_ytdlp_exe(path)

    def _on_browse_ffmpeg(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "选择 ffmpeg.exe（须与 ffprobe.exe 同目录）", self._start_dir(),
            "可执行文件 (*.exe)")
        if path:
            self._set_ffmpeg_exe(path)

    def _set_ytdlp_exe(self, path: str) -> None:
        """设置外部 yt-dlp.exe 路径（空串 = 恢复内置后端）。设置即校验并持久化。"""
        path = os.path.abspath(path.strip()) if (path or "").strip() else ""
        if path and not os.path.isfile(path):
            self.append_log("warning", f"指定的文件不存在：{path}")
            return
        if path:
            ver = self._ytdlp_ver(path)
            if not ver:
                self.append_log("error",
                                f"无法运行所选文件读取版本，请确认它确实是 yt-dlp.exe：\n{path}")
                return
            self.append_log("info", f"yt-dlp.exe 校验通过（v{ver}）：{path}")
        else:
            self.append_log("info", "已恢复“内置 yt-dlp 库”后端。")
        self.ytdlpEdit.setText(path)
        self.settings.setValue("ytdlp_exe_path", path)
        self._update_backend_status()

    def _set_ffmpeg_exe(self, path: str) -> None:
        """设置手动 ffmpeg.exe 路径（空串 = 恢复自动检测）。设置即校验并持久化。"""
        path = os.path.abspath(path.strip()) if (path or "").strip() else ""
        if path and not os.path.isfile(path):
            self.append_log("warning", f"指定的文件不存在：{path}")
            return
        if path:
            sibling = os.path.join(os.path.dirname(path), "ffprobe.exe")
            if not os.path.isfile(sibling):
                self.append_log(
                    "warning", "提示：所选目录里没有 ffprobe.exe，部分合并/探测可能失败；"
                               "建议选择同时包含 ffmpeg.exe 与 ffprobe.exe 的 bin 目录。")
            self.append_log("info", f"已指定 ffmpeg：{path}")
        else:
            self.append_log("info", "已恢复 ffmpeg 自动检测。")
        self.ffmpegEdit.setText(path)
        self.settings.setValue("ffmpeg_exe_path", path)
        self._update_ffmpeg_status()

    def _ytdlp_ver(self, exe: str) -> str:
        """读取外部 yt-dlp.exe 版本（带缓存，避免反复启动子进程）。"""
        if exe not in self._ytdlp_ver_cache:
            self._ytdlp_ver_cache[exe] = get_ytdlp_version(exe)
        return self._ytdlp_ver_cache[exe]

    def _on_parse_clicked(self):
        """解析 URL（后台线程执行 extract_info，避免卡界面）。"""

        if self._busy:
            return
        url = self.urlEdit.text().strip()
        if not url:
            item = self.queueList.currentItem()
            if item:
                url = item.text().strip()
        if not url:
            self.append_log("warning", "请先在顶部粘贴链接，或在队列中选中一项。")
            return

        # 主线程里快照所有参数，后台线程绝不直接读 UI 控件
        cookies_cfg = make_cookies_cfg(self.cookieCombo.currentText(), self._profile_value())
        noplaylist = self.noPlaylistCheck.isChecked()
        is_playlist_url = ("playlist" in url.lower()) or ("list=" in url.lower())
        ytdlp_exe = self.ytdlpEdit.text().strip() or None   # 外部 yt-dlp.exe（可空）

        def task():
            return parse_url(url, cookies_cfg=cookies_cfg,
                             noplaylist=noplaylist,
                             ytdlp_exe=ytdlp_exe,
                             log_cb=self._worker_log)

        self._start_job(task, on_done=self._on_parse_done, cancellable=False)

        if noplaylist and is_playlist_url:
            self.append_log(
                "info", "提示：当前勾选了 No Playlist。若此链接是纯播放列表且解析失败，"
                        "请取消勾选后重试。")

    def _on_parse_done(self, ok: bool, data, message: str):
        """解析结束（主线程）。data 是 parse_url 返回的字典。"""
        if not ok:
            self.append_log("error", f"解析失败：{message}")
            return
        info = data or {}

        if info.get("is_playlist"):
            n = info.get("playlist_count")
            suffix = f"，共 {n} 项" if n else ""
            self.append_log(
                "info", f"检测到播放列表“{info.get('title')}”{suffix}。"
                        "列表内各视频下载时统一套用所选的清晰度。")
            self._reset_format_options()  # 播放列表无统一 formats，只保留默认项
            return

        formats = info.get("formats") or []
        if not formats:
            self.append_log("warning", "解析成功但拿不到清晰度列表，将使用默认选项。")
            self._reset_format_options()
            return

        # 重建下拉框 = 默认最佳画质 + 默认纯音频 + 该 URL 的具体格式
        self._reset_format_options()
        for fd in formats:
            row = self.fmtCombo.count()
            self.fmtCombo.addItem(fd["label"], fd)          # userData = 格式字典
            self.fmtCombo.setItemData(row, self._format_tooltip(fd), Qt.ToolTipRole)

        title = info.get("title") or ""
        text = f"解析成功：{title}"
        dur = format_duration(info.get("duration"))
        if dur:
            text += f"（时长 {dur}）"
        if info.get("uploader"):
            text += f" ｜ 作者：{info['uploader']}"
        self.append_log("info", text)
        self.append_log("info", f"共 {len(formats)} 个可选项（已按清晰度从高到低排列）。")
    def _on_download_clicked(self):
        """开始批量下载（后台线程逐个 URL 处理，UI 不阻塞）。"""
        if self._busy:
            return

        # 收集队列；队列为空但输入框有链接时自动把输入框当作一个任务
        urls = [self.queueList.item(i).text().strip()
                for i in range(self.queueList.count())]
        urls = [u for u in urls if u]
        if not urls and self.urlEdit.text().strip():
            urls = [self.urlEdit.text().strip()]
        if not urls:
            self.append_log("warning", "队列为空：请先添加要下载的链接。")
            return

        output_dir = self.outDirEdit.text().strip()
        if not output_dir:
            self.append_log("error", "请先选择输出目录。")
            return
        try:
            os.makedirs(output_dir, exist_ok=True)
        except OSError as exc:
            self.append_log("error", f"输出目录不可用：{exc}")
            return
        self.settings.setValue("output_dir", output_dir)  # 记住本次目录

        # 当前清晰度选项（主线程内取值并转为 format 表达式）
        data = self.fmtCombo.currentData()
        if isinstance(data, dict) and data.get("expression"):
            fmt_expr = data["expression"]
            fmt_text = self.fmtCombo.currentText()
        else:
            fmt_expr = FORMAT_AUTO_BEST
            fmt_text = "默认：最佳画质"

        cookies_cfg = make_cookies_cfg(self.cookieCombo.currentText(), self._profile_value())
        noplaylist = self.noPlaylistCheck.isChecked()
        ytdlp_exe = self.ytdlpEdit.text().strip() or None    # 外部 yt-dlp.exe（可空）
        ffmpeg_exe = self.ffmpegEdit.text().strip() or None  # 手动指定 ffmpeg（可空）
        if ytdlp_exe:
            self.append_log("info", "后端：手动指定的外部 yt-dlp.exe（命令行模式）。")
        else:
            self.append_log("info", "后端：打包内置的 yt-dlp Python 库。")
        if cookies_cfg:
            self.append_log("info", f"Cookies 来源：{self.cookieCombo.currentText()}"
                                    f"（Profile: {self._profile_value() or '默认'}）")
        else:
            self.append_log("info", "Cookies：不使用")
        self.append_log("info", f"队列任务数：{len(urls)}；清晰度：{fmt_text}")

        self.progressBar.setRange(0, 1000)
        self.progressBar.setValue(0)
        self.progressInfo.setText("正在启动下载线程……")

        def task():
            # 注意：所有参数都已在主线程快照完毕，后台线程只做纯计算
            return run_download_queue(
                list(urls),
                output_dir,
                fmt_expr,
                cookies_cfg,
                noplaylist=noplaylist,
                ytdlp_exe=ytdlp_exe,                  # 外部 yt-dlp.exe（可空）
                ffmpeg_exe=ffmpeg_exe,                # 手动指定 ffmpeg（可空）
                cancel_event=self._cancel_event,      # 取消事件（线程安全）
                log_cb=self._worker_log,
                progress_cb=self._worker_progress,
            )

        self._start_job(task, on_done=self._on_download_done, cancellable=True)

    def _on_download_done(self, ok: bool, data, message: str):
        """整个队列下载结束（主线程）。"""
        if not ok:
            self.append_log("error", f"下载任务异常终止：{message}")
            self.progressInfo.setText("下载异常终止")
            return
        stats = data or {}
        if stats.get("cancelled"):
            self.append_log("warning", "下载已取消。")
            self.progressInfo.setText("下载已取消")
        else:
            self.append_log("info", "全部队列任务处理完毕。")
            self.progressInfo.setText("全部任务处理完毕")
        self.append_log(
            "info", f"统计：成功 {stats.get('success', 0)}，失败 {stats.get('failed', 0)}"
                    f"，共 {stats.get('total', 0)}。")
        if stats.get("failed"):
            self.append_log("info", "失败的链接请查看上方红色错误日志。")
        self.progressBar.setValue(0)

    def _on_cancel_clicked(self):
        """请求取消：置位事件，下载器会在下一次进度回调时中断。"""
        if self._cancel_event is not None and not self._cancel_event.is_set():
            self._cancel_event.set()
            self.cancelBtn.setEnabled(False)
            self.append_log("warning", "正在请求取消……当前下载块结束后会尽快生效。")

    # ------------------------------------------------------------ 进度与日志
    def _worker_log(self, level: str, message: str) -> None:
        """后台线程回调入口：把 yt-dlp 日志经 Worker.log 信号发回主线程。"""
        worker = self._worker
        if worker is not None:
            worker.log.emit(level, message)

    def _worker_progress(self, payload: dict) -> None:
        """后台线程回调入口：把进度字典经 Worker.progress 信号发回主线程。"""
        worker = self._worker
        if worker is not None:
            worker.progress.emit(payload)

    def append_log(self, level: str, message: str) -> None:
        """往日志区追加一行（只会在主线程被调用，天然线程安全）。

        PySide6 的 QTextEdit 没有 appendHtml()，这里用“光标移到末尾 +
        insertHtml()”追加带颜色的日志，并自动滚动到底部。
        """
        color = LOG_COLORS.get(level, LOG_COLORS["info"])
        tag = {"debug": "调试", "warning": "警告", "error": "错误"}.get(level, "")
        ts = datetime.now().strftime("%H:%M:%S")
        prefix = f"[{ts}]" + (f"[{tag}]" if tag else "")
        safe = html.escape(str(message)).replace("\r", "")
        cursor = self.logEdit.textCursor()
        cursor.movePosition(QTextCursor.End)
        self.logEdit.setTextCursor(cursor)
        self.logEdit.insertHtml(
            f'<span style="color:{color};"><b>{html.escape(prefix)}</b> '
            f'{safe}</span><br>')
        # 自动滚动到底部，保证能看到最新日志
        bar = self.logEdit.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _on_progress(self, payload: dict) -> None:
        """主线程：根据进度字典更新进度条与状态文本。"""
        status = payload.get("status")

        if status == "task_started":
            self.progressBar.setRange(0, 1000)
            self.progressBar.setValue(0)
            self.progressInfo.setText(
                f"第 {payload.get('index')}/{payload.get('total')} 个任务开始……")
            return
        if status == "task_finished":
            if not payload.get("cancelled"):
                self.progressBar.setValue(1000)
            self.progressInfo.setText(
                f"第 {payload.get('index')}/{payload.get('total')} 个任务结束。")
            return

        if status == "downloading":
            percent = payload.get("percent")
            if percent is None:
                self.progressBar.setRange(0, 0)     # 总量未知 → 忙碌动画
            else:
                if self.progressBar.maximum() == 0:
                    self.progressBar.setRange(0, 1000)
                self.progressBar.setValue(min(1000, int(percent * 10)))

            parts = []
            fn = payload.get("filename") or ""
            if fn:
                parts.append(f"文件：{fn}")
            # 直接使用 yt-dlp 格式化好的进度 / 速度 / 剩余时间字符串
            if payload.get("_percent_str"):
                parts.append(f"进度：{payload['_percent_str']}")
            if payload.get("_speed_str"):
                parts.append(f"速度：{payload['_speed_str']}")
            if payload.get("_eta_str"):
                parts.append(f"剩余：{payload['_eta_str']}")
            if payload.get("downloaded_bytes") is not None:
                d = human_bytes(payload.get("downloaded_bytes"))
                t = human_bytes(payload.get("total_bytes")) if payload.get("total_bytes") else "?"
                parts.append(f"{d} / {t}")
            self.progressInfo.setText("　｜　".join(parts) or "下载中……")

        elif status == "finished":
            if self.progressBar.maximum() == 0:
                self.progressBar.setRange(0, 1000)
            self.progressBar.setValue(1000)
            fn = payload.get("filename") or ""
            self.progressInfo.setText(f"{fn} 下载完成，正在执行合并/后处理……")
    # ------------------------------------------------ 外部程序与后端状态
    def _update_backend_status(self):
        """刷新外部程序设置区底部的“当前 yt-dlp 后端”状态文字。"""
        exe = self.ytdlpEdit.text().strip()
        if exe and os.path.isfile(exe):
            ver = self._ytdlp_ver(exe)
            detail = html.escape(exe) + (f"，v{html.escape(ver)}" if ver else "")
            text = ('<span style="color:#0a7d32;font-weight:bold;">✓ 使用外部 yt-dlp</span>'
                    f'<span style="color:#444;">（{detail}）—— 解析与下载均调用该 exe。</span>')
        else:
            text = ('<span style="color:#555;">当前后端：<b>内置 yt-dlp'
                    f'（v{html.escape(YTDLP_VERSION)}）</b>'
                    '—— 随程序打包。想用更新版本？在上方浏览选择官方下载的 yt-dlp.exe。</span>')
        self.backendStatusLabel.setText(text)

    # ------------------------------------------------------------ ffmpeg 提示
    def _update_ffmpeg_status(self):
        """顶部横幅：手动指定优先；其次自动检测；都找不到时给安装指引。"""
        manual = self.ffmpegEdit.text().strip()
        path = resolve_ffmpeg(manual or None)
        if path:
            src = "手动指定" if manual else "自动检测"
            self.ffmpegLabel.setText(
                f'<span style="color:#0a7d32;font-weight:bold;">✓ ffmpeg 已就绪</span>'
                f'<span style="color:#444;">（{src}：{html.escape(str(path))}）—— 可正常合并音视频。</span>')
            return
        if manual:
            self.ffmpegLabel.setText(
                '<span style="color:#c00000;font-weight:bold;">⚠ 手动指定的 ffmpeg 不可用</span>'
                '<span style="color:#7a2000;">（文件已失效或不是有效可执行文件），且自动检测也未找到。'
                '请在“外部程序路径”里重新指定，或按下方指引安装。</span>')
            return
        # 未检测到 ffmpeg：给出明确的分步安装指引（不依赖 PATH 知识也能照做）
        steps = (
            '<span style="color:#7a2000;">安装方法：① 最快——新开终端执行 '
            '<b>winget install Gyan.FFmpeg</b>，装完重启本程序即可自动识别，无需配置 PATH；'
            '② 或打开'
            f'<a href="{FFMPEG_DOWNLOAD_URL}" style="color:#0a58ca;">gyan.dev ffmpeg builds</a>'
            '下载 essentials 版 zip，把解压出来的 bin 目录加入系统 PATH，或把 ffmpeg.exe、ffprobe.exe'
            '复制到本程序 exe 同目录；③ 执行 ffmpeg -version 验证成功后重启本程序。</span>')
        self.ffmpegLabel.setText(
            '<span style="color:#c00000;font-weight:bold;">⚠ 未检测到 ffmpeg！'
            '需要合并音视频（默认“最佳画质”或“仅视频流”清晰度）时无法完成合成。</span><br>'
            + steps)

    # ------------------------------------------------------------ 工具方法
    def _reset_format_options(self):
        """下拉框里始终保留的两个默认项（每次解析后重建具体格式列表）。"""
        self.fmtCombo.clear()
        self.fmtCombo.addItem(
            "默认：最佳画质（自动选最高清晰度视频 + 最佳音频并合并）",
            {"kind": "expr", "expression": FORMAT_AUTO_BEST})
        self.fmtCombo.addItem(
            "默认：仅下载最佳音频（不合并）",
            {"kind": "expr", "expression": FORMAT_AUDIO_ONLY})

    def _format_tooltip(self, fd: dict) -> str:
        """给下拉框某一行格式准备的悬浮提示。"""
        size = human_bytes(fd.get("filesize")) if fd.get("filesize") else "未知"
        lines = [
            f"format_id：{fd.get('format_id')}",
            f"封装格式：{fd.get('ext')}",
            f"分辨率：{fd.get('height') or '-'}",
            f"视频编码：{fd.get('vcodec') or '-'}",
            f"音频编码：{fd.get('acodec') or '-'}",
            f"文件大小：{size}",
        ]
        if fd.get("kind") == "video":
            lines.append("提示：该行是纯视频流，下载时会自动带上最佳音频并合并（需要 ffmpeg）。")
        return "\n".join(lines)

    def _set_busy(self, busy: bool, cancellable: bool = True):
        """任务运行期间禁用会引发并发冲突的控件。"""
        self._busy = busy
        for w in (self.parseBtn, self.addBtn, self.removeBtn, self.clearBtn,
                  self.downloadBtn, self.browseBtn,
                  self.ytdlpBrowseBtn, self.ytdlpClearBtn,
                  self.ffmpegBrowseBtn, self.ffmpegClearBtn):
            w.setEnabled(not busy)
        self.cancelBtn.setEnabled(busy and cancellable)

    # ------------------------------------------------------------ 线程管理
    def _start_job(self, task, on_done, cancellable: bool):
        """启动一个后台任务：QThread + Worker（标准 moveToThread 写法）。"""
        if self._thread is not None and self._thread.isRunning():
            self.append_log("warning", "已有任务在运行，请稍候。")
            return

        if cancellable:
            # 每次下载任务使用全新的取消事件（已置位的旧事件不能复用）
            if self._cancel_event is None or self._cancel_event.is_set():
                self._cancel_event = threading.Event()
        else:
            self._cancel_event = None

        thread = QThread(self)
        thread.setObjectName("bg_task")
        worker = Worker(task)
        worker.moveToThread(thread)

        # 子线程信号 → 主线程槽（Qt 自动排队连接，线程安全）
        worker.log.connect(self.append_log)
        worker.progress.connect(self._on_progress)

        # 官方推荐的清理顺序
        worker.finished.connect(on_done)                  # 本次任务专属收尾
        worker.finished.connect(self._finalize_job)       # 通用收尾（复位按钮）
        worker.finished.connect(thread.quit)              # 退出线程事件循环
        worker.finished.connect(worker.deleteLater)       # 释放 worker
        thread.finished.connect(thread.deleteLater)       # 释放 thread

        thread.started.connect(worker.run)                # 线程启动 → 执行任务

        self._thread = thread
        self._worker = worker
        self._set_busy(True, cancellable=cancellable)
        self.append_log("info", "后台任务已启动……")
        thread.start()

    def _finalize_job(self, ok, data, message):
        """任务结束后复位（主线程执行）。"""
        del ok, data, message
        self._set_busy(False)
        self._thread = None
        self._worker = None
        self._cancel_event = None


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("YT-DLP GUI")
    app.setOrganizationName("YT-DLP-GUI")
    app.setStyleSheet(APP_STYLE)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()