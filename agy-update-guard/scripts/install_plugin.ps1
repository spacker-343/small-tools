[CmdletBinding()]
param(
    [switch]$Copy,
    [switch]$Junction,
    [switch]$Force
)

$ErrorActionPreference = 'Stop'

if ($Copy -and $Junction) {
    Write-Error "Cannot specify both -Copy and -Junction."
    exit 1
}
# Real directory copy is the default on Windows because Antigravity Desktop's
# language_server GetAllPlugins directory scanner skips NTFS ReparsePoints.
$UseJunction = [bool]$Junction

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$PluginSource = (Resolve-Path (Join-Path $ScriptDir '..')).Path
$PluginsRoot = Join-Path $HOME '.gemini\config\plugins'
$TargetDir = Join-Path $PluginsRoot 'agy-update-guard'

Write-Host "[1/4] Validating plugin with 'agy plugin validate --strict'..."
& agy plugin validate --strict $PluginSource
if ($LASTEXITCODE -ne 0) {
    Write-Error "Plugin validation failed (exit code $LASTEXITCODE). Aborting installation."
    exit $LASTEXITCODE
}

if (-not (Test-Path -LiteralPath $PluginsRoot)) {
    New-Item -ItemType Directory -Path $PluginsRoot -Force | Out-Null
}

function Sync-PluginDirectory {
    param(
        [Parameter(Mandatory = $true)][string]$Source,
        [Parameter(Mandatory = $true)][string]$Destination
    )
    if (-not (Test-Path -LiteralPath $Destination)) {
        New-Item -ItemType Directory -Path $Destination -Force | Out-Null
    }

    $SourceItems = Get-ChildItem -LiteralPath $Source -Force | Where-Object {
        $_.Name -notin @('__pycache__', '.pytest_cache') -and $_.Extension -notin @('.pyc', '.pyo')
    }
    $ValidNames = @{}
    foreach ($Item in $SourceItems) {
        $ValidNames[$Item.Name] = $true
        $DestChild = Join-Path $Destination $Item.Name
        if ($Item.PSIsContainer) {
            Sync-PluginDirectory -Source $Item.FullName -Destination $DestChild
        } else {
            Copy-Item -LiteralPath $Item.FullName -Destination $DestChild -Force
        }
    }

    # Remove stale files/dirs not present in Source (ignoring locked active CWD dirs if any)
    Get-ChildItem -LiteralPath $Destination -Force -ErrorAction SilentlyContinue | Where-Object {
        -not $ValidNames.ContainsKey($_.Name)
    } | ForEach-Object {
        Remove-Item -LiteralPath $_.FullName -Recurse -Force -ErrorAction SilentlyContinue
    }
}

