#Requires -Version 5.1
<#
.SYNOPSIS
    一键把 InsightFlow 从零启动起来：PostgreSQL -> 初始化 -> 后端 -> 前端。

.DESCRIPTION
    这是 README 里推荐的那条路径，脚本本身也就是 README 的忠实实现——
    文档和脚本不一致时，以脚本为准。

    启动顺序是**显式控制**的，不依赖 Compose 的隐式行为：
        1. 起 postgres，等它 healthy
        2. 跑 init（迁移 + 种子数据 + 知识库），等它**退出码为 0**
        3. 起 backend / frontend，等 backend readiness、frontend healthy

    任何一步失败立刻停下并返回非零退出码。

    **不会**执行 down -v，**不会**删除任何已有 volume —— 重复执行是安全的。

.PARAMETER AppMode
    demo 或 real。不传则用 .env 里的 APP_MODE；.env 里也没有则按 real。
    demo 不需要任何 API Key。

.PARAMETER ProjectName
    Compose 项目名。用于在同机上并行跑第二套实例（容器名、卷名都会带上它）。
    不传则用 docker-compose.yml 里 name: 指定的名字。

.PARAMETER OverrideFile
    额外的 Compose 覆盖文件（通常用来改端口，避免和已有实例撞车）。
    必须用 !override 覆盖 ports —— Compose 对 ports 是追加合并的。

.EXAMPLE
    Copy-Item .env.example .env
    .\scripts\bootstrap.ps1 -AppMode demo

.EXAMPLE
    # 在同机上跑第二套隔离实例
    .\scripts\bootstrap.ps1 -AppMode demo -ProjectName insightflow-clean -OverrideFile .\local.override.yml
#>
[CmdletBinding()]
param(
    [ValidateSet("demo", "real")]
    [string]$AppMode,

    [string]$ProjectName,

    [string]$OverrideFile
)

$ErrorActionPreference = "Stop"

# 脚本在 <root>/scripts/ 下，根目录是它的上一级
$Root = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $Root

# 每一步的等待上限（秒）。初始化要跑迁移和导入，给得宽一些。
$POSTGRES_TIMEOUT = 180
$INIT_TIMEOUT = 600
$BACKEND_TIMEOUT = 300
$FRONTEND_TIMEOUT = 300

# --------------------------------------------------------------------------
# 输出小工具
# --------------------------------------------------------------------------

