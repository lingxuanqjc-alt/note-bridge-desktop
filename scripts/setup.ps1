param([string]$UvPath = 'uv', [string]$PythonPath = '')
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$projectRoot = Split-Path -Parent $PSScriptRoot
Push-Location -LiteralPath $projectRoot
try {
    $env:UV_CACHE_DIR = Join-Path $projectRoot '.cache\uv'
    $env:UV_PYTHON_INSTALL_DIR = Join-Path $projectRoot '.tools\python'
    if ($PythonPath) {
        $PythonPath = (Resolve-Path -LiteralPath $PythonPath).Path
        & $PythonPath -c 'import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 13) else 1)'
        if ($LASTEXITCODE -ne 0) { throw 'The supplied interpreter must be an installed CPython 3.13.' }
    } else {
        & $UvPath python install 3.13 --no-bin --no-registry
        if ($LASTEXITCODE -ne 0) { throw 'Python installation failed. An installed official CPython 3.13 can be supplied with -PythonPath.' }
        $PythonPath = '3.13'
    }
    & $UvPath sync --locked --python $PythonPath --no-python-downloads
    if ($LASTEXITCODE -ne 0) { throw 'Python dependency installation failed' }
    New-Item -ItemType Directory -Force -Path '.cache\npm' | Out-Null
    & npm.cmd ci --prefix ui --cache '.cache\npm' --no-fund --no-audit
    if ($LASTEXITCODE -ne 0) { throw 'Frontend dependency installation failed' }
    New-Item -ItemType Directory -Force -Path 'ui\node_modules\.vite-temp','ui\dist' | Out-Null
    & npm.cmd run build --prefix ui
    if ($LASTEXITCODE -ne 0) { throw 'Frontend build failed' }
} finally { Pop-Location }