Write-Host "[2/4] Installing plugin to $TargetDir..."
$NormSource = [System.IO.Path]::GetFullPath($PluginSource).TrimEnd('\')
$ExistingItem = Get-Item -LiteralPath $TargetDir -Force -ErrorAction SilentlyContinue

if ($null -ne $ExistingItem) {
    $IsReparse = ($ExistingItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0

    if ($UseJunction -and $IsReparse -and -not $Force) {
        $ResolvedTarget = $null
        if ($ExistingItem.Target) {
            $ResolvedTarget = [System.IO.Path]::GetFullPath(($ExistingItem.Target | Select-Object -First 1)).TrimEnd('\')
        }
        if ($ResolvedTarget -and ($ResolvedTarget -ieq $NormSource)) {
            Write-Host "Junction already exists and points to $NormSource (idempotent)."
        } else {
            Write-Error "Target '$TargetDir' already exists as a junction pointing to '$ResolvedTarget'. Re-run with -Force to replace."
            exit 1
        }
    } elseif ($Force) {
        if ($IsReparse) {
            cmd /c rmdir "$TargetDir" | Out-Null
        } elseif ($UseJunction) {
            Remove-Item -LiteralPath $TargetDir -Recurse -Force
        }
        # When -Force is used in default Copy mode on an existing real directory,
        # keep directories in-place and sync files so Windows CWD locks from an
        # active sidecar never fail Remove-Item.
    } else {
        Write-Error "Target '$TargetDir' already exists. Re-run with -Force to overwrite or recreate."
        exit 1
    }
}

$PostCleanupItem = Get-Item -LiteralPath $TargetDir -Force -ErrorAction SilentlyContinue
if (-not $UseJunction) {
    if ($null -eq $PostCleanupItem -or $Force) {
        Sync-PluginDirectory -Source $NormSource -Destination $TargetDir
        Write-Host "Synced plugin directory to $TargetDir (in-place safe, bytecode caches excluded)."
    }
} else {
    if ($null -eq $PostCleanupItem) {
        New-Item -ItemType Junction -Path $TargetDir -Target $NormSource | Out-Null
        Write-Host "Created NTFS Junction: $TargetDir -> $NormSource (note: Desktop language_server may skip junctions; omit -Junction for Desktop use)."
    }
}

Write-Host "[3/4] Initializing zero-quota baseline..."
$DataDir = Join-Path $HOME '.gemini\antigravity\plugin_data\agy-update-guard'
if (-not (Test-Path -LiteralPath $DataDir)) {
    New-Item -ItemType Directory -Path $DataDir -Force | Out-Null
}
$ToolsRoot = Split-Path -Parent $NormSource
$CompanionCfg = @{
    tools_root      = $ToolsRoot
    chat_editor_dir = (Join-Path $ToolsRoot 'antigravity-chat-editor')
    fork_dir        = (Join-Path $ToolsRoot 'antigravity-fork')
}
$CompanionJson = $CompanionCfg | ConvertTo-Json -Depth 3
[System.IO.File]::WriteAllText((Join-Path $DataDir 'companion_paths.json'), $CompanionJson, [System.Text.UTF8Encoding]::new($false))

$SentinelScript = Join-Path $TargetDir 'sidecars\update-sentinel\sentinel.py'
& python $SentinelScript --init-baseline
if ($LASTEXITCODE -ne 0) {
    Write-Warning "Baseline initialization exited with code $LASTEXITCODE (you can re-run: python `"$SentinelScript`" --init-baseline)."
}

# Refresh MCP server inside running Antigravity instance if env credentials exist (best-effort)
if ($env:ANTIGRAVITY_LS_ADDRESS -and $env:ANTIGRAVITY_CSRF_TOKEN) {
    try {
        $RefreshUri = "http://$($env:ANTIGRAVITY_LS_ADDRESS)/exa.language_server_pb.LanguageServerService/RefreshMcpServers"
        $Headers = @{
            'Content-Type' = 'application/json'
            'x-codeium-csrf-token' = $env:ANTIGRAVITY_CSRF_TOKEN
        }
        $Body = '{"serverName": "agy-update-guard_update-guard"}'
        $null = Invoke-RestMethod -Uri $RefreshUri -Method Post -Headers $Headers -Body $Body -TimeoutSec 5
        Write-Host "Refreshed MCP server 'agy-update-guard_update-guard' in active Antigravity session."
    } catch {
        # Silently ignore when MCP server is not yet active or session RPC is unavailable
    }
}

Write-Host "[4/4] Done! In-app usage guide:"
Write-Host "  1. Open customization://agy-update-guard (or Overview -> Plugins -> Update Guard) to verify the plugin is enabled."
Write-Host "  2. When updates are detected, new conversations show a silent in-app alert banner (0 tokens)."
Write-Host "  3. Click '+' in the side pane -> 'Update Guard' (or run /update-audit) to open the status card and click '🚀 新建对话审查本次更新' to start a new review conversation in one click."
Write-Host "  4. Optional: enable 'Update Guard Sentinel' in sidecar://dashboard for scheduled 4-hour background checks even without opening new sessions."