function Write-Step {
    param([string]$Message)
    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Write-Ok {
    param([string]$Message)
    Write-Host "    [OK] $Message" -ForegroundColor Green
}

function Write-Warn {
    param([string]$Message)
    Write-Host "    [!]  $Message" -ForegroundColor Yellow
}

function Write-Info {
    param([string]$Message)
    Write-Host "    $Message" -ForegroundColor Gray
}

# --------------------------------------------------------------------------
# Compose 调用封装
#
# 所有 compose 命令都经过这里，好处是失败判定只有一处：
# 原生命令失败不会抛异常，只设置 $LASTEXITCODE，最容易漏掉。
# --------------------------------------------------------------------------

$script:ComposeArgs = @()
if ($ProjectName) { $script:ComposeArgs += @("-p", $ProjectName) }
$script:ComposeArgs += @("-f", (Join-Path $Root "docker-compose.yml"))
if ($OverrideFile) { $script:ComposeArgs += @("-f", $OverrideFile) }

function Invoke-Compose {
    param(
        [string[]]$Extra,
        [switch]$AllowFailure
    )

    $all = $script:ComposeArgs + $Extra

    # `| Out-Host` 不是为了好看，是必需的：
    # PowerShell 函数会把**所有**写进管道的东西当成返回值，包括原生命令的
    # 标准输出。不隔离的话，调用方拿到的 $initExit 会是一个「几十行日志 + 退出码」
    # 的数组，`-ne 0` 恒为真，于是成功的初始化被判成失败。
    & docker compose @all | Out-Host

    $code = $LASTEXITCODE
    if ($code -ne 0 -and -not $AllowFailure) {
        throw "docker compose $($Extra -join ' ') 失败（退出码 $code）"
    }
    return $code
}

function Get-ServiceContainerId {
    param([string]$Service)

    $all = $script:ComposeArgs + @("ps", "-q", $Service)
    $output = & docker compose @all 2>$null
    if (-not $output) { return $null }
    return @($output)[0].ToString().Trim()
}

function Wait-ContainerHealth {
    param(
        [string]$Service,
        [string]$Target = "healthy",
        [int]$TimeoutSeconds
    )

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    $lastStatus = "(容器不存在)"

    while ((Get-Date) -lt $deadline) {
        $containerId = Get-ServiceContainerId -Service $Service

        if ($containerId) {
            $status = & docker inspect --format "{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}" $containerId 2>$null
            if ($status) { $lastStatus = $status.ToString().Trim() }

            if ($lastStatus -eq $Target) { return $true }

            # 容器已经退出、而且我们要等的不是退出态 —— 再等下去没有意义
            if ($lastStatus -eq "exited" -and $Target -ne "exited") {
                throw "$Service 容器已退出，无法达到 $Target 状态。用下面的命令看原因：`n    docker compose $($script:ComposeArgs -join ' ') logs $Service"
            }
        }

        Start-Sleep -Seconds 3
    }

    throw "等待 $Service 变为 $Target 超时（${TimeoutSeconds}s，最后状态：$lastStatus）。"
}

function Get-PublishedPort {
    param(
        [string]$Service,
        [string]$ContainerPort
    )

    $all = $script:ComposeArgs + @("port", $Service, $ContainerPort)
    $output = & docker compose @all 2>$null
    if (-not $output) { return $null }

    $text = @($output)[0].ToString().Trim()
    if ($text -match ":(\d+)\s*$") { return $Matches[1] }
    return $null
}

# --------------------------------------------------------------------------
# 1. 前置检查
# --------------------------------------------------------------------------

Write-Step "检查 Docker"

$docker = Get-Command docker -ErrorAction SilentlyContinue
if (-not $docker) {
    Write-Host "    找不到 docker 命令。请先安装并启动 Docker Desktop。" -ForegroundColor Red
    exit 1
}

& docker info > $null 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host "    Docker 守护进程没有响应。请先启动 Docker Desktop，等它就绪后重试。" -ForegroundColor Red
    exit 1
}
Write-Ok "Docker 可用"

Write-Step "检查配置文件"

$envPath = Join-Path $Root ".env"
if (-not (Test-Path -LiteralPath $envPath)) {
    Write-Host "    项目根目录下没有 .env。" -ForegroundColor Red
    Write-Host ""
    Write-Host "    先复制一份模板，再按需填写："
    Write-Host "        Copy-Item .env.example .env" -ForegroundColor White
    Write-Host ""
    Write-Host "    Demo 模式不需要任何 API Key，把 APP_MODE 设为 demo 即可直接启动。"
    Write-Host "    （本脚本不会替你创建或覆盖 .env —— 里面可能有你自己的密钥。）"
    exit 1
}
Write-Ok "找到 .env（内容不会被本脚本改动）"

# 解析模式：命令行参数 > .env > real。
# 显式传给 compose 的是**环境变量**，它的优先级高于 .env，所以两者不会打架。
$envAppMode = $null
foreach ($line in Get-Content -LiteralPath $envPath) {
    if ($line -match "^\s*APP_MODE\s*=\s*(.+?)\s*$") {
        $envAppMode = $Matches[1].Trim().Trim('"').Trim("'")
        break
    }
}

$effectiveMode = "real"
if ($envAppMode) { $effectiveMode = $envAppMode.ToLower() }
if ($AppMode) { $effectiveMode = $AppMode.ToLower() }

if ($effectiveMode -ne "demo" -and $effectiveMode -ne "real") {
    Write-Host "    APP_MODE 只能是 real 或 demo，当前读到的是非法值。" -ForegroundColor Red
    Write-Host "    后端也会因为同一个原因拒绝启动。"
    exit 1
}

$env:APP_MODE = $effectiveMode
if ($effectiveMode -eq "demo") {
    Write-Ok "运行模式：demo（不需要任何 API Key）"
} else {
    Write-Ok "运行模式：real（需要 .env 中配置好模型相关 Key）"
}

# --------------------------------------------------------------------------
# 2. PostgreSQL
# --------------------------------------------------------------------------

