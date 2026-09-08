param([string]$NsisPath = '')
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$projectRoot = Split-Path -Parent $PSScriptRoot
Push-Location -LiteralPath $projectRoot
try {
    $projectPython = Join-Path $projectRoot '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $projectPython)) { throw 'Run scripts/setup.ps1 first.' }
    # A fresh test directory preserves failed runs and avoids deleting another run's files.
    $buildTestRelative = '.private\build-test-temp-' + [Guid]::NewGuid().ToString('N')
    # pytest and PyInstaller replace their own output directories recursively.
    foreach ($relativeTarget in @('dist', 'dist\NoteBridge', 'build', '.private', $buildTestRelative)) {
        $targetPath = [IO.Path]::GetFullPath((Join-Path $projectRoot $relativeTarget))
        if (-not $targetPath.StartsWith($projectRoot + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Build output escaped the project.' }
        if (Test-Path -LiteralPath $targetPath) {
            $targetItem = Get-Item -LiteralPath $targetPath
            if (($targetItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'Build output must not be a reparse point.' }
        }
    }
    & npm.cmd run build --prefix ui
    if ($LASTEXITCODE -ne 0) { throw 'Frontend build failed' }
    & $projectPython -m pytest -q -p no:cacheprovider --basetemp $buildTestRelative
    if ($LASTEXITCODE -ne 0) { throw 'Tests failed; packaging stopped' }
    & $projectPython 'scripts/make-icon.py'
    if ($LASTEXITCODE -ne 0) { throw 'Icon generation failed' }
    & $projectPython 'scripts/collect-licenses.py'
    if ($LASTEXITCODE -ne 0) { throw 'License collection failed' }
    & $projectPython -m PyInstaller --noconfirm 'packaging/note-bridge.spec'
    if ($LASTEXITCODE -ne 0) { throw 'Desktop build failed' }
    & $projectPython 'scripts/package-artifacts.py'
    if ($LASTEXITCODE -ne 0) { throw 'Artifact packaging failed' }
    if (-not $NsisPath) {
        $bundledCompiler = Join-Path $projectRoot '.tools\nsis-3.12\makensis.exe'
        if (Test-Path -LiteralPath $bundledCompiler) { $NsisPath = $bundledCompiler }
        elseif (Get-Command makensis -ErrorAction SilentlyContinue) { $NsisPath = (Get-Command makensis).Source }
    }
    if (-not $NsisPath) { throw 'Portable ZIP built. NSIS 3.12 is required for the installer; pass -NsisPath.' }
    $version = & $projectPython -c 'from note_bridge import __version__; print(__version__)'
    & $NsisPath '/V3' "/DVERSION=$version" "/DSTAGE=$projectRoot\dist\NoteBridge" "/DOUTPUT=$projectRoot\dist\NoteBridge-$version-setup.exe" "/DDELETE_MANIFEST=$projectRoot\build\uninstall-files.nsh" 'packaging\installer.nsi'
    if ($LASTEXITCODE -ne 0) { throw 'Installer build failed' }
    & $projectPython 'scripts/package-artifacts.py'
    if ($LASTEXITCODE -ne 0) { throw 'Final checksum generation failed' }
    Write-Output "Artifacts for version $version are in dist. Release channel and acceptance status are recorded separately in build-manifest.json."
} finally { Pop-Location }
