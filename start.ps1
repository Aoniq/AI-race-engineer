Set-Location -LiteralPath $PSScriptRoot
$python = Join-Path $PSScriptRoot "venv\Scripts\python.exe"

if (-not (Test-Path $python)) {
  Write-Host "[ERROR] venv not found. Run: python -m venv venv; .\venv\Scripts\pip install -r requirements.txt"
  pause
  exit 1
}

& $python -c "import sys" 2>$null
if ($LASTEXITCODE -ne 0) {
  Write-Host "[ERROR] venv is broken (base Python missing). Recreate it:"
  Write-Host "  Remove-Item -Recurse -Force venv"
  Write-Host "  python -m venv venv"
  Write-Host "  .\venv\Scripts\pip install -r requirements.txt"
  pause
  exit 1
}

Write-Host "[1/3] Starting server..."
Start-Process -FilePath $python -ArgumentList "server.py" -WorkingDirectory $PSScriptRoot

Write-Host "[2/3] Waiting for server..."
$ready = $false
for ($i = 0; $i -lt 30; $i++) {
  Start-Sleep -Seconds 1
  & $python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000', timeout=1)" 2>$null
  if ($LASTEXITCODE -eq 0) {
    $ready = $true
    break
  }
}

if (-not $ready) {
  Write-Host "[ERROR] Server did not become ready on http://127.0.0.1:8000"
  Write-Host "Check the Server window for errors."
  pause
  exit 1
}

Write-Host "[3/3] Server ready, starting simulator..."
Start-Process -FilePath $python -ArgumentList "simulator.py" -WorkingDirectory $PSScriptRoot
Write-Host "Done. Open http://127.0.0.1:8000 if the browser did not open."
pause
