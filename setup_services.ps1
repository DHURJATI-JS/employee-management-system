# One click: start.cmd runs this with -Start. It installs whatever is missing (uv, Python, the app's packages, WSL, Docker Desktop), makes .env with random passwords
# and the folders the app writes to, starts Redis (6379), ClamAV (3310) and Postgres (5432) in containers, runs the app and opens it in the browser.
# The app runs without vLLM (8100, the local AI on the graphics card), Whisper (9100, meeting captions) and LibreTranslate (5000, other languages), so a second, minimized window
# sets those up: a first run's huge downloads don't hold the app back, and what needs them turns on by itself once they're ready.
# app.py runs this script without -Start when Redis, ClamAV or Postgres don't answer, and with -Start when a fresh copy has no packages or no .env yet.
# Usage:  double-click start.cmd
#    or:  powershell -ExecutionPolicy Bypass -File .\setup_services.ps1 [-Start] [-NoBrowser] [-Sync]
param(
    [switch]$Start,      # + the app's packages, then run the app and open it in the browser
    [switch]$NoBrowser,  # with -Start: don't open the browser
    [switch]$Sync,       # with -Start: install the app's packages again from myapp/uv.lock (app.py asks for it when one is missing)
    [switch]$Optional    # the second window: vLLM, Whisper and LibreTranslate
)

$DockerExe = "C:\Program Files\Docker\Docker\Docker Desktop.exe"
$EnvFile = "$PSScriptRoot\.env"
$AppPython = "$PSScriptRoot\myapp\.venv\Scripts\python.exe"
$Settings = @{}

function Test-DockerRunning {
    docker info *> $null
    return $LASTEXITCODE -eq 0
}

function Test-Wsl {
    wsl --status *> $null
    return $LASTEXITCODE -eq 0
}

function Test-App($Url) {
    try { return (Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 "$Url/csrf").StatusCode -eq 200 } catch { return $false }
}

function Update-Path {
    $env:Path += ";" + [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
}

function Install-WithWinget($Id, $Name, [string[]]$Extra = @()) {
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        Write-Host "$Name is missing, and winget isn't here to install it. Get App Installer from the Microsoft Store, then run this again." -ForegroundColor Red
        exit 1
    }
    Write-Host "Installing $Name with winget..." -ForegroundColor Yellow
    winget install -e --id $Id --accept-package-agreements --accept-source-agreements @Extra
    Update-Path
}

# The card the local AI runs on: asked once, when .env is made (VLLM_GPU then decides, and can be changed by hand).
function Find-Gpu {
    $Cards = @(Get-CimInstance Win32_VideoController | ForEach-Object { $_.Name }) -join " "
    if ($Cards -match "NVIDIA") { return "nvidia" }
    if ($Cards -match "AMD|Radeon") { return "amd" }
    return "none"
}

