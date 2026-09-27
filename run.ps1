param([int]$Port = 8000, [int]$UserId = 1, [string]$Database = '')
$ErrorActionPreference = 'Stop'
$pythonCommand = Get-Command python -ErrorAction SilentlyContinue
if ($pythonCommand) {
    $gatePython = $pythonCommand.Source
} else {
    $gatePython = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
}
if (-not (Test-Path -LiteralPath $gatePython)) { throw 'Please install Python 3.10 or later.' }
$gateArguments = @((Join-Path $PSScriptRoot 'app.py'), '--port', "$Port", '--user', "$UserId")
if ($Database) { $gateArguments += @('--db', $Database) }
& $gatePython @gateArguments
