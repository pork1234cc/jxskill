param(
    [string]$PythonCommand = "python",
    [switch]$SkipBrowserInstall
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
# 使用 Skill 自身的路径规则定位共用环境，业务源码仍安装自当前 Skill。
$environmentRoot = & $PythonCommand -X utf8 -c "import sys; sys.path.insert(0, sys.argv[1]); from article_monitor.project import environment_root; print(environment_root())" (Join-Path $projectRoot "scripts")
if ($LASTEXITCODE -ne 0) {
    throw "无法解析 Skill 运行环境目录。"
}
$environmentRoot = $environmentRoot.Trim()
$venvRoot = Join-Path $environmentRoot ".venv"
$venvPython = Join-Path $venvRoot "Scripts\python.exe"
$decryptRoot = Join-Path $PSScriptRoot "wechat-decrypt"
$browserRoot = Join-Path $environmentRoot ".cache\ms-playwright"
$configTemplate = Join-Path $projectRoot "assets\env.example"
$configPath = Join-Path $environmentRoot ".env"

if (-not (Test-Path -LiteralPath $configPath -PathType Leaf)) {
    Copy-Item -LiteralPath $configTemplate -Destination $configPath
}

if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
    & $PythonCommand -m venv $venvRoot
    if ($LASTEXITCODE -ne 0) {
        throw "创建 Python 虚拟环境失败。"
    }
}

& $venvPython -m pip --version 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) {
    & $venvPython -m ensurepip --upgrade --default-pip
    if ($LASTEXITCODE -ne 0) {
        throw "虚拟环境缺少 pip，自动修复失败。请删除项目内 .venv 后重新运行初始化脚本。"
    }
}

$editableTarget = "{0}[dev]" -f $projectRoot
& $venvPython -m pip install -e $editableTarget
if ($LASTEXITCODE -ne 0) {
    throw "安装 articlemonitor 失败。"
}

$nodeCommand = Get-Command node.exe -ErrorAction SilentlyContinue
$npmCommand = Get-Command npm.cmd -ErrorAction SilentlyContinue
$npxCommand = Get-Command npx.cmd -ErrorAction SilentlyContinue
if (-not $nodeCommand -or -not $npmCommand -or -not $npxCommand) {
    throw "找不到 node.exe、npm.cmd 或 npx.cmd，请安装 Node.js 20 或更高版本并重新打开 PowerShell。"
}

$nodeVersion = (& $nodeCommand.Source --version).Trim()
if ($LASTEXITCODE -ne 0 -or $nodeVersion -notmatch '^v(?<major>\d+)\.') {
    throw "无法识别 Node.js 版本，请确认 node.exe 可正常运行。"
}
if ([int]$Matches.major -lt 20) {
    throw "当前 Node.js 版本为 $nodeVersion，本项目需要 Node.js 20 或更高版本。"
}

Push-Location $decryptRoot
try {
    & $npmCommand.Source ci
    if ($LASTEXITCODE -ne 0) {
        throw "安装视频号解密依赖失败。请检查网络连接后重新运行初始化脚本。"
    }
    if (-not $SkipBrowserInstall) {
        New-Item -ItemType Directory -Force -Path $browserRoot | Out-Null
        $env:PLAYWRIGHT_BROWSERS_PATH = $browserRoot
        & $npxCommand.Source playwright install chromium
        if ($LASTEXITCODE -ne 0) {
            throw "安装 Playwright Chromium 失败。请检查网络连接，或使用 -SkipBrowserInstall 暂时跳过。"
        }
    }
}
finally {
    Pop-Location
}

& $venvPython -c "from article_monitor.project import ensure_project_directories; ensure_project_directories()"
if ($LASTEXITCODE -ne 0) {
    throw "创建项目业务目录失败。"
}

function Get-ConfiguredPath {
    param([string]$Name)

    $line = Get-Content -LiteralPath $configPath -Encoding UTF8 |
        Where-Object { $_ -match ("^{0}\s*=" -f [regex]::Escape($Name)) } |
        Select-Object -First 1
    if (-not $line) {
        return $null
    }
    return (($line -split "=", 2)[1]).Trim()
}

$ffmpegPath = Get-ConfiguredPath -Name "FFMPEG_PATH"
$ffprobePath = Get-ConfiguredPath -Name "FFPROBE_PATH"
$ffmpegReady = [bool](Get-Command ffmpeg -ErrorAction SilentlyContinue) -or
    [bool]($ffmpegPath -and (Test-Path -LiteralPath $ffmpegPath -PathType Leaf))
$ffprobeReady = [bool](Get-Command ffprobe -ErrorAction SilentlyContinue) -or
    [bool]($ffprobePath -and (Test-Path -LiteralPath $ffprobePath -PathType Leaf))
if (-not $ffmpegReady -or -not $ffprobeReady) {
    Write-Warning "记录对标可直接使用；监控新增素材前，请安装 FFmpeg/FFprobe 并加入 PATH，或在 .env 配置绝对路径。"
}

$configLines = Get-Content -LiteralPath $configPath -Encoding UTF8
if ($configLines -contains "TIKHUB_API_KEY=your_tikhub_api_key") {
    Write-Warning ".env 中的 TIKHUB_API_KEY 仍是占位值，执行记录或监控前必须替换。"
}
if (
    $configLines -contains "DASHSCOPE_API_KEY=sk-your_dashscope_api_key" -or
    $configLines -contains "DASHSCOPE_ASR_WORKSPACE_ID=your_workspace_id"
) {
    Write-Warning "DashScope 配置仍含占位值；记录对标不受影响，监控新增素材前必须填写。"
}

Write-Output "articlemonitor 初始化完成，运行环境目录：$environmentRoot"