function New-Secret {
    $Bytes = New-Object byte[] 32
    [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($Bytes)
    return ([BitConverter]::ToString($Bytes) -replace "-").ToLower()
}

# First run: .env from .env.example, with its own random keys and passwords. An existing .env is never changed: a new SECRET_KEY would make saved bank and ID numbers unreadable.
function New-EnvFile {
    if (Test-Path $EnvFile) { return }
    $Text = [IO.File]::ReadAllText("$PSScriptRoot\.env.example")
    foreach ($Key in "SECRET_KEY", "REDIS_PASSWORD", "POSTGRES_PASSWORD", "POSTGRES_APP_PASSWORD") {
        $Text = [regex]::Replace($Text, "(?m)^$Key=[^\r\n]*", "$Key=$(New-Secret)")
    }
    $Text = [regex]::Replace($Text, "(?m)^VLLM_GPU=[^\r\n]*", "VLLM_GPU=$(Find-Gpu)")
    [IO.File]::WriteAllText($EnvFile, $Text, (New-Object Text.UTF8Encoding($false)))
    $script:NewEnv = $true
    Write-Host "Made .env with new random passwords. Keep it safe, and don't change SECRET_KEY." -ForegroundColor Green
    Write-Host "  Company creation needs a free Geoapify key (GEOAPIFY_API_KEY, GEOAPIFY_SERVER_KEY). The admin console stays off until ADMIN_USERNAME and ADMIN_PASSWORD are set."
}

# The folders the app writes to. None of them go to GitHub, so a fresh copy doesn't have them.
function New-Folders {
    foreach ($Folder in "uploads", "logs", "backups", "static\dist") { New-Item -ItemType Directory -Force "$PSScriptRoot\$Folder" | Out-Null }
}

# A new .env on a computer that already has a database: its passwords and keys can't open that data, so stop here and say so, instead of failing somewhere later.
function Stop-IfDataWithoutEnv {
    if ($NewEnv -and (docker volume ls -q --filter "name=^pgdata$")) {
        Remove-Item $EnvFile -Force -Confirm:$false
        Write-Host "This computer already has a database, made with a different .env: a new one can't open it. Put your .env back, then run this again." -ForegroundColor Red
        exit 1
    }
}

# Read KEY=VALUE pairs from .env (skips blanks and comments)
function Read-Settings {
    $script:Settings = @{}
    Get-Content $EnvFile -Encoding UTF8 | Where-Object { $_ -match '^\s*([^#=]+)=(.*)$' } |
        ForEach-Object { $script:Settings[$Matches[1].Trim()] = $Matches[2].Trim() }
    $script:VllmGpu = if ($Settings.VLLM_GPU) { $Settings.VLLM_GPU.ToLower() } else { Find-Gpu }
    if ($VllmGpu -notin "amd", "nvidia", "none") { Write-Host "VLLM_GPU in .env must be amd, nvidia or none, not $VllmGpu. Going on without the AI." -ForegroundColor Red; $script:VllmGpu = "none" }
    $script:VllmModel = if ($Settings.VLLM_MODEL) { $Settings.VLLM_MODEL } else { "gemma-3-12b" }
    $script:VllmParallel = if ($Settings.VLLM_PARALLEL) { $Settings.VLLM_PARALLEL } else { "8" }
    $script:VllmMaxLen = if ($Settings.VLLM_MAX_LEN) { $Settings.VLLM_MAX_LEN } else { "5120" }
    $script:VllmImage = "deminic-vllm-$VllmGpu"
    $script:VllmSettings = "$VllmGpu|$VllmParallel|$VllmMaxLen|$VllmModel"
}

function Find-Python {
    uv python find 3.15 --system --python-preference only-system *> $null
    return $LASTEXITCODE -eq 0
}

# uv, Python and the app's packages in myapp/.venv. An environment that's there is left alone: only -Sync (app.py, when a package is missing) touches it.
# Python comes from python.org's signed Install Manager and uv is told not to download its own: Smart App Control blocks unsigned programs, and uv's Python builds aren't signed.
function Install-Packages {
    if ((Test-Path $AppPython) -and -not $Sync) { return }
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) { Install-WithWinget "astral-sh.uv" "uv" }
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) { Write-Host "uv didn't install. Get it from https://docs.astral.sh/uv/ and run this again." -ForegroundColor Red; exit 1 }
    if (-not (Find-Python)) {
        Install-WithWinget "Python.PythonInstallManager" "Python"
        & "$env:LOCALAPPDATA\Microsoft\WindowsApps\py.exe" install --yes 3.15
        if (-not (Find-Python)) { Write-Host "Python 3.15 didn't install. Get it from https://www.python.org/downloads/ and run this again." -ForegroundColor Red; exit 1 }
    }
    Write-Host "Installing the app's packages..." -ForegroundColor Yellow
    uv sync --project "$PSScriptRoot\myapp" --frozen --no-install-project --python-preference only-system
    if ($LASTEXITCODE -ne 0) { Write-Host "The app's packages didn't install: the reason is above. If it says an Application Control policy blocked a file, Windows refused to run it." -ForegroundColor Red; exit 1 }
    $env:DEMINIC_SYNCED = "1"
}

# WSL, then Docker Desktop if it's missing, then start it and wait for the engine. Enabling WSL for the first time asks for administrator rights and a restart.
function Start-Docker {
    Update-Path
    if (-not (Test-Wsl)) {
        Write-Host "Installing WSL, which Docker needs. Windows will ask for permission..." -ForegroundColor Yellow
        Start-Process wsl -Verb RunAs -ArgumentList "--install", "--no-distribution" -Wait
        Write-Host "Restart Windows, then double-click start.cmd again: it carries on from here." -ForegroundColor Yellow
        exit 1
    }
    if (-not (Get-Command docker -ErrorAction SilentlyContinue) -and -not (Test-Path $DockerExe)) {
        Install-WithWinget "Docker.DockerDesktop" "Docker Desktop" @("--override", "install --quiet --accept-license")
        if (-not (Test-Path $DockerExe)) { Write-Host "Docker Desktop didn't install. Get it from https://www.docker.com/products/docker-desktop and run this again." -ForegroundColor Red; exit 1 }
    }
    if (-not (Test-DockerRunning)) {
        Write-Host "Starting Docker Desktop. If it opens a window asking for something, answer it..."
        Start-Process $DockerExe
        Wait-Until "Docker" { Test-DockerRunning } 300
    }
}

