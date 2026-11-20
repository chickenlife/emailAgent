$Host.UI.RawUI.WindowTitle = "My AI Secretary - Mail"
Set-Location $PSScriptRoot
$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
$requirements = Join-Path $PSScriptRoot "requirements.txt"
$main = Join-Path $PSScriptRoot "server.py"
Write-Host "============================================"
Write-Host " My AI Secretary - Mail Edition"
Write-Host "============================================"
if (-not (Test-Path $python)) {
    Write-Host "[INFO] Creating venv..."
    py -m venv .venv
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $python)) {
        Write-Host "[ERROR] Cannot create venv. Check installed Python." -ForegroundColor Red
        return
    }
}
if (-not (Test-Path $requirements) -or -not (Test-Path $main)) {
    Write-Host "[ERROR] Project files missing." -ForegroundColor Red
    return
}
# Use the environment's own pip, not global pip.
# Avoid multiline Python -c quoting issues in Windows PowerShell 5.1.
$checkFile = Join-Path $PSScriptRoot "check_dependencies.py"
& $python $checkFile
if ($LASTEXITCODE -ne 0) {
    Write-Host "[INFO] Installing missing dependencies..." -ForegroundColor Yellow
    & $python -m pip install -r $requirements
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[ERROR] pip installation failed." -ForegroundColor Red
        return
    }
    & $python $checkFile
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[ERROR] Dependencies still missing." -ForegroundColor Red
        return
    }
}
Write-Host "[INFO] Starting MailAgent web service (see .env WEB_BIND_HOST / WEB_PORT)"
Write-Host "[INFO] Press Ctrl+C to stop."
& $python -u $main
if ($LASTEXITCODE -ne 0) { Write-Host "[ERROR] MailAgent exited: $LASTEXITCODE" -ForegroundColor Red }
