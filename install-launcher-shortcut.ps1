[CmdletBinding()]
param(
    [string]$ProjectDirectory = $HOME,
    [Parameter(Mandatory = $true)][string]$ShortcutDirectory,
    [string]$CommandDirectory = (Join-Path $HOME '.local\opencode-proxy\bin')
)
$ErrorActionPreference = 'Stop'
$project = (Resolve-Path -LiteralPath $ProjectDirectory).Path
$directory = (Resolve-Path -LiteralPath $ShortcutDirectory).Path
if (-not (Test-Path -LiteralPath $project -PathType Container) -or
    -not (Test-Path -LiteralPath $directory -PathType Container)) { throw 'Directories required.' }
$launcher = Join-Path ([IO.Path]::GetFullPath($CommandDirectory)) 'opencode.exe'
if (-not (Test-Path -LiteralPath $launcher -PathType Leaf)) { throw 'Install the command wrapper first.' }
foreach ($path in @($launcher, $project)) {
    if ($path.Contains('"')) { throw 'Unsupported quote in path.' }
}
$target = $launcher
$arguments = ''
$path = Join-Path $directory 'OpenCode via SSH.lnk'
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($path)
if (Test-Path -LiteralPath $path) {
    if ($shortcut.TargetPath -ne $target -or $shortcut.Arguments -ne $arguments -or $shortcut.WorkingDirectory -ne $project) {
        throw 'An unrelated/different shortcut exists; it was not changed.'
    }
    Write-Output 'Matching launcher shortcut already exists.'
    return
}
$shortcut.TargetPath = $target
$shortcut.Arguments = $arguments
$shortcut.WorkingDirectory = $project
$shortcut.Description = 'OpenCode terminal through local SSH HTTP proxy (choose any project)'
$shortcut.Save()
Write-Output 'Launcher shortcut created. No process started and no startup entry changed.'
