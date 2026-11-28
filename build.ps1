$ErrorActionPreference = "Stop"

# 1. Chạy test trước khi đóng gói
python -m pytest -q

# 2. Dọn thư mục build/dist cũ
if (Test-Path build) { Remove-Item -Recurse -Force build }
if (Test-Path dist) { Remove-Item -Recurse -Force dist }

# 3. Đóng gói thành DUY NHẤT 1 file SDN-Emulator.exe (--onefile)
$iconArgs = @()
if (Test-Path "desktop\app.ico") {
  $iconArgs = @("--icon", "desktop\app.ico", "--add-data", "desktop\app.ico;desktop")
}

python -m PyInstaller `
  --noconfirm `
  --clean `
  --onefile `
  --noconsole `
  --name "SDN-Emulator" `
  @iconArgs `
  --paths "src" `
  --add-data "src\sdnsim\ui;sdnsim\ui" `
  --add-data "examples\os_ken_controller.py;examples" `
  --hidden-import "clr" `
  --hidden-import "pythonnet" `
  --collect-submodules "sdnsim" `
  --collect-submodules "uvicorn" `
  --collect-submodules "websockets" `
  --collect-all "webview" `
  --collect-all "pythonnet" `
  --collect-all "clr_loader" `
  --collect-all "os_ken" `
  --collect-all "oslo_config" `
  --collect-all "pbr" `
  --copy-metadata "os-ken" `
  --copy-metadata "pbr" `
  --copy-metadata "fastapi" `
  --copy-metadata "uvicorn" `
  desktop\main.py

Write-Host "`nBuild thanh cong! Copy file nay sang may khac de chay:" -ForegroundColor Green
Write-Host "  dist\SDN-Emulator.exe" -ForegroundColor Cyan