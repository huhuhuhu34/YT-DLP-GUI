# YT-DLP GUI —— PySide6 桌面批量下载器

基于 **yt-dlp Python API**（不拼命令行）的 Windows 桌面 GUI：解析清晰度、批量排队、逐条下载，支持 Cookies、进度条、取消与应用图标。

## 文件结构

| 文件/目录 | 说明 |
| --- | --- |
| `main.py` | 程序入口 + 主界面（PySide6）|
| `worker.py` | QThread 后台任务封装（Worker + 信号）|
| `downloader.py` | yt-dlp 下载逻辑封装（不依赖 Qt）|
| `ytdlp_cli.py` | 外部 yt-dlp.exe 命令行后端（界面手动指定 exe 时启用）|
| `make_icon.py` | 图标生成脚本（需 PySide6，重跑可换图标）|
| `assets/` | 图标产物：`app.ico`（exe 用）、`app_icon.png`（窗口/任务栏用）|
| `requirements.txt` | 从源码运行时需要的 Python 依赖 |
| `build.ps1` | 一键打包脚本（不内置 ffmpeg，自动带图标）|

---

## 最终用户需要安装的依赖（重要）

打包好的 `YoutubeDlpGUI.exe` 已内置 Python、yt-dlp、PySide6、pycryptodomex、图标，**用户不需要安装 Python**。

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

### （可选）手动指定 yt-dlp.exe / ffmpeg.exe

默认行为：程序**内置** yt-dlp Python 库（版本随 exe 一起打包），ffmpeg 自动检测（系统 PATH / winget 安装目录 / 本程序 exe 同目录）。如果不想等程序更新也想用新版 yt-dlp，或把 ffmpeg 放在了自定义目录，可在界面中部的「**外部程序路径（可选）**」区域手动指定：

- **yt-dlp.exe**：点“浏览…”选择官网下载的 `yt-dlp.exe`（单文件便携版）。保存后“解析”和“下载”都会改用调用该 exe 完成（命令行模式），其版本显示在下方状态行；点“恢复内置”即回到随程序打包的版本。
- **ffmpeg.exe**：点“浏览…”选择 `ffmpeg.exe`（**须与 ffprobe.exe 位于同一目录**）。指定后优先于自动检测，顶部横幅会标注其来源；点“自动检测”即可还原。

几点说明：

- 中文文件名 / 中文路径完全受支持：外部 yt-dlp 的输出会按 UTF-8 / GBK 自动识别解码，日志与进度里的中文标题不会乱码。
- 手动指定的路径会被记住，下次启动自动恢复；若文件已被删除会回退默认并提示。
- 命令行模式的进度来自逐行解析 yt-dlp 输出，精度略低于内置库（断点续传、分片并发等下载行为不变）。
- 下载中“取消”会直接结束外部 yt-dlp 进程；配合断点续传，已下载的分片不会丢失。
- 下载/解析任务运行期间这两个路径的按钮会置灰，避免任务中变更参数。

---

## 开发者：从源码运行

需要：Python 3.10+、ffmpeg（同上）、以及下方 Python 依赖。

```powershell
python -m venv .venv
.\\.venv\\Scripts\\Activate.ps1
python -m pip install -U pip
python -m pip install -r requirements.txt   # yt-dlp / PySide6 / pycryptodomex
python make_icon.py                        # 生成 assets/app.ico 与 app_icon.png
python main.py
```

## 使用流程

1. 粘贴链接 → “加入队列”（可一次加入多个，按列表顺序依次下载）；
2. 点击“解析”获取清晰度列表（后台线程执行，不卡界面），选择所需清晰度；
3. 选择输出目录（自动记住上次目录）、Cookies 来源 → “开始批量下载”；
4. 下载中可点“取消”（下次进度回调时中断，并停止队列）。

## 打包为单文件 exe（不内置 ffmpeg）

```powershell
# 一键打包（推荐）：自动用 .venv、缺 pyinstaller 自动装、图标自动生成/带上
.\build.ps1
```

等价的手工命令：

```powershell
.\\.venv\\Scripts\\python -m pip install -U pyinstaller
.\\.venv\\Scripts\\python -m PyInstaller --noconfirm --clean --onefile --windowed `
    --name "YoutubeDlpGUI" `
    --icon "assets\app.ico" `
    --add-data "assets\app_icon.png;assets" `
    --collect-all yt_dlp `
    --hidden-import Cryptodome `
    main.py
```

产物：`dist\YoutubeDlpGUI.exe`。

### 打包注意事项

- **必须用装好依赖的解释器打包**（推荐 `.venv` 里的 python）。若用没有安装 yt-dlp / PySide6 的全局 Python，PyInstaller 分析阶段会报 `No module named`。
- **`--collect-all yt_dlp` 必须保留**：yt-dlp 有大量延迟加载的抽取器模块，否则打包后“能启动但一解析就报错”。
- **不内置 ffmpeg**：这样打包不会因为缺 ffmpeg.exe 而失败，exe 也更小；用户运行前按上文安装 ffmpeg 即可（程序会自动识别 PATH、winget 安装目录或 exe 同目录下的 ffmpeg）。
- `--windowed` 无控制台；调试期可去掉该参数观察 yt-dlp 原始输出。
- 未签名单文件 exe 首次运行易被 Windows SmartScreen / 杀软拦截，正式分发建议加代码签名。

### 更换图标

- 想换风格：修改 `make_icon.py` 里的 `draw_icon()`（或让它加载你自己的 256×256 图片），然后运行 `python make_icon.py` 重新生成；
- 已有现成图标：直接把你的 `.ico` 覆盖到 `assets/app.ico`、`.png` 覆盖到 `assets/app_icon.png` 即可；
- 重新执行 `\build.ps1` 打包后，窗口图标与 exe 图标都会一起更新。

## 其他说明

- “No Playlist”默认勾选：链接带 `list` 参数时只下载当前视频；取消勾选则整张播放列表依次下载。
- Cookies 来源选 Chrome/Edge/Firefox 等后会自动读取对应浏览器 Cookie。选择来源后界面会：
  - **自动列出**该浏览器已安装的 Profile 目录供选择（Chrome/Edge/Brave/Vivaldi 的 `User Data\Default`、Firefox `Profiles\*.default*` 等，包含中文名的 Profile 亦可正常显示）；
  - 提示该浏览器 Profile 通常放在哪里；找不到时可用下拉末尾的“**手动选择 Profile 目录…**”浏览指定；
  - 也可直接手动输入 Profile 名或完整路径；留空 = 自动使用默认 Profile（**Opera 不支持 Profile**，选择 Opera 时该栏禁用）。
  若浏览器 Cookie 库被占用而读取失败，请先彻底关闭浏览器再重试。