# Start an existing container by name, or create it with the given `docker run` arguments.
# --restart unless-stopped on create: the container comes back automatically whenever Docker starts.
function Start-Container($Name, [string[]]$RunArgs) {
    $existing = docker ps -a --filter "name=^$Name$" --format "{{.Names}}"
    if ($existing -eq $Name) {
        Write-Host "Starting existing $Name container..."
        docker start $Name | Out-Null
    } else {
        Write-Host "Creating $Name container..."
        docker run -d --name $Name --restart unless-stopped @RunArgs | Out-Null
    }
}

# Redis, ClamAV and Postgres answer on this computer only (127.0.0.1). A container made before that listens on every network, and a running
# container's ports can't be changed: it's replaced. Nothing is lost: Postgres and ClamAV keep their data in volumes, and Redis only holds login sessions (people log in again).
function Close-Port($Name) {
    $ports = docker inspect $Name --format '{{json .HostConfig.PortBindings}}' 2>$null
    if ($ports -and $ports -notmatch '127\.0\.0\.1') {
        Write-Host "$Name listens on every network: replacing its container so only this computer can reach it..." -ForegroundColor Yellow
        docker rm -f $Name | Out-Null
        Start-Sleep -Seconds 5  # Docker takes a moment to free the port: the new container can't bind it before that
    }
}

# Run a check command until it succeeds or the timeout passes. -Soft: the app runs without this service, so it's a note, not the end.
function Wait-Until($Label, [scriptblock]$Check, $Timeout, [switch]$Soft) {
    $elapsed = 0
    while (-not (& $Check)) {
        if ($elapsed -ge $Timeout) {
            Write-Host "$Label didn't come up within $Timeout seconds." -ForegroundColor Red
            if ($Soft) { return }
            exit 1
        }
        Start-Sleep -Seconds 5
        $elapsed += 5
        Write-Host "  waiting for $Label... ($elapsed s)"
    }
    Write-Host "$Label is up." -ForegroundColor Green
}

# What the app can't run without.
function Start-RequiredServices {
    # Redis: this computer only, with the password in .env (REDIS_PASSWORD; changing it means removing the container so it's made again: docker rm -f redis)
    Close-Port "redis"
    $RedisAuth = if ($Settings.REDIS_PASSWORD) { @("-a", $Settings.REDIS_PASSWORD, "--no-auth-warning") } else { @() }
    Start-Container "redis" (@("-p", "127.0.0.1:6379:6379", "redis:7-alpine") + $(if ($Settings.REDIS_PASSWORD) { @("redis-server", "--requirepass", $Settings.REDIS_PASSWORD) } else { @() }))
    Wait-Until "Redis on localhost:6379" { (docker exec redis redis-cli @RedisAuth ping) -eq "PONG" } 30

    # ClamAV, this computer only (the clamav-db volume keeps virus definitions between restarts; the image updates them itself)
    Close-Port "clamav"
    Start-Container "clamav" @("-p", "127.0.0.1:3310:3310", "-v", "clamav-db:/var/lib/clamav", "clamav/clamav:stable")
    # First start downloads the virus database, which can take a few minutes
    Wait-Until "ClamAV on localhost:3310" { docker exec clamav clamdscan --ping 1 *> $null; $LASTEXITCODE -eq 0 } 600

    # Postgres + pgvector, this computer only (credentials from .env; only applied the first time, when the pgdata volume is created)
    # The pgvector image is official Postgres 17 with the vector extension built in (the app runs CREATE EXTENSION at startup).
    Close-Port "postgres"
    Start-Container "postgres" @(
        "-p", "127.0.0.1:$($Settings.POSTGRES_PORT):5432",
        "-e", "POSTGRES_USER=$($Settings.POSTGRES_USER)",
        "-e", "POSTGRES_PASSWORD=$($Settings.POSTGRES_PASSWORD)",
        "-e", "POSTGRES_DB=$($Settings.POSTGRES_DB)",
        "-v", "pgdata:/var/lib/postgresql/data",
        "pgvector/pgvector:0.8.7-pg17")
    Wait-Until "Postgres on localhost:$($Settings.POSTGRES_PORT)" { docker exec postgres pg_isready -q -U $Settings.POSTGRES_USER; $LASTEXITCODE -eq 0 } 60
}

function Get-VllmRunningSettings {
    if (docker ps -a --filter "name=^vllm$" --format "{{.Names}}") { return (docker inspect vllm --format '{{json .Config.Labels}}' | ConvertFrom-Json).settings }
}

