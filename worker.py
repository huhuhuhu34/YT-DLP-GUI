# -*- coding: utf-8 -*-
"""
worker.py —— QThread 后台任务封装（官方推荐的 QObject + moveToThread 模式）

标准用法（由 main.py 负责接线）：
    thread = QThread()
    worker = Worker(func, *args)
    worker.moveToThread(thread)
    thread.started.connect(worker.run)
    worker.finished.connect(thread.quit)
    worker.finished.connect(worker.deleteLater)
    thread.finished.connect(thread.deleteLater)
    thread.start()

Worker 的三个信号（跨线程自动排队，线程安全）：
    log(level, message)          转发 yt-dlp 日志到主线程
    progress(payload)            转发下载进度 / 任务状态到主线程
    finished(ok, data, message)  任务结束
"""
from __future__ import annotations

from PySide6.QtCore import QObject, Signal, Slot

from downloader import friendly_error  # 复用异常翻译，保证任何异常都被友好呈现


class Worker(QObject):
    """把任意“无 UI 依赖”的 Python 函数放到子线程执行的载体。"""

    log = Signal(str, str)                 # (level, message)
    progress = Signal(dict)                # 进度字典
    finished = Signal(bool, object, str)   # (是否成功, 返回值或异常对象, 提示信息)

    def __init__(self, func, *args, **kwargs):
        super().__init__()
        self._func = func   # 在子线程中执行的函数（downloader 的 parse_url / run_download_queue）
        self._args = args
        self._kwargs = kwargs

    @Slot()
    def run(self):
        """由 thread.started 触发，在子线程中真正执行目标任务。"""
        try:
            # data 通常是 parse_url / run_download_queue 返回的字典
            data = self._func(*self._args, **self._kwargs)
            self.finished.emit(True, data, "")
        except Exception as exc:  # 兜底异常处理：任何异常都不能让线程静默崩溃
            self.finished.emit(False, exc, friendly_error(exc))