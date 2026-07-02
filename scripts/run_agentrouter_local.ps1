param(
    [switch]$NoNotify
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$OutputEncoding = [Console]::OutputEncoding
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Resolve-Path -LiteralPath (Join-Path $scriptDir "..")
$envPath = Join-Path $repoRoot ".env"
$logDir = Join-Path $repoRoot "logs"
$logPath = Join-Path $logDir ("agentrouter-local-{0}.log" -f (Get-Date -Format "yyyyMMdd"))

function Write-RunLog {
    param([string]$Message)
    $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    Write-Output $line
    Add-Content -LiteralPath $logPath -Encoding UTF8 -Value $line
}

function Get-DotEnvValue {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Name
    )

    if (-not (Test-Path -LiteralPath $Path)) {
        return $null
    }

    foreach ($line in Get-Content -LiteralPath $Path -Encoding UTF8) {
        $trimmed = $line.Trim()
        if (-not $trimmed -or $trimmed.StartsWith("#")) {
            continue
        }

        $pattern = "^\s*{0}\s*=(.*)$" -f [regex]::Escape($Name)
        if ($line -match $pattern) {
            $value = $Matches[1].Trim()
            if (($value.StartsWith('"') -and $value.EndsWith('"')) -or ($value.StartsWith("'") -and $value.EndsWith("'"))) {
                $value = $value.Substring(1, $value.Length - 2)
            }
            return $value
        }
    }

    return $null
}

function Resolve-PythonExe {
    $candidates = New-Object System.Collections.Generic.List[string]

    if ($env:ANYROUTER_PYTHON) {
        $candidates.Add($env:ANYROUTER_PYTHON)
    }

    $candidates.Add((Join-Path $repoRoot ".venv\Scripts\python.exe"))
    $candidates.Add((Join-Path $env:USERPROFILE ".cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"))

    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if ($pythonCommand) {
        $candidates.Add($pythonCommand.Source)
    }

    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path -LiteralPath $candidate)) {
            return (Resolve-Path -LiteralPath $candidate).Path
        }
    }

    throw "Python was not found. Install Python or set ANYROUTER_PYTHON to python.exe."
}

New-Item -ItemType Directory -Force -Path $logDir | Out-Null
Set-Location -LiteralPath $repoRoot

try {
    $accountsJson = $env:ANYROUTER_ACCOUNTS
    if (-not $accountsJson) {
        $accountsJson = Get-DotEnvValue -Path $envPath -Name "ANYROUTER_ACCOUNTS"
    }
    if (-not $accountsJson) {
        throw "ANYROUTER_ACCOUNTS was not found. Configure it in .env first."
    }

    $parsedAccounts = ConvertFrom-Json -InputObject $accountsJson
    if ($parsedAccounts -is [array]) {
        $accounts = @($parsedAccounts)
    }
    else {
        $accounts = @($parsedAccounts)
    }

    $agentAccounts = @($accounts | Where-Object { $_.provider -eq "agentrouter" })
    if ($agentAccounts.Count -eq 0) {
        throw "No provider=agentrouter account was found in ANYROUTER_ACCOUNTS."
    }

    $agentAccountJsonItems = @()
    foreach ($agentAccount in $agentAccounts) {
        $agentAccountJsonItems += (ConvertTo-Json -InputObject $agentAccount -Depth 50 -Compress)
    }
    $env:ANYROUTER_ACCOUNTS = "[{0}]" -f ($agentAccountJsonItems -join ",")
    if ($NoNotify) {
        $env:SERVERCHAN_KEY = ""
    }

    $pythonExe = Resolve-PythonExe
    Write-RunLog ("AgentRouter local check-in started. account_count={0}" -f $agentAccounts.Count)
    Write-RunLog ("Python: {0}" -f $pythonExe)

    $previousErrorActionPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        & $pythonExe "checkin.py" 2>&1 | ForEach-Object { $_.ToString() } | Tee-Object -FilePath $logPath -Append
        $exitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }

    Write-RunLog ("AgentRouter local check-in finished. exit_code={0}" -f $exitCode)
    exit $exitCode
}
catch {
    Write-RunLog ("AgentRouter local check-in failed: {0}" -f $_.Exception.Message)
    exit 1
}