# LibreTranslate: the pages in other languages. Bound to 127.0.0.1 only. The libretranslate_models volume keeps its language models; the first start downloads them.
function Start-Translator {
    Start-Container "libretranslate" @("-p", "127.0.0.1:5000:5000", "-v", "libretranslate_models:/home/libretranslate/.local", "-e", "LT_LOAD_ONLY=en,hi,ar,de,fr,nl,es,it,zh", "libretranslate/libretranslate:v1.9.6")
    Wait-Until "LibreTranslate on localhost:5000 (the first start downloads its language models)" { try { (Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 http://127.0.0.1:5000/languages).StatusCode -eq 200 } catch { $false } } 1800 -Soft
}

# Whisper: live meeting captions (whisper_service/: faster-whisper, large-v3-turbo, int8 on the CPU). Built here from the Dockerfile,
# bound to 127.0.0.1 only (no login). The whisper-models volume keeps the model; the first start downloads it (about 1.6 GB).
function Start-Whisper {
    $WhisperModel = if ($Settings.WHISPER_MODEL) { $Settings.WHISPER_MODEL } else { "large-v3-turbo" }
    if (-not (docker images -q deminic-whisper)) {
        Write-Host "Building the whisper image (first time only)..." -ForegroundColor Yellow
        docker build -t deminic-whisper "$PSScriptRoot\whisper_service"
    }
    Start-Container "whisper" @("-p", "127.0.0.1:9100:9000", "-e", "WHISPER_MODEL=$WhisperModel", "-e", "WHISPER_THREADS=8", "-v", "whisper-models:/models", "deminic-whisper")
    Wait-Until "Whisper on localhost:9100 (the first start downloads its model)" { try { $null -ne (Invoke-RestMethod -TimeoutSec 3 http://127.0.0.1:9100/health) } catch { $false } } 1800 -Soft
}

# vLLM: the local AI on the graphics card (Deminic, AI reviews, the finder, meeting notes, the shop), one model for all of it: Gemma 3 12B, which reads pictures too.
# Bound to 127.0.0.1 only: its API has no login. The vllm-models volume keeps the model and what's compiled. Optional for the app: when it's down the AI's light turns red and its buttons are disabled.
# VLLM_GPU in .env picks the card: amd, nvidia or none. Change it (or VLLM_PARALLEL, VLLM_MAX_LEN, VLLM_MODEL), run this script again, and the container is replaced.
function Start-Vllm {
    if ($VllmGpu -eq "none") { Write-Host "No AMD or NVIDIA card (VLLM_GPU=none in .env): the app runs with its AI buttons turned off."; return }
    if (-not (docker images -q $VllmImage)) {
        if ((Get-PSDrive C).Free / 1GB -lt 65) { Write-Host "The local AI needs about 60 GB of free disk space: leaving it out. The app runs with its AI buttons turned off." -ForegroundColor Red; return }
        Write-Host "Building the vLLM image for $VllmGpu (first time only, a download of about 10 GB)..." -ForegroundColor Yellow
        docker build -t $VllmImage -f "$PSScriptRoot\vllm_service\Dockerfile.$VllmGpu" "$PSScriptRoot\vllm_service"
        if ($LASTEXITCODE -ne 0) { Write-Host "The vLLM image didn't build: the app runs with its AI buttons turned off." -ForegroundColor Red; return }
    }
    if ((docker run --rm -v vllm-models:/models alpine sh -c "test -f /models/gemma-3-12b/gemma-3-12b-it-Q4_K_M.gguf && echo yes") -ne "yes") {
        Write-Host "Downloading the model (about 7.6 GB, first time only)..." -ForegroundColor Yellow
        docker run --rm -v vllm-models:/models $VllmImage python -c "from huggingface_hub import hf_hub_download as get; [get('ggml-org/gemma-3-12b-it-GGUF', f, local_dir='/models/gemma-3-12b') for f in ('gemma-3-12b-it-Q4_K_M.gguf', 'mmproj-model-f16.gguf')]"
        if ($LASTEXITCODE -ne 0) { Write-Host "The model didn't download: the app runs with its AI buttons turned off." -ForegroundColor Red; return }
    }
    $VllmOld = Get-VllmRunningSettings
    if ($VllmOld -and $VllmOld -ne $VllmSettings) { Write-Host "vLLM's settings changed ($VllmOld -> $VllmSettings): replacing its container..."; docker rm -f vllm | Out-Null }
    $VllmCard = if ($VllmGpu -eq "amd") {  # Windows hands the card to Docker as /dev/dxg (vllm_service/Dockerfile.amd). AOTriton: without it, reading a picture alone wants 8 GB of the card's memory
        @("--device", "/dev/dxg", "-v", "/usr/lib/wsl:/usr/lib/wsl:ro", "--security-opt", "seccomp=unconfined", "-e", "TORCH_ROCM_AOTRITON_ENABLE_EXPERIMENTAL=1")
    } else { @("--gpus", "all") }
    Start-Container "vllm" (@("-p", "127.0.0.1:8100:8000", "--label", "settings=$VllmSettings", "--ipc=host", "-v", "vllm-models:/models", "-e", "VLLM_CACHE_ROOT=/models/vllm-cache", "-e", "TRITON_CACHE_DIR=/models/triton-cache") + $VllmCard + @(
        $VllmImage, "vllm", "serve", "/models/gemma-3-12b/gemma-3-12b-it-Q4_K_M.gguf", "--served-model-name", $VllmModel, "--tokenizer", "unsloth/gemma-3-12b-it", "--dtype", "float32",  # float32: GGUF allows float16 or float32, vLLM refuses float16 for Gemma 3
        "--max-model-len", $VllmMaxLen, "--max-num-seqs", $VllmParallel, "--max-num-batched-tokens", "512", "--gpu-memory-utilization", "0.85", "--limit-mm-per-prompt.image", "1",  # 512: its picture budget (2,048 would rehearse 8 pictures at once); the app sends one picture at most
        "--structured-outputs-config.backend", "xgrammar", "--structured-outputs-config.disable_any_whitespace", "true", "--host", "0.0.0.0", "--port", "8000"))  # no whitespace: or JSON answers fill up with blank lines until they're cut off
    Wait-Until "vLLM on localhost:8100 (the first start compiles for some minutes)" { try { (Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 http://127.0.0.1:8100/health).StatusCode -eq 200 } catch { $false } } 900 -Soft
}

# The second window opens only when there's something to do: a container that isn't running, or vLLM's settings changed in .env.
function Start-OptionalWindow {
    $Running = @(docker ps --format "{{.Names}}")
    $Wanted = @("libretranslate", "whisper") + $(if ($VllmGpu -ne "none") { @("vllm") } else { @() })
    $Stale = $VllmGpu -ne "none" -and (Get-VllmRunningSettings) -and (Get-VllmRunningSettings) -ne $VllmSettings
    if (@($Wanted | Where-Object { $_ -notin $Running }).Count -or $Stale) {
        Write-Host "Setting up the AI, captions and languages in a minimized window. The app doesn't wait for them." -ForegroundColor Yellow
        Start-Process powershell -WindowStyle Minimized -ArgumentList "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$PSCommandPath`"", "-Optional"
    }
}

# Opens the browser once the app answers. A job: the app itself holds this window.
function Open-WhenReady($Url) {
    Start-Job -ArgumentList $Url -ScriptBlock {
        param($Url)
        for ($i = 0; $i -lt 300; $i++) {
            try { Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 "$Url/csrf" | Out-Null; Start-Process $Url; return } catch { Start-Sleep -Seconds 1 }
        }
    } | Out-Null
}

if ($MyInvocation.InvocationName -eq ".") { return }  # dot-sourced by a test: just the functions
Set-Location $PSScriptRoot
New-EnvFile
New-Folders
Read-Settings

if ($Optional) {
    $Lock = New-Object Threading.Mutex($false, "Local\DeminicOptionalServices")
    if (-not $Lock.WaitOne(0)) { exit 0 }  # another window is already on it
    $Host.UI.RawUI.WindowTitle = "Deminic: setting up the AI, captions and languages (you can use the app meanwhile)"
    Start-Transcript -Path "$PSScriptRoot\logs\setup_optional.log" -Append | Out-Null
    Start-Translator
    Start-Whisper
    Start-Vllm
    Stop-Transcript | Out-Null
    exit 0
}

$Port = if ($env:PORT) { $env:PORT } elseif ($Settings.PORT) { $Settings.PORT } else { "8000" }
$Url = "http://127.0.0.1:$Port"
if ($Start -and (Test-App $Url)) {
    Write-Host "Already running: $Url" -ForegroundColor Green
    if (-not $NoBrowser) { Start-Process $Url }
    exit 0
}
if ($Start) { Install-Packages }
Start-Docker
Stop-IfDataWithoutEnv
Start-RequiredServices
Start-OptionalWindow
if ($Start) {
    if (-not $NoBrowser) { Open-WhenReady $Url }
    Write-Host "Starting the app: $Url  (close this window to stop it)" -ForegroundColor Green
    & $AppPython "$PSScriptRoot\app.py"
    exit $LASTEXITCODE
}
