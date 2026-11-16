$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$env:PYTHONPATH = Join-Path $root "backend"
$python = Join-Path $root "..\.venv\Scripts\python.exe"
if (-not (Test-Path $python)) { $python = (Get-Command python.exe -ErrorAction Stop).Source }
Push-Location (Join-Path $root "backend")
try { & $python -m uvicorn app.taas_app:app --host 0.0.0.0 --port 8100 } finally { Pop-Location }
