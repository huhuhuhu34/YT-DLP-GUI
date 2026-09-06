# build.ps1 —— 一键打包为单文件 exe（不内置 ffmpeg）
#
# 说明：
#  1) 自动使用项目内的 .venv 解释器（找不到则退回系统 python）；
#  2) 缺 pyinstaller 时自动安装；
#  3) 图标 assets/app.ico 缺失时会先运行 make_icon.py 自动生成；
#  4) 不打包 ffmpeg —— 用户运行时自行安装即可（winget install Gyan.FFmpeg，或见 README），
#     程序会自动识别系统 PATH 或 exe 同目录下的 ffmpeg.exe/ffprobe.exe。

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
Set-Location $root

# 1) 确定解释器：优先 .venv
$py = "$root\.venv\Scripts\python.exe"
if (Test-Path $py) {
    Write-Host "[build] using venv python"
} else {
    $py = "python"
    Write-Host "[build] .venv not found, use system python"
}

# 2) 确保 pyinstaller 可用（无 stderr 噪音的判断方式）
& $py -c "import importlib.util, sys; sys.exit(0 if importlib.util.find_spec('PyInstaller') else 1)"
if ($LASTEXITCODE -ne 0) {
    Write-Host "[build] installing pyinstaller ..."
    & $py -m pip install -U pyinstaller
    if ($LASTEXITCODE -ne 0) { throw "pyinstaller install failed" }
}

# 2.5) 确保图标存在（缺失时自动生成）
if (-not (Test-Path "$root\assets\app.ico")) {
    Write-Host "[build] generating icon (make_icon.py) ..."
    & $py "$root\make_icon.py"
    if ($LASTEXITCODE -ne 0) { throw "icon generation failed" }
}

# 3) 组装参数（图标 + 不内置 ffmpeg）
$pyargs = @(
    "--noconfirm", "--clean", "--onefile", "--windowed",
    "--name", "YoutubeDlpGUI",
    "--icon", "$root\assets\app.ico",
    "--add-data", "$root\assets\app_icon.png;assets",
    "--collect-all", "yt_dlp",
    "--hidden-import", "Cryptodome"
)

Write-Host "[build] PyInstaller start (no ffmpeg bundled) ..."
& $py -m PyInstaller $pyargs "$root\main.py"
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed, exit $LASTEXITCODE" }

Write-Host ""
Write-Host "[build] DONE -> $root\dist\YoutubeDlpGUI.exe"
Write-Host "[build] Note: ffmpeg not bundled; users install it via: winget install Gyan.FFmpeg"