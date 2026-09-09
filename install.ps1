<#
.SYNOPSIS
Install DesktopActionTool into this project folder using uv and uv.lock.
.DESCRIPTION
The default mode installs CLI, UI Automation and MCP. No desktop input or
client configuration changes are performed. Run again to update the environment.
.EXAMPLE
.\install.ps1 -Mode full
.EXAMPLE
.\install.ps1 -Mode cli -Offline -UvPath C:\Tools\uv.exe
#>
[CmdletBinding()]
param(
    [ValidateSet('full', 'cli', 'uia', 'mcp')]
    [string]$Mode = 'full',
    [string]$UvPath,
    [switch]$Offline,
    [switch]$Pause,
    [switch]$Help
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

if ($Help) {
    Write-Output @'
DesktopActionTool installer (Windows x64, PowerShell 5.1+)
  install.bat                  Full install; wait for Enter before closing.
  install.bat -Mode full        CLI + UI Automation + MCP (default).
  install.bat -Mode cli         CLI only.
  install.bat -Mode uia         CLI + UI Automation.
  install.bat -Mode mcp         CLI + MCP, without UI Automation.
  install.bat -Offline          Use cached Python/packages and an existing uv.
  install.bat -UvPath C:\Tools\uv.exe
  install.bat -Help
The same arguments work with install.ps1. Explicit arguments do not pause
unless -Pause is supplied. Selecting fewer components removes unused packages
from this project's .venv. Settings and client configurations are preserved.
'@
    exit 0
}

$projectDirectory = [IO.Path]::GetFullPath($PSScriptRoot)
$environmentDirectory = Join-Path $projectDirectory '.venv'
$toolsDirectory = Join-Path $projectDirectory '.tools'
$uvVersion = '0.12.10'
$uvArchiveHash = 'f65744f94072152b1f86ba2aace4d01f1124d9a8ecb235805039e3718c36cac2'
$uvExecutableHash = 'a8bf95637ba520491de06713d718a55b90f18d127980b9531fd8fc5a8e99dc1d'
$savedEnvironment = @{}
$installerExitCode = 1

function Assert-LocalDirectory([string]$Path) {
    $absolute = [IO.Path]::GetFullPath($Path)
    if (-not $absolute.StartsWith($projectDirectory + [IO.Path]::DirectorySeparatorChar,
                                 [StringComparison]::OrdinalIgnoreCase)) {
        throw "Directory is outside this project: $absolute"
    }
    if (Test-Path -LiteralPath $absolute) {
        $item = Get-Item -LiteralPath $absolute -Force
        if (-not $item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
            throw "Expected a normal directory, not a file or link: $absolute"
        }
    }
}

function Test-UvVersion([string]$Executable) {
    $output = & $Executable --version
    if ($LASTEXITCODE -ne 0) { throw "Cannot run uv: $Executable" }
    if (([string]$output) -notmatch '^uv (\d+\.\d+\.\d+)') { throw 'Unrecognized uv version output.' }
    return ([version]$Matches[1] -ge [version]$uvVersion)
}

function Get-UvExecutable {
    if ($UvPath) {
        $explicitPath = (Resolve-Path -LiteralPath $UvPath).ProviderPath
        if (-not (Test-Path -LiteralPath $explicitPath -PathType Leaf) -or
            [IO.Path]::GetExtension($explicitPath) -ne '.exe') { throw '-UvPath must point to uv.exe.' }
        if (-not (Test-UvVersion $explicitPath)) { throw "uv $uvVersion or newer is required." }
        return $explicitPath
    }
    $candidates = @()
    $command = Get-Command uv.exe -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($command) { $candidates += $command.Source }
    if ($env:USERPROFILE) { $candidates += Join-Path $env:USERPROFILE '.local\bin\uv.exe' }
    foreach ($candidate in ($candidates | Select-Object -Unique)) {
        if ((Test-Path -LiteralPath $candidate -PathType Leaf) -and (Test-UvVersion $candidate)) { return $candidate }
    }

    Assert-LocalDirectory $toolsDirectory
    $cachedUv = Join-Path $toolsDirectory "uv-$uvVersion.exe"
    if (Test-Path -LiteralPath $cachedUv -PathType Leaf) {
        if ((Get-FileHash -LiteralPath $cachedUv -Algorithm SHA256).Hash -ne $uvExecutableHash) {
            throw "Cached uv checksum mismatch. Remove this file and retry: $cachedUv"
        }
        return $cachedUv
    }
    if ($Offline) { throw 'uv is unavailable. Install uv first or retry without -Offline.' }

    Write-Host "Downloading uv $uvVersion from astral-sh/uv (SHA-256 checked)..."
    [IO.Directory]::CreateDirectory($toolsDirectory) | Out-Null
    $temporaryId = [Guid]::NewGuid().ToString('N')
    $archivePath = Join-Path $toolsDirectory "uv-$temporaryId.zip"
    $executablePath = Join-Path $toolsDirectory "uv-$temporaryId.exe"
    $oldProtocol = [Net.ServicePointManager]::SecurityProtocol
    try {
        [Net.ServicePointManager]::SecurityProtocol = $oldProtocol -bor [Net.SecurityProtocolType]::Tls12
        $downloadUrl = "https://github.com/astral-sh/uv/releases/download/$uvVersion/uv-x86_64-pc-windows-msvc.zip"
        Invoke-WebRequest -UseBasicParsing -Uri $downloadUrl -OutFile $archivePath -TimeoutSec 120
        if ((Get-FileHash -LiteralPath $archivePath -Algorithm SHA256).Hash -ne $uvArchiveHash) {
            throw 'Downloaded uv archive checksum mismatch; nothing was executed.'
        }
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        $archive = [IO.Compression.ZipFile]::OpenRead($archivePath)
        try {
            $entry = $archive.GetEntry('uv.exe')
            if ($null -eq $entry) { throw 'The uv archive does not contain uv.exe.' }
            # Extract only this fixed entry, never archive-supplied paths.
            [IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $executablePath)
        } finally { $archive.Dispose() }
        if ((Get-FileHash -LiteralPath $executablePath -Algorithm SHA256).Hash -ne $uvExecutableHash) {
            throw 'Extracted uv checksum mismatch; nothing was executed.'
        }
        Move-Item -LiteralPath $executablePath -Destination $cachedUv
        return $cachedUv
    } finally {
        [Net.ServicePointManager]::SecurityProtocol = $oldProtocol
        foreach ($temporaryPath in @($archivePath, $executablePath)) {
            if (Test-Path -LiteralPath $temporaryPath) { Remove-Item -LiteralPath $temporaryPath -Force }
        }
    }
}

try {
    if ($env:OS -ne 'Windows_NT' -or
        ($env:PROCESSOR_ARCHITECTURE -ne 'AMD64' -and $env:PROCESSOR_ARCHITEW6432 -ne 'AMD64')) {
        throw 'This installer supports Windows x64.'
    }
    if ($PSVersionTable.PSVersion.Major -lt 5) { throw 'PowerShell 5.1 or newer is required.' }
    # Validate this directory BEFORE uv can discover a project in a parent directory.
    foreach ($name in @('pyproject.toml', 'uv.lock', '.python-version', 'settings.json',
                        'type_text.py', 'desktop_cli.py', 'configuration.py', 'mcp_server.py',
                        'mcp_bridge.py', 'mcp_contract.py')) {
        if (-not (Test-Path -LiteralPath (Join-Path $projectDirectory $name) -PathType Leaf)) {
            throw "Missing $name next to install.ps1. Extract the complete DesktopActionTool project first."
        }
    }
    $projectText = Get-Content -LiteralPath (Join-Path $projectDirectory 'pyproject.toml') -Raw -Encoding utf8
    $projectSection = [regex]::Match($projectText, '(?ms)^\[project\]\s*\r?\n(.*?)(?=^\[|\z)').Groups[1].Value
    if ($projectSection -notmatch '(?m)^name\s*=\s*"desktopactiontool"\s*$') {
        throw 'This is not a DesktopActionTool project directory.'
    }
    Assert-LocalDirectory $environmentDirectory
    $pythonRequest = (Get-Content -LiteralPath (Join-Path $projectDirectory '.python-version') -Raw).Trim()
    if ($pythonRequest -notmatch '^\d+\.\d+\.\d+$') { throw 'Expected an exact Python version in .python-version.' }
    $lockPath = Join-Path $projectDirectory 'uv.lock'
    $lockHash = (Get-FileHash -LiteralPath $lockPath -Algorithm SHA256).Hash
    $settingsPath = Join-Path $projectDirectory 'settings.json'
    $settingsHash = (Get-FileHash -LiteralPath $settingsPath -Algorithm SHA256).Hash
    foreach ($key in @('UV_PROJECT_ENVIRONMENT', 'VIRTUAL_ENV', 'PYTHONPATH', 'PYTHONHOME',
                        'PYTHONDONTWRITEBYTECODE', 'PYTHONIOENCODING', 'DESKTOPACTION_CONTROLLER')) {
        $savedEnvironment[$key] = [Environment]::GetEnvironmentVariable($key, 'Process')
        [Environment]::SetEnvironmentVariable($key, $null, 'Process')
    }
    $env:UV_PROJECT_ENVIRONMENT = $environmentDirectory
    $env:PYTHONDONTWRITEBYTECODE = '1'
    $env:PYTHONIOENCODING = 'utf-8'

    Write-Host "DesktopActionTool installation: $projectDirectory"
    Write-Host "Components: $Mode; Python: $pythonRequest"
    $uvExecutable = Get-UvExecutable
    Write-Host "Using uv: $uvExecutable"
    $uvArguments = @('--directory', $projectDirectory, 'sync', '--project', $projectDirectory,
                     '--locked', '--no-default-groups', '--python', $pythonRequest)
    if ($Mode -in @('full', 'uia')) { $uvArguments += @('--extra', 'uia') }
    if ($Mode -in @('full', 'mcp')) { $uvArguments += @('--extra', 'mcp') }
    if ($Offline) { $uvArguments += '--offline' }
    & $uvExecutable @uvArguments
    if ($LASTEXITCODE -ne 0) { throw "uv sync failed (exit $LASTEXITCODE). Installation did not complete." }
    $pythonExecutable = Join-Path $environmentDirectory 'Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $pythonExecutable -PathType Leaf)) { throw 'uv did not create the expected Python executable.' }

    Write-Host 'Checking the installation without desktop input...'
    $probe = @'
import importlib.metadata as metadata
import json
from pathlib import Path
import subprocess
import sys
import tomllib
project = Path(sys.argv[1])
mode = sys.argv[2]
sys.path.insert(0, str(project))
expected_python = (project / '.python-version').read_text().strip()
assert '.'.join(map(str, sys.version_info[:3])) == expected_python, 'Unexpected Python version'
assert sys.maxsize > 2**32, '64-bit Python is required'
assert Path(sys.prefix).resolve() == (project / '.venv').resolve(), 'Unexpected environment'
config = tomllib.loads((project / 'pyproject.toml').read_text(encoding='utf-8'))
for extra in ('uia', 'mcp'):
    if mode in ('full', extra):
        for requirement in config['project']['optional-dependencies'][extra]:
            name, version = requirement.split('==')
            assert metadata.version(name) == version, 'Dependency version mismatch: ' + name
def check_cli(*args):
    result = subprocess.run([sys.executable, '-B', str(project / 'type_text.py'), *args],
                            cwd=project, capture_output=True, timeout=30)
    if result.returncode:
        raise RuntimeError(result.stdout.decode('utf-8', errors='replace') + result.stderr.decode('utf-8', errors='replace'))
    return result.stdout
check_cli('--help')
result = json.loads(check_cli('--text', 'Installation check', '--dry-run', '--quiet'))
assert result['ok'] and result['typed_chars'] == 0, 'CLI dry-run failed'
if mode in ('full', 'mcp'):
    from mcp_bridge import Bridge, SERVER_VERSION
    from mcp_server import create_server
    assert SERVER_VERSION == config['project']['version'], 'MCP version mismatch'
    create_server(Bridge(project))
if mode in ('full', 'uia'):
    from uia_worker import use_memory_com_cache
    use_memory_com_cache()
    from controls_backend import import_uiautomation
    import_uiautomation()
print('Installation checks passed: DesktopActionTool ' + config['project']['version'])
'@
    & $pythonExecutable -B -c $probe $projectDirectory $Mode
    if ($LASTEXITCODE -ne 0) { throw "Installation checks failed (exit $LASTEXITCODE)." }
    if ((Get-FileHash -LiteralPath $lockPath -Algorithm SHA256).Hash -ne $lockHash -or
        (Get-FileHash -LiteralPath $settingsPath -Algorithm SHA256).Hash -ne $settingsHash) {
        throw 'uv.lock or settings.json changed during installation; inspect the files.'
    }
    Write-Host ''
    Write-Host 'Installation completed.'
    Write-Host "Python: $pythonExecutable"
    Write-Host "CLI:    $(Join-Path $projectDirectory 'type_text.py')"
    if ($Mode -in @('full', 'mcp')) {
        Write-Host "MCP:    $(Join-Path $projectDirectory 'mcp_server.py')"
        Write-Host 'Use this Python executable as the MCP client command and mcp_server.py as its argument.'
        Write-Host 'See README.md for client configuration; reconnect the client after updating.'
    }
    $installerExitCode = 0
} catch {
    [Console]::Error.WriteLine('ERROR: ' + $_.Exception.Message)
} finally {
    foreach ($key in $savedEnvironment.Keys) {
        [Environment]::SetEnvironmentVariable($key, $savedEnvironment[$key], 'Process')
    }
    if ($Pause) { Read-Host 'Press Enter to close' | Out-Null }
}
exit $installerExitCode
