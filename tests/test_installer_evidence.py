"""UTF-8 evidence must retain its Chinese scope before any native process runs."""
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows PowerShell installer guard')
@pytest.mark.parametrize('version', [None, '1.2.3'])
def test_bomless_utf8_chinese_evidence_preserves_scope_and_skips_known_uninstaller(tmp_path, version):
    root = tmp_path / '中文项目'
    scripts = root / 'scripts'
    scripts.mkdir(parents=True)
    script = scripts / 'installer-evidence.ps1'
    script.write_bytes((Path(__file__).resolve().parents[1] / 'scripts/installer-evidence.ps1').read_bytes())
    old = root / '.private/旧安装目录'
    (old / 'UserExports').mkdir(parents=True)
    (old / '.note-bridge-install').touch()
    marker = old / 'UserExports/keep.txt'
    marker.write_bytes(b'preserved export')
    blocked_bytes = b'synthetic known blocked bytes, not an executable'
    uninstaller = old / 'Uninstall.exe'
    uninstaller.write_bytes(blocked_bytes)
    previous = root / '.private/previous.json'
    previous.write_text(json.dumps({
        'kind': 'developer-host-windows-install', 'status': 'blocked', 'blocked_step': 'uninstall',
        'test_installation_preserved': True, 'install_directory': str(old), 'blocked_file': str(uninstaller),
        'uninstaller_sha256': hashlib.sha256(blocked_bytes).hexdigest(),
    }, ensure_ascii=False), encoding='utf-8')
    assert not previous.read_bytes().startswith(b'\xef\xbb\xbf')
    assert '旧安装目录'.encode('utf-8') in previous.read_bytes()
    (root / 'dist').mkdir()
    installer_name = f'NoteBridge-{version or "1.0.0"}-setup.exe'
    (root / 'dist' / installer_name).write_bytes(b'synthetic installer, not an executable')
    new, evidence, log = root / '.private/新安装目录', root / '.private/evidence/new', tmp_path / 'calls.json'
    harness = tmp_path / 'simulate.ps1'
    source = r'''
$ErrorActionPreference = 'Stop'
if ($PSVersionTable.PSVersion.Major -ne 5 -or $PSVersionTable.PSVersion.Minor -ne 1) { throw 'WinPS 5.1 required' }
Get-Command Get-FileHash | Out-Null
[Console]::OutputEncoding = [Text.UTF8Encoding]::new()
$global:installerGuardCalls = [System.Collections.Generic.List[string]]::new()
function Test-Path {
    [CmdletBinding()]
    param([Parameter(Position=0)][string]$Path,[string]$LiteralPath,[string]$PathType)
    $checkedPath = if ($LiteralPath) { $LiteralPath } else { $Path }
    if ($checkedPath -like 'HKCU:*') { return $true }
    return Microsoft.PowerShell.Management\Test-Path @PSBoundParameters
}
function Get-ItemProperty {
    param([string]$LiteralPath)
    if ($LiteralPath -eq 'HKCU:\Software\NoteBridge') { return [pscustomobject]@{InstallDir={{OLD}}} }
    if ($LiteralPath -eq 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\NoteBridge') {
        return [pscustomobject]@{InstallLocation={{OLD}};UninstallString=('"'+{{UNINSTALLER}}+'"')}
    }
    throw 'Unexpected registry query'
}
function Start-Process {
    param([string]$FilePath,[string[]]$ArgumentList,[switch]$PassThru,[string]$WindowStyle)
    $name = [IO.Path]::GetFileName($FilePath)
    $global:installerGuardCalls.Add($name)
    if ($name -eq 'Uninstall.exe') { throw 'Known blocked uninstaller must never be invoked' }
    if ($name -eq {{INSTALLER_NAME}}) {
        [void][IO.Directory]::CreateDirectory({{NEW}})
        [IO.File]::WriteAllText((Join-Path {{NEW}} 'NoteBridge.exe'),'synthetic app')
        [IO.File]::WriteAllBytes((Join-Path {{NEW}} 'Uninstall.exe'),[IO.File]::ReadAllBytes({{UNINSTALLER}}))
    }
    $process = [pscustomobject]@{Id=12345;ExitCode=0}
    $process | Add-Member -MemberType ScriptMethod -Name WaitForExit -Value {param($Milliseconds) return $true}
    return $process
}
try {
    & {{SCRIPT}} -InstallDirectory {{NEW}} -PreviousBlockedEvidence {{PREVIOUS}} -EvidenceDirectory {{EVIDENCE}} {{VERSION_ARGUMENT}}
    $outcome = $LASTEXITCODE
} catch { $outcome = 9; Write-Output $_.Exception.Message }
@{exit_code=$outcome;calls=@($global:installerGuardCalls.ToArray());powershell_version=$PSVersionTable.PSVersion.ToString()} |
    ConvertTo-Json | Set-Content -LiteralPath {{LOG}} -Encoding UTF8
'''
    source = source.replace('{{VERSION_ARGUMENT}}', '' if version is None else f"-Version '{version}'")
    for key, value in {'OLD': old, 'NEW': new, 'UNINSTALLER': uninstaller, 'SCRIPT': script,
                       'INSTALLER_NAME': installer_name,
                       'PREVIOUS': previous, 'EVIDENCE': evidence, 'LOG': log}.items():
        source = source.replace('{{' + key + '}}', "'" + str(value).replace("'", "''") + "'")
    harness.write_text(source, encoding='utf-8-sig')
    winps = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0'
    result = subprocess.run([str(winps / 'powershell.exe'), '-NoProfile', '-NonInteractive',
                             '-ExecutionPolicy', 'Bypass', '-File', str(harness)],
                            capture_output=True, text=True, encoding='utf-8', timeout=20,
                            env={**os.environ, 'PSModulePath': str(winps / 'Modules')},
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    assert result.returncode == 0, result.stderr
    calls = json.loads(log.read_text('utf-8-sig'))
    assert calls['powershell_version'].startswith('5.1.')
    assert calls['exit_code'] == 2, result.stdout
    assert len(calls['calls']) == 5 and 'Uninstall.exe' not in calls['calls']
    proof = json.loads((evidence / 'evidence.json').read_text('utf-8-sig'))
    assert proof['status'] == 'blocked' and proof['install_directory'] == str(new)
    assert proof['version'] == (version or '1.0.0')
    assert len(proof['checks']) == 5 and len(proof['not_run']) == 4
    assert proof['previous_installation_preserved'] is True and proof['blocked_executable_retried'] is False
    assert uninstaller.read_bytes() == blocked_bytes and marker.read_bytes() == b'preserved export'
