<#
.SYNOPSIS
    Deploy Cortex to the Synology NAS from this PC.

.DESCRIPTION
    Operational tooling (SDSI §3), not part of the shipped application.
    Modeled on flammeau's scripts/ps1/deploy-nas.ps1: everything runs through
    the `synology` Docker context, so nothing is copied to the NAS by hand.
    One deliberate difference: Cortex isn't built on the NAS. CI builds one
    image per VERSION and pushes it to ghcr.io/jimmydagher/cortex; this pulls
    that exact tag (sdsi:deploy, promote by tag), so the NAS runs what CI tested.

    Every run, idempotently:
      1. creates the NAS folders from .env.nas and gives them to PUID:PGID
         (an existing brain folder's ownership is left alone);
      2. uploads config/override/nas.local.yaml as the NAS's /config/nas.yaml
         (created on the first run from the nas.yaml template, with the NAS's
         address filled in: edit it for extra host names or HTTPS);
      3. writes any missing secret on the NAS: the admin password is asked
         for once, the session key is generated, and the optional read-only
         guest password only with -SetGuestPassword. Values travel over the
         SSH-tunneled Docker API on stdin and are never saved on this PC;
      4. runs `validate-config` in the real container wiring (the pre-flight);
      5. pulls the image, starts it, and waits for /healthz to answer.

    One-time prerequisites (shared with flammeau and life-dashboard): the
    `synology` Docker context and its SSH key. Plus .env.nas, copied from
    .env.nas.example. docs/setup-nas.md has the details.

.PARAMETER Version
    Image tag to deploy. Defaults to the VERSION file (CI must have published it).

.PARAMETER Context
    Docker context for the NAS. Default: synology.

.PARAMETER EnvFile
    Compose wiring for the NAS. Default: .env.nas at the repo root.

.PARAMETER NasConfig
    This deployment's config override, uploaded as /config/nas.yaml.
    Default: config/override/nas.local.yaml (gitignored).

.PARAMETER PreflightOnly
    Stop after validate-config: folders, config and secrets are in place,
    nothing is started or restarted.

.PARAMETER ResetAdminPassword
    Ask for a new admin password and replace the NAS's (signs everyone out).

.PARAMETER RotateSessionKey
    Generate a new session key on the NAS (signs everyone out).

.PARAMETER SetGuestPassword
    Create the read-only guest account, or change its password: asks for a
    guest password (it must differ from the admin's) and writes it to the NAS.
    A guest can browse the brain, graph, SYNAPSE and activity but change nothing.
    Changing it signs out guests only.

.PARAMETER RemoveGuest
    Delete the guest password from the NAS: the guest account stops working
    on the restart this deploy does.

.EXAMPLE
    .\scripts\ps1\deploy-nas.ps1
.EXAMPLE
    .\scripts\ps1\deploy-nas.ps1 -PreflightOnly
.EXAMPLE
    .\scripts\ps1\deploy-nas.ps1 -Version 0.1.0 -ResetAdminPassword
.EXAMPLE
    .\scripts\ps1\deploy-nas.ps1 -SetGuestPassword
#>
#Requires -Version 7
[CmdletBinding()]
param(
    [string]$Version,
    [string]$Context = "synology",
    [string]$EnvFile,
    [string]$NasConfig,
    [switch]$PreflightOnly,
    [switch]$ResetAdminPassword,
    [switch]$RotateSessionKey,
    [switch]$SetGuestPassword,
    [switch]$RemoveGuest
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "../..")).Path
Set-Location $repoRoot
$composeFile = Join-Path $repoRoot "docker-compose.yml"
if (-not $EnvFile) { $EnvFile = Join-Path $repoRoot ".env.nas" }
if (-not $NasConfig) { $NasConfig = Join-Path $repoRoot "config/override/nas.local.yaml" }
$adminSecret = "cortex-admin-pwd"
$sessionSecret = "cortex-session-key"
$guestSecret = "cortex-guest-pwd"
$healthWaitSeconds = 60
if ($SetGuestPassword -and $RemoveGuest) { throw "Use -SetGuestPassword or -RemoveGuest, not both." }

function Invoke-Checked {
    param([string]$Description, [scriptblock]$Command)
    Write-Host "==> $Description" -ForegroundColor Cyan
    # $LASTEXITCODE, not $?: inside a script block $? reports whether the block ran,
    # not whether docker succeeded (see flammeau's deploy-nas.ps1).
    $global:LASTEXITCODE = 0
    & $Command
    if ($LASTEXITCODE -ne 0) { throw "Failed ($LASTEXITCODE): $Description" }
}

function Read-EnvFile {
    param([string]$Path)
    $values = @{}
    foreach ($line in Get-Content $Path) {
        if ($line -match '^\s*([A-Z_][A-Z0-9_]*)\s*=\s*(.*?)\s*$') { $values[$Matches[1]] = $Matches[2] }
    }
    return $values
}

# Run a shell command as root in a throwaway Cortex container on the NAS, with the
# given NAS folders mounted. Returns the exit code; -InputText goes to its stdin.
function Invoke-OnNas {
    param([string[]]$Mounts, [string]$Script, [string]$InputText)
    $arguments = @("--context", $Context, "run", "--rm", "-i", "--user", "0", "--entrypoint", "sh")
    foreach ($mount in $Mounts) { $arguments += @("-v", $mount) }
    $arguments += @($image, "-c", $Script)
    $global:LASTEXITCODE = 0
    if ($PSBoundParameters.ContainsKey("InputText")) { $InputText | docker @arguments } else { "" | docker @arguments }
    return $LASTEXITCODE
}

function New-SessionKey {
    $bytes = [byte[]]::new(48)
    [System.Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
    return [Convert]::ToBase64String($bytes)
}

# ---------- preconditions ----------

if (-not (Test-Path $EnvFile)) {
    throw "$EnvFile not found: copy .env.nas.example to .env.nas and check its values first (docs/setup-nas.md)."
}
$wiring = Read-EnvFile $EnvFile
foreach ($name in "CORTEX_DATA_PATH", "CORTEX_CONFIG_PATH", "CORTEX_LOGS_PATH", "CORTEX_SECRETS_PATH", "PUID", "PGID", "CORTEX_PORT") {
    if (-not $wiring[$name]) { throw "$EnvFile is missing $name (see .env.nas.example)." }
}
if (-not $Version) { $Version = (Get-Content (Join-Path $repoRoot "VERSION") -Raw).Trim() }
$env:CORTEX_VERSION = $Version  # the shell wins over --env-file in compose, so -Version applies everywhere below
$image = "ghcr.io/jimmydagher/cortex:$Version"
$owner = "$($wiring.PUID):$($wiring.PGID)"

$endpoint = docker context inspect $Context --format '{{.Endpoints.docker.Host}}' 2>$null
if ($LASTEXITCODE -ne 0 -or -not $endpoint) { throw "Docker context '$Context' not found (docker context ls). docs/setup-nas.md › Before you start." }
$nasHost = if ($endpoint -match '^ssh://(?:[^@]+@)?([^:/]+)') { $Matches[1] } else { "localhost" }
Write-Host "Deploying Cortex $Version to $nasHost (context '$Context')" -ForegroundColor Green

Write-Host "==> Pulling $image on the NAS" -ForegroundColor Cyan
$global:LASTEXITCODE = 0
docker --context $Context pull --quiet $image
if ($LASTEXITCODE -ne 0) {
    # Docker's "manifest unknown" means GHCR has no such tag: CI publishes a version only after its run on main.
    throw "Couldn't pull $image. If Docker said 'manifest unknown', CI hasn't published $Version yet: wait for the run on main to finish (https://github.com/jimmydagher/cortex/actions), or pass -Version with a published tag."
}

# ---------- 1. folders ----------

$data, $config, $logs, $secrets = $wiring.CORTEX_DATA_PATH, $wiring.CORTEX_CONFIG_PATH, $wiring.CORTEX_LOGS_PATH, $wiring.CORTEX_SECRETS_PATH
Write-Host "==> Creating NAS folders and setting their owner to $owner" -ForegroundColor Cyan
# Everything Cortex owns (its state, config, logs, secrets) goes to PUID:PGID, files included: a secret
# written by hand or by another account would otherwise be unreadable. The brain is the human's:
# only a brand-new, empty brain folder is handed over.
$folders = 'mkdir -p /d/brain /d/cortex && chown ' + $owner + ' /d && chown -R ' + $owner + ' /d/cortex /c /l /s && ' +
           'chmod 700 /s && find /s -type f -exec chmod 600 {} + && ' +
           'if [ -z "$(ls -A /d/brain)" ]; then chown ' + $owner + ' /d/brain; fi'
if ((Invoke-OnNas -Mounts "${data}:/d", "${config}:/c", "${logs}:/l", "${secrets}:/s" -Script $folders) -ne 0) {
    throw "Couldn't create or chown the NAS folders."
}

# ---------- 2. config ----------

if (-not (Test-Path $NasConfig)) {
    $template = Get-Content (Join-Path $repoRoot "config/override/nas.yaml") -Raw
    $filled = $template -replace 'allowed_hosts: <not configured>', "allowed_hosts: [`"$nasHost`"]"
    $header = "# This NAS's config override, uploaded as /config/nas.yaml by scripts/ps1/deploy-nas.ps1.`n" +
              "# Created from config/override/nas.yaml; gitignored. Edit it, then redeploy.`n"
    Set-Content -Path $NasConfig -Value ($header + $filled) -NoNewline -Encoding utf8
    Write-Host "    Created $NasConfig with allowed_hosts [`"$nasHost`"]; edit it for other host names or HTTPS." -ForegroundColor Yellow
}
Write-Host "==> Uploading $(Split-Path $NasConfig -Leaf) as the NAS's /config/nas.yaml" -ForegroundColor Cyan
$configText = Get-Content $NasConfig -Raw
if ((Invoke-OnNas -Mounts "${config}:/c" -Script "cat > /c/nas.yaml && chown $owner /c/nas.yaml && chmod 644 /c/nas.yaml" -InputText $configText) -ne 0) {
    throw "Couldn't upload the config override."
}

# ---------- 3. secrets ----------

function Test-NasSecret { param([string]$Name) return (Invoke-OnNas -Mounts "${secrets}:/s" -Script "test -s /s/$Name") -eq 0 }
# Cortex reads secrets only at startup, so any change here makes step 5 recreate the container.
$secretsChanged = $false
function Set-NasSecret {
    param([string]$Name, [string]$Value)
    $write = "umask 077 && cat > /s/$Name && chown $owner /s/$Name"
    if ((Invoke-OnNas -Mounts "${secrets}:/s" -Script $write -InputText $Value) -ne 0) { throw "Couldn't write the $Name secret." }
    $script:secretsChanged = $true
}

function Read-NewPassword {
    param([string]$Prompt)
    $first = Read-Host $Prompt -AsSecureString
    $second = Read-Host "Repeat it" -AsSecureString
    $plain = ConvertFrom-SecureString $first -AsPlainText
    if ($plain -ne (ConvertFrom-SecureString $second -AsPlainText)) { throw "The passwords don't match; nothing was changed." }
    if ($plain.Length -lt 12) { throw "Use at least 12 characters; nothing was changed." }
    return $plain
}

if ($ResetAdminPassword -or -not (Test-NasSecret $adminSecret)) {
    Write-Host "==> Setting the GUI admin password on the NAS" -ForegroundColor Cyan
    $plain = Read-NewPassword "New Cortex admin password"
    Set-NasSecret $adminSecret $plain
    Remove-Variable plain
} else {
    Write-Host "==> Admin password already set on the NAS (-ResetAdminPassword to change it)" -ForegroundColor DarkGray
}
if ($RotateSessionKey -or -not (Test-NasSecret $sessionSecret)) {
    Write-Host "==> Generating the session key on the NAS" -ForegroundColor Cyan
    Set-NasSecret $sessionSecret (New-SessionKey)
} else {
    Write-Host "==> Session key already set on the NAS (-RotateSessionKey to replace it)" -ForegroundColor DarkGray
}
# The guest account is optional: it exists exactly when its secret file does. The pre-flight
# below refuses a guest password equal to the admin's.
if ($SetGuestPassword) {
    Write-Host "==> Setting the read-only guest password on the NAS" -ForegroundColor Cyan
    $plain = Read-NewPassword "New Cortex guest password (not the admin's)"
    Set-NasSecret $guestSecret $plain
    Remove-Variable plain
} elseif ($RemoveGuest) {
    Write-Host "==> Removing the guest account from the NAS" -ForegroundColor Cyan
    if ((Invoke-OnNas -Mounts "${secrets}:/s" -Script "rm -f /s/$guestSecret") -ne 0) { throw "Couldn't remove the $guestSecret secret." }
    $secretsChanged = $true
} elseif (Test-NasSecret $guestSecret) {
    Write-Host "==> Guest account on (-SetGuestPassword to change its password, -RemoveGuest to delete it)" -ForegroundColor DarkGray
} else {
    Write-Host "==> No guest account (-SetGuestPassword to create a read-only one)" -ForegroundColor DarkGray
}

# ---------- 4. pre-flight ----------

Invoke-Checked "Pre-flight: validate-config with the NAS wiring" {
    docker --context $Context compose --env-file $EnvFile -f $composeFile run --rm --no-deps cortex validate-config
}
if ($PreflightOnly) {
    Write-Host "`nPre-flight passed (-PreflightOnly): nothing was started or restarted." -ForegroundColor Green
    return
}

# ---------- 5. start and check ----------

# Built by appending, not `$x = if (...) { @(...) }`: an if-expression unwraps a one-item array
# into a string, which PowerShell would then pass to docker one character at a time.
$upArgs = @("up", "-d")
if ($secretsChanged) { $upArgs += "--force-recreate" }
Invoke-Checked "Starting Cortex $Version" {
    docker --context $Context compose --env-file $EnvFile -f $composeFile $upArgs
}
Invoke-Checked "Container status" {
    docker --context $Context compose --env-file $EnvFile -f $composeFile ps
}

$baseUrl = "http://${nasHost}:$($wiring.CORTEX_PORT)"
Write-Host "==> Waiting for $baseUrl/healthz" -ForegroundColor Cyan
$deadline = (Get-Date).AddSeconds($healthWaitSeconds)
$healthy = $false
while (-not $healthy -and (Get-Date) -lt $deadline) {
    try { $healthy = (Invoke-WebRequest "$baseUrl/healthz" -UseBasicParsing -TimeoutSec 3).Content -eq "ok" } catch { Start-Sleep -Seconds 2 }
}
if (-not $healthy) {
    throw "Cortex didn't answer on $baseUrl/healthz within $healthWaitSeconds s. Logs: docker --context $Context compose --env-file .env.nas logs --tail 50 cortex"
}

Write-Host "`nDone. Cortex $Version is running: $baseUrl" -ForegroundColor Green
Write-Host "Logs any time:  docker --context $Context compose --env-file .env.nas logs -f cortex"
