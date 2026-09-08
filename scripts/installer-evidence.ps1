param(
    [Parameter(Mandatory=$true)][string]$InstallDirectory,
    [string]$PreviousBlockedEvidence = '',
    [string]$EvidenceDirectory = '',
    [ValidatePattern('^\d+\.\d+\.\d+$')][string]$Version = '1.0.0'
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$projectRoot = Split-Path -Parent $PSScriptRoot
$testRoot = [IO.Path]::GetFullPath((Join-Path $projectRoot '.private'))
$installPath = [IO.Path]::GetFullPath($InstallDirectory)
if (-not $installPath.StartsWith($testRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) { throw 'Installer test must stay inside .private' }
if (Test-Path -LiteralPath $installPath) { throw 'Use a fresh directory; existing files will not be removed.' }
$installKey = 'HKCU:\Software\NoteBridge'
$uninstallKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\NoteBridge'
$previous = $null
$previousMarkerHash = ''
if ($PreviousBlockedEvidence) {
    $PreviousBlockedEvidence = [IO.Path]::GetFullPath($PreviousBlockedEvidence)
    if (-not $PreviousBlockedEvidence.StartsWith($testRoot + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Previous evidence must stay inside .private' }
    $previous = Get-Content -LiteralPath $PreviousBlockedEvidence -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($previous.kind -ne 'developer-host-windows-install' -or $previous.status -ne 'blocked' -or $previous.blocked_step -ne 'uninstall' -or $previous.test_installation_preserved -ne $true -or $previous.uninstaller_sha256 -notmatch '^[a-fA-F0-9]{64}$') { throw 'Previous evidence is not a preserved blocked installer test.' }
    $previousPath = [IO.Path]::GetFullPath($previous.install_directory)
    if (-not $previousPath.StartsWith($testRoot + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Previous installation must stay inside .private' }
    $previousUninstaller = Join-Path $previousPath 'Uninstall.exe'
    if ([IO.Path]::GetFullPath($previous.blocked_file) -ne $previousUninstaller -or -not (Test-Path -LiteralPath (Join-Path $previousPath '.note-bridge-install'))) { throw 'Previous installation identity does not match the evidence.' }
    if ((Get-FileHash -LiteralPath $previousUninstaller -Algorithm SHA256).Hash -ne $previous.uninstaller_sha256) { throw 'Previous blocked executable has changed.' }
    $previousMarker = Join-Path $previousPath 'UserExports\keep.txt'
    $previousMarkerHash = (Get-FileHash -LiteralPath $previousMarker -Algorithm SHA256).Hash
}
if ((Test-Path -LiteralPath $installKey) -or (Test-Path -LiteralPath $uninstallKey)) {
    if (-not $previous -or -not (Test-Path -LiteralPath $installKey) -or -not (Test-Path -LiteralPath $uninstallKey)) { throw 'Existing user installation found; automatic installer test stopped before changes.' }
    $registeredInstall = Get-ItemProperty -LiteralPath $installKey
    $registeredUninstall = Get-ItemProperty -LiteralPath $uninstallKey
    if ($registeredInstall.InstallDir -ne $previousPath -or $registeredUninstall.InstallLocation -ne $previousPath -or $registeredUninstall.UninstallString -ne ('"' + $previousUninstaller + '"')) { throw 'Existing registry is not the explicitly identified blocked test installation.' }
}
$desktop = [Environment]::GetFolderPath('Desktop')
if (-not $desktop) { throw 'Current user desktop is unavailable; run in the normal Windows user environment.' }
$shortcut = Join-Path $desktop ([string][char]0x7b14 + [char]0x8bb0 + [char]0x4e92 + [char]0x8fc1 + '.lnk')
$originalShortcut = if (Test-Path -LiteralPath $shortcut) { (Get-FileHash -LiteralPath $shortcut -Algorithm SHA256).Hash } else { '' }
if (-not $EvidenceDirectory) { $EvidenceDirectory = Join-Path $testRoot ('evidence\installer-' + [Guid]::NewGuid().ToString('N')) }
$EvidenceDirectory = [IO.Path]::GetFullPath($EvidenceDirectory)
if (-not $EvidenceDirectory.StartsWith($testRoot + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Installer evidence must stay inside .private' }
if (Test-Path -LiteralPath $EvidenceDirectory) { throw 'Use a fresh evidence directory; old records will not be overwritten.' }
[void](New-Item -ItemType Directory -Path $EvidenceDirectory)
$installer = Join-Path $projectRoot ("dist\NoteBridge-$Version-setup.exe")
$evidence = [ordered]@{kind='developer-host-windows-install';version=$Version;formal_acceptance=$false;status='running';checks=@();os=[Environment]::OSVersion.VersionString;time=(Get-Date).ToUniversalTime().ToString('o');installer_sha256=(Get-FileHash -LiteralPath $installer -Algorithm SHA256).Hash.ToLowerInvariant();install_directory=$installPath;policy_changed=$false;blocked_executable_retried=$false}
$allChecks = @('install-Chinese-path','frozen-resources','frozen-WebView2-bridge','same-version-upgrade','upgraded-WebView2-bridge','uninstall','user-export-preserved','registry-cleaned','desktop-shortcut-preserved')
function Save-Evidence {
    $script:evidence | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $EvidenceDirectory 'evidence.json') -Encoding UTF8
}
function Run-Check([string]$File, [string[]]$Arguments, [string]$Label) {
    try { $process = Start-Process -FilePath $File -ArgumentList $Arguments -PassThru -WindowStyle Hidden }
    catch {
        $script:evidence.status = 'failed'
        $script:evidence.failed_step = $Label
        $script:evidence.error_type = $_.Exception.GetType().Name
        $script:evidence.not_run = @($allChecks | Where-Object { $_ -notin $script:evidence.checks -and $_ -ne $Label })
        Save-Evidence
        throw
    }
    if (-not $process.WaitForExit(45000)) { throw "$Label timed out; process $($process.Id) remains for inspection." }
    if ($process.ExitCode -ne 0) { throw "$Label failed with exit code $($process.ExitCode)" }
    $script:evidence.checks += $Label
    Save-Evidence
}
Run-Check $installer @('/S','/NoShortcut',('/D=' + $installPath)) 'install-Chinese-path'
$app = Join-Path $installPath 'NoteBridge.exe'
if (-not (Test-Path -LiteralPath $app)) { throw 'Installer did not create the application' }
$env:NOTE_BRIDGE_DATA_DIR = Join-Path $EvidenceDirectory 'native-smoke-data'
Run-Check $app @('--check') 'frozen-resources'
Run-Check $app @('--smoke-test') 'frozen-WebView2-bridge'
$keep = Join-Path $installPath 'UserExports\keep.txt'
[void](New-Item -ItemType Directory -Path (Split-Path -Parent $keep))
[IO.File]::WriteAllText($keep, 'user export survives upgrade and uninstall')
Run-Check $installer @('/S','/NoShortcut',('/D=' + $installPath)) 'same-version-upgrade'
if ([IO.File]::ReadAllText($keep) -ne 'user export survives upgrade and uninstall') { throw 'Upgrade altered user export' }
Run-Check $app @('--smoke-test') 'upgraded-WebView2-bridge'
$uninstaller = Join-Path $installPath 'Uninstall.exe'
$evidence.uninstaller_sha256 = (Get-FileHash -LiteralPath $uninstaller -Algorithm SHA256).Hash.ToLowerInvariant()
if ($previous) {
    if ((Get-FileHash -LiteralPath $previousMarker -Algorithm SHA256).Hash -ne $previousMarkerHash -or (Get-FileHash -LiteralPath $previousUninstaller -Algorithm SHA256).Hash -ne $previous.uninstaller_sha256) { throw 'The preserved earlier test files changed.' }
    $evidence.previous_blocked_evidence = $PreviousBlockedEvidence
    $evidence.previous_installation_preserved = $true
}
if ($previous -and $evidence.uninstaller_sha256 -eq $previous.uninstaller_sha256) {
    $evidence.status = 'blocked'
    $evidence.blocked_step = 'uninstall'
    $evidence.blocked_file = $uninstaller
    $evidence.reason = 'Uninstaller SHA256 matches the previously proven Windows Application Control block; execution was skipped.'
    $evidence.not_run = @($allChecks | Where-Object { $_ -notin $evidence.checks })
    $evidence.export_marker_present = Test-Path -LiteralPath $keep
    $evidence.test_installation_preserved = $true
    Save-Evidence
    $evidence | ConvertTo-Json -Depth 5 -Compress
    exit 2
}
Save-Evidence
Run-Check $uninstaller @('/S',('_?=' + $installPath)) 'uninstall'
if ((Test-Path -LiteralPath $app) -or -not (Test-Path -LiteralPath $keep)) { throw 'Uninstall did not preserve the expected file boundary' }
if ([IO.File]::ReadAllText($keep) -ne 'user export survives upgrade and uninstall') { throw 'Uninstall altered user export' }
if ((Test-Path -LiteralPath $installKey) -or (Test-Path -LiteralPath $uninstallKey)) { throw 'Test installation registry remains' }
$finalShortcut = if (Test-Path -LiteralPath $shortcut) { (Get-FileHash -LiteralPath $shortcut -Algorithm SHA256).Hash } else { '' }
if ($finalShortcut -ne $originalShortcut) { throw 'NoShortcut install/uninstall altered the existing desktop shortcut' }
$evidence.checks += @('user-export-preserved','registry-cleaned','desktop-shortcut-preserved')
$evidence.status = 'passed'
Save-Evidence
$evidence | ConvertTo-Json -Depth 5 -Compress
