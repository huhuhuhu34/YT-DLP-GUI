# YT-DLP GUI —— PySide6 桌面批量下载器

基于 **yt-dlp Python API**（不拼命令行）的 Windows 桌面 GUI：解析清晰度、批量排队、逐条下载，支持 Cookies、进度条与取消。

## 文件结构

| 文件 | 说明 |
| --- | --- |
| `main.py` | 程序入口 + 主界面（PySide6）|
| `worker.py` | QThread 后台任务封装（Worker + 信号）|
| `downloader.py` | yt-dlp 下载逻辑封装（不依赖 Qt）|
| `requirements.txt` | 从源码运行时需要的 Python 依赖 |
| `build.ps1` | 一键打包脚本（不内置 ffmpeg）|

---

## 最终用户需要安装的依赖（重要）

打包好的 `YoutubeDlpGUI.exe` 已内置 Python、yt-dlp、PySide6、pycryptodomex，**用户不需要安装 Python**。

唯一需要用户自行安装的外部依赖是 **ffmpeg**（含 ffprobe.exe）：把“视频流 + 音频流”合并成一个文件、下载 m3u8/HLS 流、格式转换都要用到它。

### 安装 ffmpeg（Windows）

**方法一（推荐，winget 一键安装）**

```powershell
winget install Gyan.FFmpeg
```

装完**重启本程序**即可——程序会自动识别（winget 改的是 PATH，只对“新打开”的终端/程序生效，已开着的窗口要先重启）。无需准备任何 ffmpeg.exe 文件，也不用手动配置 PATH。

**方法二：手动下载 zip（备用）**

1. 打开 https://www.gyan.dev/ffmpeg/builds/ ，下载 **ffmpeg-release-essentials.zip**（约 80MB，内含 ffmpeg.exe 与 ffprobe.exe）；
2. 解压，进入其中的 **bin** 目录；
3. 二选一：把 bin 目录加入系统 PATH；或把 `ffmpeg.exe`、`ffprobe.exe` 复制到 `YoutubeDlpGUI.exe` 同目录。

**验证**：新开命令行执行 `ffmpeg -version`；然后启动程序，顶部横幅显示绿色“✓ ffmpeg 已就绪”即完成。
> 提示：不装 ffmpeg 也能下载“单文件已含音轨”的格式或纯音频，但默认的“最佳画质（自动合并）”与高清“仅视频流”选项会失败。

---

## 开发者：从源码运行

需要：Python 3.10+、ffmpeg（同上）、以及下方 Python 依赖。

```powershell
python -m venv .venv
.\\.venv\\Scripts\\Activate.ps1
python -m pip install -U pip
python -m pip install -r requirements.txt   # yt-dlp / PySide6 / pycryptodomex
python main.py
```

## 使用流程

1. 粘贴链接 → “加入队列”（可一次加入多个，按列表顺序依次下载）；
2. 点击“解析”获取清晰度列表（后台线程执行，不卡界面），选择所需清晰度；
3. 选择输出目录（自动记住上次目录）、Cookies 来源 → “开始批量下载”；
4. 下载中可点“取消”（下次进度回调时中断，并停止队列）。

## 打包为单文件 exe（不内置 ffmpeg）

```powershell
# 一键打包（推荐）：自动使用 .venv，缺 pyinstaller 会自动安装
.\build.ps1
```

等价的手工命令：

```powershell
.\\.venv\\Scripts\\python -m pip install -U pyinstaller
.\\.venv\\Scripts\\python -m PyInstaller --noconfirm --clean --onefile --windowed `
    --name "YoutubeDlpGUI" `
    --collect-all yt_dlp `
    --hidden-import Cryptodome `
    main.py
```

产物：`dist\YoutubeDlpGUI.exe`。

### 打包注意事项

- **必须用装好依赖的解释器打包**（推荐 `.venv` 里的 python）。若用没有安装 yt-dlp / PySide6 的全局 Python，PyInstaller 分析阶段会报 `No module named`。
- **`--collect-all yt_dlp` 必须保留**：yt-dlp 有大量延迟加载的抽取器模块，否则打包后“能启动但一解析就报错”。
- **不内置 ffmpeg**：这样打包不会因为缺 ffmpeg.exe 而失败，exe 也更小；用户运行前按上文安装 ffmpeg 即可（程序会自动识别 PATH 或 exe 同目录下的 ffmpeg）。
- `--windowed` 无控制台；调试期可去掉该参数观察 yt-dlp 原始输出。
- 未签名单文件 exe 首次运行易被 Windows SmartScreen / 杀软拦截，正式分发建议加代码签名。

## 其他说明

- “No Playlist”默认勾选：链接带 `list` 参数时只下载当前视频；取消勾选则整张播放列表依次下载。
- Cookies 来源选 Chrome/Edge/Firefox 等后会自动读取对应浏览器 Cookie（可选填 Profile 名）；若浏览器 Cookie 库被占用而失败，请先完全关闭浏览器重试。