Write-Step "启动 PostgreSQL"
Invoke-Compose -Extra @("up", "-d", "postgres") | Out-Null
Wait-ContainerHealth -Service "postgres" -Target "healthy" -TimeoutSeconds $POSTGRES_TIMEOUT | Out-Null
Write-Ok "PostgreSQL 已就绪"

# --------------------------------------------------------------------------
# 3. 初始化（迁移 + 种子数据 + 知识库）
#
# 用 `run --rm` 而不是 `up -d`：前者会**阻塞到容器退出**，
# 退出码就是初始化脚本的退出码，失败能立刻发现。
# 依赖 `up -d` 的顺序要模糊得多。
# --------------------------------------------------------------------------

Write-Step "执行初始化（数据库迁移 + 种子数据 + 知识库）"
Write-Info "重复执行是安全的：已存在的零售数据不会重复插入，未变化的知识切片不会重算向量。"

$initExit = Invoke-Compose -Extra @("--profile", "init", "run", "--rm", "init") -AllowFailure
if ($initExit -ne 0) {
    Write-Host ""
    Write-Host "    初始化失败（退出码 $initExit）。" -ForegroundColor Red
    Write-Host "    看详细输出："
    Write-Host "        docker compose $($script:ComposeArgs -join ' ') --profile init run --rm init" -ForegroundColor White
    exit 1
}
Write-Ok "初始化完成"

# --------------------------------------------------------------------------
# 4. 后端与前端
# --------------------------------------------------------------------------

Write-Step "启动后端与前端"
Invoke-Compose -Extra @("up", "-d", "backend", "frontend") | Out-Null

Write-Step "等待后端就绪（readiness）"
Write-Info "这里等的是 readiness，不是进程存活：迁移没跑完、缺表、real 模式缺 Key 都不算就绪。"
Wait-ContainerHealth -Service "backend" -Target "healthy" -TimeoutSeconds $BACKEND_TIMEOUT | Out-Null
Write-Ok "后端已就绪"

Write-Step "等待前端就绪"
Wait-ContainerHealth -Service "frontend" -Target "healthy" -TimeoutSeconds $FRONTEND_TIMEOUT | Out-Null
Write-Ok "前端已就绪"

# --------------------------------------------------------------------------
# 5. 汇总
# --------------------------------------------------------------------------

$frontendPort = Get-PublishedPort -Service "frontend" -ContainerPort "3000"
$backendPort = Get-PublishedPort -Service "backend" -ContainerPort "8000"

$frontendUrl = if ($frontendPort) { "http://localhost:$frontendPort" } else { "（未映射到宿主机端口）" }
$backendDocs = if ($backendPort) { "http://localhost:$backendPort/docs" } else { "（未映射到宿主机端口）" }
$backendHealth = if ($backendPort) { "http://localhost:$backendPort/api/v1/health" } else { "（未映射到宿主机端口）" }
$backendReady = if ($backendPort) { "http://localhost:$backendPort/api/v1/readiness" } else { "（未映射到宿主机端口）" }

Write-Host ""
Write-Host "============================================================" -ForegroundColor Green
Write-Host "  启动完成" -ForegroundColor Green
Write-Host "============================================================" -ForegroundColor Green
Write-Host ""
Write-Host "  模式      $effectiveMode"
Write-Host "  前端      $frontendUrl"
Write-Host "  接口文档  $backendDocs"
Write-Host "  健康检查  $backendHealth"
Write-Host "  就绪检查  $backendReady"
Write-Host ""
Write-Host "  常用命令（在项目根目录执行）："
Write-Host "    查看状态    docker compose $($script:ComposeArgs -join ' ') ps"
Write-Host "    后端日志    docker compose $($script:ComposeArgs -join ' ') logs -f backend"
Write-Host "    前端日志    docker compose $($script:ComposeArgs -join ' ') logs -f frontend"
Write-Host "    数据库日志  docker compose $($script:ComposeArgs -join ' ') logs -f postgres"
Write-Host "    停止服务    docker compose $($script:ComposeArgs -join ' ') down"
Write-Host ""
Write-Host "  停止用 down，**不要**加 -v：加上会连数据卷一起删掉。" -ForegroundColor Yellow
Write-Host ""

if ($effectiveMode -eq "demo") {
    Write-Host "  Demo 模式只支持固定的几个演示问题，页面上会列出可问的清单。" -ForegroundColor Gray
    Write-Host ""
}

exit 0
