<#
.SYNOPSIS
    Publishes the xam stack to a remote server over SSH.

.DESCRIPTION
    Packs this production-config directory, uploads it to the server, then runs
    deploy.sh there. deploy.sh clones or updates the three repositories, installs
    these configs, builds the images and restarts the stack.

    The server must have Docker Engine, the Docker Compose plugin and git.
    Configure an SSH alias (for example "chh") in %USERPROFILE%\.ssh\config or
    pass -Server user@host.

.PARAMETER Server
    SSH host or alias. Default: chh.

.PARAMETER User
    SSH user. Omit to use the user from your SSH config/alias. Ignored when
    Server already contains "@".

.PARAMETER AppDir
    Directory on the server that contains the three repositories. Default: /opt/xam.

.PARAMETER Branch
    Git branch to deploy. Default: main.

.EXAMPLE
    .\publish.ps1

.EXAMPLE
    .\publish.ps1 -Server root@203.0.113.10 -AppDir /srv/xam
#>
[CmdletBinding()]
param(
    [string]$Server = "chh",
    [string]$User = "",
    [string]$AppDir = "/opt/xam",
    [string]$Branch = "main"
)

$ErrorActionPreference = "Stop"

function Require-Command {
    param([string]$Name)

    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "Command '$Name' not found. Install the OpenSSH client and make sure it is in PATH."
    }
}

Require-Command ssh
Require-Command scp
Require-Command tar

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$envFile = Join-Path $scriptDir ".env.production"

if (-not (Test-Path -LiteralPath $envFile)) {
    Write-Warning "Local .env.production not found. The server must already have $AppDir/xam_call_service/deploy/production/.env.production."
}
else {
    $envText = Get-Content -LiteralPath $envFile -Raw
    if ($envText -match "203\.0\.113\.10") {
        Write-Warning "SERVER_IP/TURN_EXTERNAL_IP is still the placeholder 203.0.113.10. Replace it when the server IP is known."
    }
    if ($envText -match "CHANGE_ME") {
        Write-Warning ".env.production still contains CHANGE_ME placeholders. The deploy script will refuse to start."
    }
}

if ($Server -match "@" -or [string]::IsNullOrWhiteSpace($User)) {
    $target = $Server
}
else {
    $target = "$User@$Server"
}

# The server pulls origin/main, so warn about changes that will not be deployed.
if (Get-Command git -ErrorAction SilentlyContinue) {
    $workspaceRoot = Resolve-Path (Join-Path $scriptDir "../../..") -ErrorAction SilentlyContinue
    if ($workspaceRoot) {
        foreach ($repoName in @("xam_frontend", "xam_backend", "xam_call_service")) {
            $repoPath = Join-Path $workspaceRoot $repoName
            if (-not (Test-Path -LiteralPath (Join-Path $repoPath ".git"))) {
                continue
            }

            $dirty = & git -C $repoPath status --porcelain
            if ($dirty) {
                Write-Warning "$repoName has uncommitted changes; the server deploys origin/main and will not include them."
            }

            $ahead = & git -C $repoPath rev-list --count "@{u}..HEAD" 2>$null
            if ($LASTEXITCODE -eq 0 -and [int]$ahead -gt 0) {
                Write-Warning "$repoName has $ahead unpushed commit(s); push them before publishing."
            }
        }
    }
}

$tempTar = Join-Path ([System.IO.Path]::GetTempPath()) ("xam-production-{0}.tgz" -f [guid]::NewGuid().ToString("N"))

try {
    Write-Host "Packing production configuration from $scriptDir"
    & tar -czf $tempTar -C $scriptDir .
    if ($LASTEXITCODE -ne 0) {
        throw "tar failed with exit code $LASTEXITCODE"
    }

    Write-Host "Preparing /tmp/xam-production-config on $target"
    & ssh $target "rm -rf /tmp/xam-production-config /tmp/xam-production-config.tgz && mkdir -p /tmp/xam-production-config"
    if ($LASTEXITCODE -ne 0) {
        throw "ssh failed with exit code $LASTEXITCODE"
    }

    Write-Host "Uploading configuration to ${target}:/tmp/xam-production-config.tgz"
    & scp $tempTar "${target}:/tmp/xam-production-config.tgz"
    if ($LASTEXITCODE -ne 0) {
        throw "scp failed with exit code $LASTEXITCODE"
    }

    $remoteCommand = @'
chmod 600 /tmp/xam-production-config.tgz 2>/dev/null || true
tar -xzf /tmp/xam-production-config.tgz -C /tmp/xam-production-config || exit 1
chmod 600 /tmp/xam-production-config/.env.production 2>/dev/null || true
sed -i 's/\r$//' /tmp/xam-production-config/deploy.sh || exit 1
if [ -f /tmp/xam-production-config/.env.production ]; then
    sed -i 's/\r$//' /tmp/xam-production-config/.env.production || exit 1
fi
REPO_BRANCH='__BRANCH__' bash /tmp/xam-production-config/deploy.sh '__APPDIR__' '/tmp/xam-production-config'
'@
    $remoteCommand = $remoteCommand.Replace('__BRANCH__', $Branch).Replace('__APPDIR__', $AppDir)
    $remoteCommand = $remoteCommand.Replace("`r`n", "`n")

    Write-Host "Deploying on $target (branch $Branch, app dir $AppDir)"
    & ssh $target $remoteCommand
    if ($LASTEXITCODE -ne 0) {
        throw "remote deployment failed with exit code $LASTEXITCODE"
    }

    Write-Host ""
    Write-Host "Deployment finished."
}
finally {
    if (Test-Path -LiteralPath $tempTar) {
        Remove-Item -LiteralPath $tempTar -Force
    }
}
