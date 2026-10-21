param(
    [string]$EnvFile = (Join-Path $PSScriptRoot '.env')
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$exitCode = 1

try {
    if (-not (Test-Path -LiteralPath $EnvFile -PathType Leaf)) {
        throw 'Configuration file not found. Copy .env.example to .env and fill in the values.'
    }
    # Read configuration as literal text, without executing or expanding values.
    $lineNumber = 0
    foreach ($line in Get-Content -LiteralPath $EnvFile -Encoding UTF8) {
        $lineNumber++
        $text = $line.Trim()
        if ($text.Length -eq 0 -or $text.StartsWith('#')) { continue }
        if ($text -notmatch '^(VIDEO_AGENT_[A-Z0-9_]+)\s*=(.*)$') {
            throw "Invalid configuration at line $lineNumber. Expected VIDEO_AGENT_NAME=value."
        }
        $name = $matches[1]
        $value = $matches[2].Trim()
        if ($value.StartsWith('"') -or $value.StartsWith("'")) {
            if ($value.Length -lt 2 -or $value[$value.Length - 1] -ne $value[0]) {
                throw "Unclosed quote in configuration at line $lineNumber."
            }
            $value = $value.Substring(1, $value.Length - 2)
        }
        [Environment]::SetEnvironmentVariable($name, $value, 'Process')
    }

    $python = Join-Path $PSScriptRoot '.venv/Scripts/python.exe'
    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
        throw 'Python environment not found. Run: python -m venv .venv; .venv/Scripts/python.exe -m pip install -e .'
    }
    if ([string]::IsNullOrWhiteSpace($env:VIDEO_AGENT_HOME)) {
        $env:VIDEO_AGENT_HOME = Join-Path $PSScriptRoot '.video-agent'
    } elseif (-not [IO.Path]::IsPathRooted($env:VIDEO_AGENT_HOME)) {
        $env:VIDEO_AGENT_HOME = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot $env:VIDEO_AGENT_HOME))
    }

    Push-Location -LiteralPath $PSScriptRoot
    try {
        Write-Host 'Starting Video Agent desktop test tool. Close its window to exit.'
        & $python -m desktop_app
        $exitCode = $LASTEXITCODE
    } finally {
        Pop-Location
    }
} catch {
    [Console]::Error.WriteLine($_.Exception.Message)
}
exit $exitCode
