"""Exercise the launcher's API branch without stopping real processes."""

import json
from pathlib import Path
import shutil
import subprocess

import pytest


POWERSHELL = shutil.which("powershell.exe")
LAUNCHER = Path(__file__).resolve().parents[1] / "start-webapi.ps1"
pytestmark = pytest.mark.skipif(not POWERSHELL, reason="Windows PowerShell required")


def run_api_branch(scenario):
    script = r'''
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$script:events = [Collections.Generic.List[string]]::new()
$script:stopped = $false
$python = 'Invoke-TestPython'
$env:VIDEO_AGENT_API_HOST = '127.0.0.1'
$env:VIDEO_AGENT_API_PORT = '18000'
$env:VIDEO_AGENT_API_LOG_LEVEL = 'info'
function Get-NetTCPConnection {
    param($State, $ErrorAction)
    if ($scenario -ne 'empty' -and (-not $script:stopped -or $scenario -eq 'still-busy')) {
        [pscustomobject]@{ LocalPort = 18000; OwningProcess = 12345 }
    }
}
function Get-CimInstance {
    param($ClassName, $Filter, $ErrorAction)
    $command = 'Invoke-TestPython -m uvicorn api_app.app:create_app --factory --port 18000'
    if ($scenario -eq 'quoted') { $command = '"Invoke-TestPython" -m uvicorn api_app.app:create_app --factory --port 18000' }
    if ($scenario -eq 'unrelated') { $command = 'python -m http.server 18000' }
    if ($scenario -eq 'other-project') {
        $command = 'C:\other\python.exe -m uvicorn api_app.app:create_app --factory --port 18000'
    }
    [pscustomobject]@{ ExecutablePath = 'Invoke-TestPython'; CommandLine = $command }
}
function Stop-Process {
    param($Id, [switch]$Force, $ErrorAction)
    $script:events.Add('stop')
    if ($scenario -eq 'stop-failure') { throw 'Access denied' }
    $script:stopped = $true
}
function Wait-Process {
    param($Id, $Timeout, $ErrorAction)
    $script:events.Add('wait')
}
function Invoke-TestPython {
    $script:events.Add('start')
}
$tokens = $null
$errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile($launcher, [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw ($errors | Out-String) }
$switch = $ast.Find({ param($node) $node -is [Management.Automation.Language.SwitchStatementAst] }, $true)
$branch = ($switch.Clauses | Where-Object { $_.Item1.Value -eq 'api' }).Item2
try {
    $body = $branch.Extent.Text
    . ([scriptblock]::Create($body.Substring(1, $body.Length - 2)))
    $failure = $null
} catch {
    $failure = $_.Exception.Message
}
[pscustomobject]@{ Events = @($script:events.ToArray()); Failure = $failure } | ConvertTo-Json -Compress
'''
    prefix = f"$launcher = '{str(LAUNCHER).replace(chr(39), chr(39) * 2)}'\n$scenario = '{scenario}'\n"
    result = subprocess.run(
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", prefix + script],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    return json.loads(result.stdout.splitlines()[-1])


@pytest.mark.parametrize(
    ("scenario", "events", "failed"),
    [
        ("empty", ["start"], False),
        ("existing", ["stop", "wait", "start"], False),
        ("quoted", ["stop", "wait", "start"], False),
        ("unrelated", [], True),
        ("other-project", [], True),
        ("stop-failure", ["stop"], True),
        ("still-busy", ["stop", "wait"], True),
    ],
)
def test_api_restart(scenario, events, failed):
    result = run_api_branch(scenario)
    assert result["Events"] == events
    assert bool(result["Failure"]) is failed
