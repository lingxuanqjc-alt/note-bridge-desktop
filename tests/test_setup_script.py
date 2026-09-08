"""Bootstrap choices must preserve locks and fail before dependency mutations."""

import json
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.skipif(sys.platform != "win32", reason="Windows bootstrap script")
@pytest.mark.parametrize("mode", ["managed", "provided", "wrong_version"])
def test_setup_interpreter_choice_preserves_locked_sync_and_rejects_wrong_version(tmp_path, mode):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    setup = scripts / "setup.ps1"
    setup.write_bytes((Path(__file__).resolve().parents[1] / "scripts/setup.ps1").read_bytes())
    log = tmp_path / "calls.json"
    interpreter = Path(getattr(sys, "_base_executable", sys.executable))
    if mode == "wrong_version":
        interpreter = tmp_path / "wrong-version.ps1"
        interpreter.write_text("param([string]$c)\n$global:LASTEXITCODE = 1\n", encoding="utf-8-sig")
    def quote(value):
        return "'" + str(value).replace("'", "''") + "'"
    harness = tmp_path / "test-setup.ps1"
    harness.write_text("""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new()
$global:setupAuditCalls = [System.Collections.Generic.List[object]]::new()
function uv_test {
    $global:setupAuditCalls.Add(@{command='uv';arguments=[string[]]@($args)})
    $global:LASTEXITCODE = 0
}
function npm.cmd {
    $global:setupAuditCalls.Add(@{command='npm';arguments=[string[]]@($args)})
    $global:LASTEXITCODE = 0
}
try {
    & SETUP -UvPath uv_test PYTHON
    $outcome = 'succeeded'
} catch { $outcome = 'failed'; Write-Output $_.Exception.Message }
@{outcome=$outcome;calls=@($global:setupAuditCalls.ToArray())} | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath LOG -Encoding UTF8
""".replace("SETUP", quote(setup)).replace("PYTHON", "" if mode == "managed" else "-PythonPath " + quote(interpreter))
        .replace("LOG", quote(log)), encoding="utf-8-sig")
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(harness)],
                            capture_output=True, text=True, encoding="utf-8", timeout=20,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert result.returncode == 0
    proof = json.loads(log.read_text("utf-8-sig"))
    if mode == "wrong_version":
        assert proof == {"outcome": "failed", "calls": []}
        return
    assert proof["outcome"] == "succeeded", result.stdout
    uv = [call["arguments"] for call in proof["calls"] if call["command"] == "uv"]
    expected = "3.13" if mode == "managed" else str(interpreter.resolve())
    assert uv[-1] == ["sync", "--locked", "--python", expected, "--no-python-downloads"]
    assert len(uv) == (2 if mode == "managed" else 1)
    if mode == "managed":
        assert uv[0] == ["python", "install", "3.13", "--no-bin", "--no-registry"]
    assert [call["arguments"][:2] for call in proof["calls"] if call["command"] == "npm"] == [["ci", "--prefix"], ["run", "build"]]
