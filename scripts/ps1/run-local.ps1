<#
.SYNOPSIS
    Run Cortex on this PC from the project's .venv (no Docker).

.DESCRIPTION
    Operational tooling (SDSI §3), not part of the shipped application.
    Wired like the container (sdsi:deploy, local wiring mirrors deployed wiring):
    src/ on PYTHONPATH, CORTEX_ENV=local with config/override/local.yaml, and
    the two secrets as files in ./secrets (gitignored). Idempotent:
      1. creates .venv with `python -m venv` if missing, and installs
         requirements-dev.txt whenever that file changed since the last install;
      2. creates the secrets when missing: asks once for a local admin password,
         generates the session key;
      3. with -BrainPath, points ./data/brain at that folder (a directory
         junction, no copy): Cortex then reads and writes that brain;
      4. runs validate-config, then serves http://localhost:8765 until Ctrl+C.

.PARAMETER BrainPath
    A brain folder to serve instead of ./data/brain, e.g. S:\Backup\Markdown\claude-brain.
    Cortex writes to it (SYNAPSE, ENGRAM, approved commits), as it would on the NAS.

.PARAMETER CheckOnly
    Set everything up and run validate-config, then stop without serving.

.PARAMETER NoBrowser
    Don't open the GUI in the browser.

.PARAMETER ResetAdminPassword
    Ask for a new local admin password.

.EXAMPLE
    .\scripts\ps1\run-local.ps1
.EXAMPLE
    .\scripts\ps1\run-local.ps1 -BrainPath S:\Backup\Markdown\claude-brain
.EXAMPLE
    .\scripts\ps1\run-local.ps1 -CheckOnly
#>
#Requires -Version 7
[CmdletBinding()]
param(
    [string]$BrainPath,
    [switch]$CheckOnly,
    [switch]$NoBrowser,
    [switch]$ResetAdminPassword
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "../..")).Path
Set-Location $repoRoot
$venvPython = Join-Path $repoRoot ".venv/Scripts/python.exe"
$requirements = Join-Path $repoRoot "requirements-dev.txt"
$installedStamp = Join-Path $repoRoot ".venv/requirements-dev.sha256"
$secretsDir = Join-Path $repoRoot "secrets"
$brainLink = Join-Path $repoRoot "data/brain"
$url = "http://localhost:8765"

function Invoke-Checked {
    param([string]$Description, [scriptblock]$Command)
    Write-Host "==> $Description" -ForegroundColor Cyan
    $global:LASTEXITCODE = 0
    & $Command
    if ($LASTEXITCODE -ne 0) { throw "Failed ($LASTEXITCODE): $Description" }
}

function New-SessionKey {
    $bytes = [byte[]]::new(48)
    [System.Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
    return [Convert]::ToBase64String($bytes)
}

# ---------- 1. the project's Python environment ----------

if (-not (Test-Path $venvPython)) {
    $python = Get-Command python -ErrorAction SilentlyContinue
    if (-not $python) { throw "python isn't on PATH: install Python 3.14 from python.org first." }
    $version = & python -c "import sys; print('%d.%d' % sys.version_info[:2])"
    if ([version]$version -lt [version]"3.14") { throw "python is $version; Cortex needs 3.14 or newer." }
    Invoke-Checked "Creating .venv with Python $version" { python -m venv .venv }
}
$wanted = (Get-FileHash $requirements -Algorithm SHA256).Hash
$installed = if (Test-Path $installedStamp) { (Get-Content $installedStamp -Raw).Trim() } else { "" }
if ($wanted -ne $installed) {
    Invoke-Checked "Installing requirements-dev.txt into .venv" {
        & $venvPython -m pip install --quiet --disable-pip-version-check -r $requirements
    }
    Set-Content -Path $installedStamp -Value $wanted -NoNewline
} else {
    Write-Host "==> .venv is up to date with requirements-dev.txt" -ForegroundColor DarkGray
}

# ---------- 2. secrets (files, like Docker secrets on the NAS) ----------

New-Item -ItemType Directory -Force -Path $secretsDir | Out-Null
$adminFile = Join-Path $secretsDir "cortex-admin-pwd"
$sessionFile = Join-Path $secretsDir "cortex-session-key"
if ($ResetAdminPassword -or -not (Test-Path $adminFile)) {
    if ($env:CORTEX_ADMIN_PWD -and -not $ResetAdminPassword) {
        Write-Host "==> Using CORTEX_ADMIN_PWD from the environment for the admin password" -ForegroundColor DarkGray
    } else {
        $first = Read-Host "Local Cortex admin password" -AsSecureString
        $second = Read-Host "Repeat it" -AsSecureString
        $plain = ConvertFrom-SecureString $first -AsPlainText
        if ($plain -ne (ConvertFrom-SecureString $second -AsPlainText)) { throw "The passwords don't match." }
        if (-not $plain) { throw "The password can't be empty." }
        Set-Content -Path $adminFile -Value $plain -NoNewline
        Remove-Variable plain
        Write-Host "==> Saved the local admin password in secrets\cortex-admin-pwd (gitignored)" -ForegroundColor Cyan
    }
}
if (-not (Test-Path $sessionFile)) {
    Set-Content -Path $sessionFile -Value (New-SessionKey) -NoNewline
    Write-Host "==> Generated secrets\cortex-session-key (gitignored)" -ForegroundColor Cyan
}

# ---------- 3. which brain ----------

if ($BrainPath) {
    $target = (Resolve-Path $BrainPath).Path
    New-Item -ItemType Directory -Force -Path (Split-Path $brainLink) | Out-Null
    $existing = Get-Item $brainLink -Force -ErrorAction SilentlyContinue
    if ($existing -and $existing.LinkType -ne "Junction") {
        if (Get-ChildItem $brainLink -Force | Select-Object -First 1) {
            throw "data\brain is a real folder with files in it; move it aside before pointing it at $target."
        }
        Remove-Item $brainLink -Force
        $existing = $null
    }
    if ($existing -and $existing.Target -ne $target) { $existing.Delete() ; $existing = $null }
    if (-not $existing) { New-Item -ItemType Junction -Path $brainLink -Target $target | Out-Null }
    Write-Host "==> data\brain now points at $target (Cortex reads and writes it)" -ForegroundColor Cyan
} else {
    $linked = Get-Item $brainLink -Force -ErrorAction SilentlyContinue
    $where = if ($linked -and $linked.LinkType -eq "Junction") { "$($linked.Target) (via data\brain)" } else { "data\brain" }
    Write-Host "==> Brain: $where  (-BrainPath <folder> to serve another one)" -ForegroundColor DarkGray
}

# ---------- 4. check, then serve ----------

$env:PYTHONPATH = Join-Path $repoRoot "src"
$env:CORTEX_ENV = "local"
$env:CORTEX_CONFIG_DIR = Join-Path $repoRoot "config"
$env:CORTEX_OVERRIDE_DIR = Join-Path $repoRoot "config/override"

Invoke-Checked "validate-config" { & $venvPython -m cortex validate-config }
if ($CheckOnly) {
    Write-Host "`nReady (-CheckOnly): run this script without -CheckOnly to serve $url" -ForegroundColor Green
    return
}

if (-not $NoBrowser) {
    Start-Job -ScriptBlock { param($address) Start-Sleep -Seconds 2; Start-Process $address } -ArgumentList $url | Out-Null
}
Write-Host "`nServing $url  (Ctrl+C to stop; the log is in logs\cortex.log)" -ForegroundColor Green
& $venvPython -m cortex serve
