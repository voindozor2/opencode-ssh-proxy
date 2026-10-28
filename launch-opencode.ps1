[CmdletBinding()]
param(
    [string]$ProjectDirectory = (Get-Location).Path,
    [string]$SettingsFile = (Join-Path $HOME '.config\opencode-ssh-proxy\settings.local.json'),
    [string]$OpenCodeExecutable
)

$ErrorActionPreference = 'Stop'
$names = @('HTTP_PROXY', 'HTTPS_PROXY', 'NO_PROXY')
$previous = @{}
foreach ($name in $names) {
    $previous[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
}
$code = 1
$pushed = $false
try {
    if (-not (Test-Path -LiteralPath $ProjectDirectory -PathType Container)) {
        throw 'Invalid project directory.'
    }
    if (-not $OpenCodeExecutable) {
        foreach ($command in @(Get-Command opencode -CommandType Application -All)) {
            $candidate = Join-Path (Split-Path $command.Source) 'opencode-cli.exe'
            if (Test-Path -LiteralPath $candidate -PathType Leaf) { $OpenCodeExecutable = $candidate; break }
        }
        if (-not $OpenCodeExecutable) { throw 'Native executable not found.' }
    }
    $OpenCodeExecutable = (Resolve-Path -LiteralPath $OpenCodeExecutable).Path
    # Never use a cmd shim: percent-encoded passwords must not be expanded by cmd.
    if ([IO.Path]::GetExtension($OpenCodeExecutable) -notin @('.exe', '.ps1')) {
        throw 'Native executable required (PowerShell fixture scripts also supported).'
    }
    $settings = [IO.File]::ReadAllText($SettingsFile) | ConvertFrom-Json
    $port = 0
    if (-not [int]::TryParse([string]$settings.listen_port, [ref]$port) -or $port -lt 1 -or $port -gt 65535) {
        throw 'Invalid proxy port.'
    }
    $password = [IO.File]::ReadAllText($settings.password_file).Trim()
    if (-not $password) { throw 'Empty proxy password.' }

    # Availability only: this is not an SSH/authentication/provider health check.
    $tcp = New-Object Net.Sockets.TcpClient
    try {
        $pending = $tcp.ConnectAsync('127.0.0.1', $port)
        if (-not $pending.Wait(3000) -or -not $tcp.Connected) { throw 'Proxy unavailable.' }
    } finally {
        $tcp.Dispose()
    }
    $proxyUrl = 'http://opencode:' + [Uri]::EscapeDataString($password) + '@127.0.0.1:' + $port
    [Environment]::SetEnvironmentVariable('HTTP_PROXY', $proxyUrl, 'Process')
    [Environment]::SetEnvironmentVariable('HTTPS_PROXY', $proxyUrl, 'Process')
    [Environment]::SetEnvironmentVariable('NO_PROXY', 'localhost,127.0.0.1,::1', 'Process')
    Push-Location -LiteralPath $ProjectDirectory
    $pushed = $true
    & $OpenCodeExecutable --standalone
    $code = $LASTEXITCODE
} catch {
    # Do not echo exception objects: they may contain private paths or URLs.
    Write-Output 'Launcher failed. Check project, native CLI, private settings/password and local proxy availability. Private details suppressed.'
    $code = 1
} finally {
    if ($pushed) { Pop-Location }
    foreach ($name in $names) {
        [Environment]::SetEnvironmentVariable($name, $previous[$name], 'Process')
    }
    Remove-Variable password, proxyUrl -ErrorAction SilentlyContinue
}
exit $code
