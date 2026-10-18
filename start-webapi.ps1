param(
    [ValidateSet('api', 'worker', 'migrate')]
    [string]$Mode = 'api',
    [string]$EnvFile = (Join-Path $PSScriptRoot '.env')
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$exitCode = 1

try {
    if (-not (Test-Path -LiteralPath $EnvFile -PathType Leaf)) {
        throw 'Configuration file not found. Copy .env.example to .env and fill in the values.'
    }
    # Read values as literal text. Never execute or expand configuration content.
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
        switch ($Mode) {
            'migrate' {
                & $python -m alembic upgrade head
            }
            'worker' {
                & $python -m video_agent.worker_main
            }
            'api' {
                $bindHost = $env:VIDEO_AGENT_API_HOST
                if ([string]::IsNullOrWhiteSpace($bindHost)) { $bindHost = '127.0.0.1' }
                $port = 8000
                if (-not [string]::IsNullOrWhiteSpace($env:VIDEO_AGENT_API_PORT)) {
                    if (-not [int]::TryParse($env:VIDEO_AGENT_API_PORT, [ref]$port) -or $port -lt 1 -or $port -gt 65535) {
                        throw 'VIDEO_AGENT_API_PORT must be an integer between 1 and 65535.'
                    }
                }
                $logLevel = $env:VIDEO_AGENT_API_LOG_LEVEL
                if ([string]::IsNullOrWhiteSpace($logLevel)) { $logLevel = 'info' }
                if ($logLevel -notin @('critical', 'error', 'warning', 'info', 'debug', 'trace')) {
                    throw 'VIDEO_AGENT_API_LOG_LEVEL must be critical, error, warning, info, debug or trace.'
                }
                Write-Host "Starting WebAPI on ${bindHost}:$port. Press Ctrl+C to stop."
                & $python -m uvicorn api_app.app:create_app --factory --host $bindHost --port $port --log-level $logLevel
            }
        }
        $exitCode = $LASTEXITCODE
    } finally {
        Pop-Location
    }
} catch {
    [Console]::Error.WriteLine($_.Exception.Message)
}
exit $exitCode
