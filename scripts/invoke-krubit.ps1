param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$KrubitArguments = @("run")
)

$ErrorActionPreference = "Stop"

$masterEnvPath = "D:\Dropbox\05 Software Development\.env"
$projectPath = Split-Path -Parent $PSScriptRoot
$allowedNames = @(
    "DISCORD_KRUBIT_APPLICATION_ID",
    "DISCORD_KRUBIT_BOT_TOKEN",
    "KRUBIT_DATABASE_PATH",
    "KRUBIT_STAFF_CHANNEL_ID",
    "TWITCH_KRUBIT_CLIENT_ID",
    "TWITCH_KRUBIT_CLIENT_SECRET",
    "KRUBIT_LIVE_SIGNALS_ENABLED",
    "YOUTUBE_KRUBIT_API_KEY",
    "YOUTUBE_KRUBIT_PUSH_CALLBACK_SECRET",
    "X_KRUBIT_BEARER_TOKEN",
    "META_KRUBIT_APP_ID",
    "META_KRUBIT_APP_SECRET",
    "META_KRUBIT_CALLBACK_BASE_URL",
    "TIKTOK_KRUBIT_CLIENT_KEY",
    "TIKTOK_KRUBIT_CLIENT_SECRET",
    "TIKTOK_KRUBIT_CALLBACK_BASE_URL",
    "KRUBIT_CREATOR_SIGNALS_ENABLED",
    "KRUBIT_SOCIAL_DELIVERY_ENABLED",
    "KRUBIT_CREDENTIAL_ENCRYPTION_KEY",
    "KRUBIT_CALLBACK_PUBLIC_BASE_URL",
    "KRUBIT_CALLBACK_PORT",
    "KRUBIT_CALLBACK_BIND_HOST",
    "KRUBIT_WATCHDOG_ENABLED",
    "KRUBIT_WATCHDOG_NOTIFICATIONS_ENABLED",
    "KRUBIT_WATCHDOG_WATCH_WINDOW_HOURS",
    "KRUBIT_WATCHDOG_ZARIYA_BRIDGE_URL",
    "KRUBIT_ACTIVITY_LEDGER_ENABLED",
    "KRUBIT_ACTIVITY_LEDGER_EXCLUDED_CHANNEL_IDS",
    "KRUBIT_ACTIVITY_LEDGER_RETENTION_DAYS",
    "KRUBIT_ACTIVITY_LEDGER_INACTIVITY_THRESHOLD_DAYS"
)
$requiredNames = @(
    "DISCORD_KRUBIT_APPLICATION_ID",
    "DISCORD_KRUBIT_BOT_TOKEN",
    "KRUBIT_DATABASE_PATH"
)
$loadedNames = @{}

foreach ($line in Get-Content -LiteralPath $masterEnvPath) {
    if ($line -notmatch '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') {
        continue
    }

    $name = $matches[1]
    if ($allowedNames -notcontains $name) {
        continue
    }

    $value = $matches[2].Trim()
    if (
        ($value.StartsWith('"') -and $value.EndsWith('"')) -or
        ($value.StartsWith("'") -and $value.EndsWith("'"))
    ) {
        $value = $value.Substring(1, $value.Length - 2)
    }

    if ([string]::IsNullOrWhiteSpace($value)) {
        if ($requiredNames -contains $name) {
            throw "$name is empty in the master .env"
        }
        continue
    }

    [Environment]::SetEnvironmentVariable($name, $value, "Process")
    $loadedNames[$name] = $true
}

foreach ($name in $requiredNames) {
    if (-not $loadedNames.ContainsKey($name)) {
        throw "$name is missing from the master .env"
    }
}

Set-Location -LiteralPath $projectPath
$pythonPath = Join-Path $projectPath ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw "Krubit's virtual-environment Python is missing: $pythonPath"
}
& $pythonPath -m krubit @KrubitArguments
exit $LASTEXITCODE
