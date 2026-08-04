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
        throw "$name is empty in the master .env"
    }

    [Environment]::SetEnvironmentVariable($name, $value, "Process")
    $loadedNames[$name] = $true
}

foreach ($name in $allowedNames) {
    if (-not $loadedNames.ContainsKey($name)) {
        throw "$name is missing from the master .env"
    }
}

Set-Location -LiteralPath $projectPath
& uv run python -m krubit @KrubitArguments
exit $LASTEXITCODE
