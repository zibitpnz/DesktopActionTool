<#
.SYNOPSIS
Check, update or roll back this DesktopActionTool installation.
.DESCRIPTION
No arguments checks the latest stable release. Installation is explicit (-Update).
Stop MCP/CLI before updating; settings, screenshots and client paths are preserved.
#>
[CmdletBinding(DefaultParameterSetName = 'Check')]
param(
    [Parameter(ParameterSetName = 'Check')][switch]$Check,
    [Parameter(Mandatory, ParameterSetName = 'Update')][switch]$Update,
    [Parameter(Mandatory, ParameterSetName = 'Rollback')][switch]$Rollback,
    [Parameter(Mandatory, ParameterSetName = 'Status')][switch]$Status,
    [string]$PythonPath,
    [string]$UvPath,
    [Parameter(ParameterSetName = 'Update')][switch]$Offline,
    [switch]$Pause,
    [switch]$Help
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$OutputEncoding = [Console]::OutputEncoding
if ($Help) {
    Write-Output @'
DesktopActionTool updater (Windows x64, PowerShell 5.1+, base Python 3.14+)
  update.bat                  Check for a release; wait for Enter before closing.
  update.bat -Check            Check only (also the default for update.ps1).
  update.bat -Update           Install the latest stable release with backup.
  update.bat -Status           Show local update/recovery state; no network.
  update.bat -Rollback         Restore the last update's source and environment.
  update.bat -Update -Offline  Cache-only Python/packages; GitHub still needs HTTPS.
  update.bat -Update -UvPath C:\Tools\uv.exe
  update.bat -PythonPath C:\Python314\python.exe -Status
  update.bat -Help
The same arguments work with update.ps1. Explicit arguments do not pause unless
-Pause is supplied. Close this installation's MCP/CLI sessions before Update or
Rollback, then reconnect the MCP client. No processes are stopped automatically.
Local source edits block replacement. Settings, screenshots, _local_tools, .git
and other user files are preserved. Backups remain in .tools/updater.
Run install.bat first if this copy has no environment. No scheduled updates.
'@
    exit 0
}

$projectDirectory = [IO.Path]::GetFullPath($PSScriptRoot)
$updaterExitCode = 1
$savedEnvironment = @{}

function Assert-NormalPath([string]$Path) {
    $itemPath = [IO.Path]::GetFullPath($Path)
    while ($itemPath) {
        if (Test-Path -LiteralPath $itemPath) {
            $item = Get-Item -LiteralPath $itemPath -Force
            if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
                throw "Links/junctions are not supported: $itemPath"
            }
        }
        $itemPath = [IO.Path]::GetDirectoryName($itemPath)
    }
}

function Get-BasePython {
    $candidates = @()
    if ($PythonPath) {
        $candidates += (Resolve-Path -LiteralPath $PythonPath).ProviderPath
    } else {
        $configuration = Join-Path $projectDirectory '.venv\pyvenv.cfg'
        Assert-NormalPath $configuration
        if (Test-Path -LiteralPath $configuration -PathType Leaf) {
            foreach ($line in (Get-Content -LiteralPath $configuration -Encoding UTF8)) {
                if ($line -match '^home\s*=\s*(.+)$') { $candidates += Join-Path $Matches[1].Trim() 'python.exe' }
            }
        }
        $recovery = Join-Path $projectDirectory '.tools\updater\recovery.json'
        Assert-NormalPath $recovery
        if (Test-Path -LiteralPath $recovery -PathType Leaf) {
            $record = Get-Content -LiteralPath $recovery -Raw -Encoding UTF8 | ConvertFrom-Json
            if ($record.root -eq $projectDirectory) { $candidates += $record.python }
        }
        $command = Get-Command python.exe -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($command) { $candidates += $command.Source }
    }
    foreach ($candidate in ($candidates | Select-Object -Unique)) {
        if (-not $candidate -or -not (Test-Path -LiteralPath $candidate -PathType Leaf)) { continue }
        Assert-NormalPath $candidate
        $absolute = [IO.Path]::GetFullPath($candidate)
        if ($absolute.StartsWith((Join-Path $projectDirectory '.venv') + '\', [StringComparison]::OrdinalIgnoreCase)) { continue }
        if ([IO.Path]::GetExtension($absolute) -ne '.exe') { continue }
        $answer = & $absolute -I -B -c "import json,struct,sys; print(json.dumps({'ok':sys.version_info >= (3,14) and struct.calcsize('P') == 8 and sys.prefix == sys.base_prefix,'path':sys.executable}))"
        if ($LASTEXITCODE -eq 0) {
            $result = [string]$answer | ConvertFrom-Json
            if ($result.ok) { return $result.path }
        }
    }
    throw 'Base Python 3.14+ x64 was not found outside .venv. Supply -PythonPath C:\path\python.exe.'
}

try {
    if (-not [Environment]::Is64BitOperatingSystem -or $PSVersionTable.PSVersion -lt [version]'5.1') {
        throw 'Windows x64 and PowerShell 5.1 or newer are required.'
    }
    # Do not execute Python startup code or redirect uv using inherited settings.
    foreach ($name in @('PYTHONPATH', 'PYTHONHOME', 'VIRTUAL_ENV')) {
        $savedEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
        [Environment]::SetEnvironmentVariable($name, $null, 'Process')
    }
    Assert-NormalPath $projectDirectory
    $basePython = Get-BasePython
    $action = $PSCmdlet.ParameterSetName.ToLowerInvariant()
    $core = Join-Path $projectDirectory 'desktop_action_tool\update_runtime.py'
    $active = Join-Path $projectDirectory '.tools\updater\active.json'
    $recoveryCore = Join-Path $projectDirectory '.tools\updater\recovery.py'
    Assert-NormalPath $active
    if (($Rollback -or $Status) -and (Test-Path -LiteralPath $active) -and (Test-Path -LiteralPath $recoveryCore -PathType Leaf)) {
        $core = $recoveryCore
    }
    Assert-NormalPath $core
    if (-not (Test-Path -LiteralPath $core -PathType Leaf)) { throw 'Updater module is missing. Restore it from the release archive.' }
    $arguments = @('-I', '-B', $core, '--root', $projectDirectory, '--action', $action)
    if ($UvPath) { $arguments += @('--uv', (Resolve-Path -LiteralPath $UvPath).ProviderPath) }
    if ($Offline) { $arguments += '--offline' }
    & $basePython @arguments
    $updaterExitCode = $LASTEXITCODE
} catch {
    [Console]::Error.WriteLine($_.Exception.Message)
    $updaterExitCode = 1
} finally {
    foreach ($entry in $savedEnvironment.GetEnumerator()) {
        [Environment]::SetEnvironmentVariable($entry.Key, $entry.Value, 'Process')
    }
    if ($Pause) { [void](Read-Host 'Press Enter to close') }
}
exit $updaterExitCode
