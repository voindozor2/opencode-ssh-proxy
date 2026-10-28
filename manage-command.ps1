[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][ValidateSet('Install', 'Remove')][string]$Action,
    [string]$CommandDirectory = (Join-Path $HOME '.local\opencode-proxy\bin'),
    [string]$StateDirectory = (Join-Path $HOME '.config\opencode-ssh-proxy'),
    [string]$PythonExecutable,
    [string]$NativeExecutable,
    # Offline tests use a file instead of the actual user registry/PATH broadcast.
    [string]$PathFixture
)
$ErrorActionPreference = 'Stop'
$bin = [IO.Path]::GetFullPath($CommandDirectory).TrimEnd('\')
$state = [IO.Path]::GetFullPath($StateDirectory)
$manifestPath = Join-Path $state 'command-install-before.json'
$runtimePath = Join-Path $state 'command.local.json'
$wrapperPath = Join-Path $bin 'opencode.exe'
$launcher = Join-Path $PSScriptRoot 'command_launcher.py'
$settingsPath = Join-Path $state 'settings.local.json'

function Read-UserPath {
    if ($PathFixture) {
        return ([IO.File]::ReadAllText($PathFixture) | ConvertFrom-Json)
    }
    $key = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey('Environment', $false)
    try {
        if ($null -eq $key -or $key.GetValueNames() -notcontains 'Path') {
            return [pscustomobject]@{ value = $null; kind = 'ExpandString' }
        }
        return [pscustomobject]@{
            value = $key.GetValue('Path', $null, [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
            kind = [string]$key.GetValueKind('Path')
        }
    } finally { if ($key) { $key.Dispose() } }
}

function Write-UserPath($Value, [string]$Kind) {
    if ($Kind -notin @('String', 'ExpandString')) { throw 'Unsupported PATH registry type.' }
    if ($PathFixture) {
        [IO.File]::WriteAllText($PathFixture, (@{ value = $Value; kind = $Kind } | ConvertTo-Json))
        return
    }
    $key = [Microsoft.Win32.Registry]::CurrentUser.CreateSubKey('Environment')
    try {
        if ($null -eq $Value) { $key.DeleteValue('Path', $false) }
        else { $key.SetValue('Path', $Value, [Microsoft.Win32.RegistryValueKind]$Kind) }
    } finally { $key.Dispose() }
    if (-not ('OpenCodeProxy.EnvironmentBroadcast' -as [type])) {
        Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
namespace OpenCodeProxy {
    public static class EnvironmentBroadcast {
        [DllImport("user32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        public static extern IntPtr SendMessageTimeout(IntPtr hwnd, uint msg, UIntPtr wparam,
            string lparam, uint flags, uint timeout, out UIntPtr result);
    }
}
'@
    }
    $result = [UIntPtr]::Zero
    [void][OpenCodeProxy.EnvironmentBroadcast]::SendMessageTimeout([IntPtr]65535, 0x1A,
        [UIntPtr]::Zero, 'Environment', 2, 3000, [ref]$result)
}

function Write-PrivateFile([string]$Path, [string]$Text) {
    if (Test-Path -LiteralPath $Path) {
        if ([IO.File]::ReadAllText($Path) -ceq $Text) { return }
        throw 'Private installation file already exists with different contents.'
    }
    [IO.File]::WriteAllText($Path, $Text, [Text.UTF8Encoding]::new($false))
    $acl = [Security.AccessControl.FileSecurity]::new()
    $user = [Security.Principal.WindowsIdentity]::GetCurrent().User
    $acl.SetOwner($user)
    $acl.SetAccessRuleProtection($true, $false)
    foreach ($principal in @($user, [Security.Principal.SecurityIdentifier]::new('S-1-5-18'))) {
        $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($principal, 'FullControl', 'Allow'))
    }
    Set-Acl -LiteralPath $Path -AclObject $acl
}

$manifest = $null
if (Test-Path -LiteralPath $manifestPath -PathType Leaf) {
    $manifest = [IO.File]::ReadAllText($manifestPath) | ConvertFrom-Json
    if ($manifest.bin -ne $bin -or $manifest.runtime_path -ne $runtimePath -or $manifest.version -ne 1) {
        throw 'Different installation manifest; no changes made.'
    }
}
$current = Read-UserPath
if ($current.kind -notin @('String', 'ExpandString')) { throw 'Unsupported PATH registry type.' }
# Compare expanded entries without rewriting other entries or expanding the stored PATH.
function Is-CommandPath([string]$Entry) {
    return [Environment]::ExpandEnvironmentVariables($Entry.Trim().Trim('"')).TrimEnd('\') -ieq $bin
}
$parts = if ($null -eq $current.value) { @() } else { @($current.value -split ';') }

if ($Action -eq 'Remove') {
    if ($null -eq $manifest) {
        if (Test-Path -LiteralPath $wrapperPath) { throw 'No ownership manifest; wrapper not removed.' }
        Write-Output 'Command wrapper already absent.'
        return
    }
    if ((Test-Path -LiteralPath $wrapperPath) -and
        (Get-FileHash -LiteralPath $wrapperPath -Algorithm SHA256).Hash -ne $manifest.wrapper_hash) {
        throw 'Installed executable has changed; no removal performed.'
    }
    if ((Test-Path -LiteralPath $runtimePath) -and [IO.File]::ReadAllText($runtimePath) -cne $manifest.runtime_text) {
        throw 'Installed config has changed; no removal performed.'
    }
    $kept = @($parts | Where-Object { -not (Is-CommandPath $_) })
    $next = $kept -join ';'
    if ($kept.Count -eq 0 -and $null -eq $manifest.original_path.value) { $next = $null }
    if ($next -cne $current.value) { Write-UserPath $next $current.kind }
    foreach ($file in @($wrapperPath, $runtimePath)) {
        if (Test-Path -LiteralPath $file) { Remove-Item -LiteralPath $file }
    }
    # Keep the backup, including the exact original PATH, for inspection/recovery.
    $archived = Join-Path $state ('command-install-before-' + [Guid]::NewGuid().ToString('N') + '.json')
    Move-Item -LiteralPath $manifestPath -Destination $archived
    Write-Output 'Command wrapper removed. Other PATH entries and running processes were preserved.'
    return
}

if (-not (Test-Path -LiteralPath $settingsPath -PathType Leaf)) { throw 'Configure the proxy first.' }
if (-not $PythonExecutable) {
    $PythonExecutable = (Get-Command python.exe -CommandType Application | Select-Object -First 1).Source
}
$PythonExecutable = (Resolve-Path -LiteralPath $PythonExecutable).Path
if (-not $NativeExecutable) {
    foreach ($command in @(Get-Command opencode -CommandType Application -All)) {
        $candidate = Join-Path (Split-Path $command.Source) 'opencode-cli.exe'
        if (Test-Path -LiteralPath $candidate -PathType Leaf) { $NativeExecutable = $candidate; break }
    }
}
if (-not $NativeExecutable -and $manifest) {
    $NativeExecutable = ($manifest.runtime_text | ConvertFrom-Json).native_cli
}
if (-not $NativeExecutable) { throw 'Native OpenCode executable not found; provide NativeExecutable.' }
$NativeExecutable = (Resolve-Path -LiteralPath $NativeExecutable).Path
if ([IO.Path]::GetExtension($NativeExecutable) -ne '.exe') { throw 'Native exe required.' }
if ($NativeExecutable -ieq $wrapperPath) { throw 'Native executable cannot be the proxy wrapper.' }
$source = [IO.File]::ReadAllText((Join-Path $PSScriptRoot 'command-shim.cs'))
$source = $source.Replace('@@PYTHON@@', $PythonExecutable.Replace('"', '""')).Replace(
    '@@LAUNCHER@@', $launcher.Replace('"', '""')).Replace('@@CONFIG@@', $runtimePath.Replace('"', '""'))
$runtime = [ordered]@{ native_cli = $NativeExecutable; settings_file = $settingsPath } | ConvertTo-Json
if ($manifest) {
    if ($manifest.wrapper_source -cne $source -or $manifest.runtime_text -cne $runtime) {
        throw 'Installation parameters changed. Remove the previous installation explicitly first.'
    }
}
if ((Test-Path -LiteralPath $wrapperPath) -and
    ($null -eq $manifest -or (Get-FileHash -LiteralPath $wrapperPath -Algorithm SHA256).Hash -ne $manifest.wrapper_hash)) {
    throw 'Existing unowned/modified executable; no changes made.'
}
if ((Test-Path -LiteralPath $runtimePath) -and
    ($null -eq $manifest -or [IO.File]::ReadAllText($runtimePath) -cne $runtime)) {
    throw 'Existing unowned/modified config; no changes made.'
}
if (-not $manifest) {
    if (@($parts | Where-Object { Is-CommandPath $_ }).Count -gt 0) {
        throw 'Command directory already in PATH without our manifest; no changes made.'
    }
}
if (-not (Test-Path -LiteralPath $bin)) { New-Item -ItemType Directory -Path $bin | Out-Null }
if (-not (Test-Path -LiteralPath $wrapperPath)) {
    if ($manifest) { throw 'Installed exe missing. Remove installation first; backup was preserved.' }
    # Compilation happens before PATH changes, using the installed .NET compiler.
    Add-Type -TypeDefinition $source -Language CSharp -OutputAssembly $wrapperPath -OutputType ConsoleApplication
}
if (-not $manifest) {
    $manifest = [ordered]@{ version = 1; bin = $bin; runtime_path = $runtimePath;
        original_path = $current; wrapper_source = $source; runtime_text = $runtime;
        wrapper_hash = (Get-FileHash -LiteralPath $wrapperPath -Algorithm SHA256).Hash }
    Write-PrivateFile $manifestPath ($manifest | ConvertTo-Json -Depth 5)
}
Write-PrivateFile $runtimePath $runtime
$rest = @($parts | Where-Object { -not (Is-CommandPath $_) })
$next = (@($bin) + $rest) -join ';'
if ($next -cne $current.value) { Write-UserPath $next $current.kind }
Write-Output 'Command wrapper installed at the front of user PATH. Open a new terminal. No service stopped or global proxy env changed.'
