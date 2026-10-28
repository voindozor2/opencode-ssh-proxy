[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('Configure', 'Start', 'Stop', 'Status', 'InstallStartup', 'RemoveStartup')]
    [string]$Action,
    [string]$StateDirectory = (Join-Path $HOME '.config\opencode-ssh-proxy'),
    [string]$PythonExecutable,
    [string]$SshExecutable,
    [string]$VpsHost,
    [string]$SshUser,
    [string]$IdentityFile,
    [string]$PasswordFile,
    [int]$ListenPort = 18080,
    [int]$SocksPort = 18081,
    [string]$StartupDirectory = ([Environment]::GetFolderPath('Startup'))
)

$ErrorActionPreference = 'Stop'
$repo = [IO.Path]::GetFullPath($PSScriptRoot)
$state = [IO.Path]::GetFullPath($StateDirectory)
$configPath = Join-Path $state 'settings.local.json'
$supervisor = Join-Path $repo 'supervisor.py'

if ($state -eq $repo -or $state.StartsWith($repo + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'State directory must be outside the source checkout.'
}

if (-not $PythonExecutable) {
    $PythonExecutable = (Get-Command python.exe -CommandType Application -ErrorAction Stop).Source
}
$PythonExecutable = (Resolve-Path -LiteralPath $PythonExecutable).Path
if ($PythonExecutable.Contains('"') -or $configPath.Contains('"') -or $supervisor.Contains('"')) {
    throw 'Unsupported quote in path.'
}

function Set-PrivateAcl([string]$Path, [bool]$Directory) {
    $user = [Security.Principal.WindowsIdentity]::GetCurrent().User
    $system = [Security.Principal.SecurityIdentifier]::new('S-1-5-18')
    if ($Directory) {
        $acl = [Security.AccessControl.DirectorySecurity]::new()
        $inherit = [Security.AccessControl.InheritanceFlags]'ContainerInherit, ObjectInherit'
    } else {
        $acl = [Security.AccessControl.FileSecurity]::new()
        $inherit = [Security.AccessControl.InheritanceFlags]::None
    }
    $acl.SetOwner($user)
    $acl.SetAccessRuleProtection($true, $false)
    foreach ($principal in @($user, $system)) {
        $rule = [Security.AccessControl.FileSystemAccessRule]::new(
            $principal, [Security.AccessControl.FileSystemRights]::FullControl,
            $inherit, [Security.AccessControl.PropagationFlags]::None,
            [Security.AccessControl.AccessControlType]::Allow)
        $acl.AddAccessRule($rule)
    }
    Set-Acl -LiteralPath $Path -AclObject $acl
}

if ($Action -eq 'Configure') {
    if (Test-Path -LiteralPath $configPath) {
        throw 'Configuration already exists; it was not overwritten.'
    }
    if (-not $VpsHost -or -not $SshUser -or -not $IdentityFile) {
        throw 'Configure requires VpsHost, SshUser and IdentityFile.'
    }
    if (-not $SshExecutable) {
        $SshExecutable = (Get-Command ssh.exe -CommandType Application -ErrorAction Stop).Source
    }
    $SshExecutable = (Resolve-Path -LiteralPath $SshExecutable).Path
    $IdentityFile = (Resolve-Path -LiteralPath $IdentityFile).Path
    if (Test-Path -LiteralPath $state) {
        if (@(Get-ChildItem -LiteralPath $state -Force).Count -gt 0) {
            throw 'Use a new or empty dedicated state directory; existing contents were not changed.'
        }
    } else {
        New-Item -ItemType Directory -Path $state | Out-Null
    }
    Set-PrivateAcl $state $true
    if ($PasswordFile) {
        $PasswordFile = (Resolve-Path -LiteralPath $PasswordFile).Path
        # Existing credential files are reused without changing their contents or ACL.
    } else {
        $PasswordFile = Join-Path $state 'proxy-password'
        $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
        try {
            $bytes = New-Object byte[] 32
            $rng.GetBytes($bytes)
        } finally {
            $rng.Dispose()
        }
        [IO.File]::WriteAllText($PasswordFile, [Convert]::ToBase64String($bytes), [Text.UTF8Encoding]::new($false))
        Set-PrivateAcl $PasswordFile $false
        Remove-Variable bytes
    }
    $settings = [ordered]@{
        ssh_executable = $SshExecutable
        ssh_host = $VpsHost
        ssh_user = $SshUser
        identity_file = $IdentityFile
        password_file = $PasswordFile
        listen_port = $ListenPort
        socks_port = $SocksPort
    }
    [IO.File]::WriteAllText($configPath, ($settings | ConvertTo-Json), [Text.UTF8Encoding]::new($false))
    Set-PrivateAcl $configPath $false
    & $PythonExecutable $supervisor --config $configPath --check
    if ($LASTEXITCODE -ne 0) { throw 'Configuration validation failed; no process or startup entry was created.' }
    Write-Output 'Configured. No process started and no startup entry installed.'
    return
}

if (-not (Test-Path -LiteralPath $configPath -PathType Leaf)) {
    throw 'Configuration not found. Run Configure first.'
}

switch ($Action) {
    'Status' {
        & $PythonExecutable $supervisor --config $configPath --status
        if ($LASTEXITCODE -gt 1) { throw 'Status failed.' }
    }
    'Stop' {
        & $PythonExecutable $supervisor --config $configPath --stop
        if ($LASTEXITCODE -ne 0) { throw 'Stop failed; no unrelated process was terminated.' }
    }
    'Start' {
        & $PythonExecutable $supervisor --config $configPath --status *> $null
        $statusCode = $LASTEXITCODE
        if ($statusCode -eq 0) { Write-Output 'Already running.'; return }
        if ($statusCode -ne 1) { throw 'Cannot determine supervisor status.' }
        $pythonw = Join-Path (Split-Path $PythonExecutable) 'pythonw.exe'
        if (-not (Test-Path -LiteralPath $pythonw -PathType Leaf)) { throw 'pythonw.exe not found.' }
        Start-Process -FilePath $pythonw -ArgumentList @("`"$supervisor`"", '--config', "`"$configPath`"") -WorkingDirectory $repo
        Start-Sleep -Seconds 2
        & $PythonExecutable $supervisor --config $configPath --status
        if ($LASTEXITCODE -ne 0) { throw 'Supervisor did not stay running. Check ports/configuration or launch supervisor.py in a console.' }
    }
    { $_ -in 'InstallStartup', 'RemoveStartup' } {
        $pythonw = Join-Path (Split-Path $PythonExecutable) 'pythonw.exe'
        $arguments = "`"$supervisor`" --config `"$configPath`""
        $startup = [IO.Path]::GetFullPath($StartupDirectory)
        $realStartup = [IO.Path]::GetFullPath([Environment]::GetFolderPath('Startup'))
        if ($Action -eq 'InstallStartup' -and $startup -eq $realStartup) {
            $temp = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\') + '\'
            if ($repo.StartsWith($temp, [StringComparison]::OrdinalIgnoreCase)) {
                throw 'Move/clone the repository to a permanent directory before installing startup.'
            }
        }
        if (-not (Test-Path -LiteralPath $startup -PathType Container)) {
            throw 'Startup directory does not exist.'
        }
        $shortcutPath = Join-Path $startup 'OpenCodeSSHProxy.lnk'
        $shell = New-Object -ComObject WScript.Shell
        if (Test-Path -LiteralPath $shortcutPath) {
            $existing = $shell.CreateShortcut($shortcutPath)
            if ($existing.TargetPath -ne $pythonw -or $existing.Arguments -ne $arguments -or $existing.WorkingDirectory -ne $repo) {
                throw 'An unrelated/different startup shortcut exists; it was not changed.'
            }
            if ($Action -eq 'RemoveStartup') {
                Remove-Item -LiteralPath $shortcutPath
                Write-Output 'Startup entry removed. Running processes were not stopped.'
            } else {
                Write-Output 'Matching startup entry already installed.'
            }
        } elseif ($Action -eq 'RemoveStartup') {
            Write-Output 'Startup entry already absent.'
        } else {
            if (-not (Test-Path -LiteralPath $pythonw -PathType Leaf)) { throw 'pythonw.exe not found.' }
            $shortcut = $shell.CreateShortcut($shortcutPath)
            $shortcut.TargetPath = $pythonw
            $shortcut.Arguments = $arguments
            $shortcut.WorkingDirectory = $repo
            $shortcut.Description = 'OpenCode SSH HTTP proxy supervisor'
            $shortcut.Save()
            Write-Output 'Startup entry installed for the current user. No process started.'
        }
    }
}
