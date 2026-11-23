# Đóng gói SDN Emulator thành ứng dụng Windows (.exe)
$ErrorActionPreference = "Stop"

# 1. Kiểm tra pytest trước khi build
pytest -q

# 2. Dọn thư mục build/dist cũ
if (Test-Path build) { Remove-Item -Recurse -Force build }
if (Test-Path dist) { Remove-Item -Recurse -Force dist }

# 3. Đóng gói dạng thư mục (--onedir: mở nhanh, ít bị Windows Defender chặn nhầm)
#    Đổi --onedir thành --onefile nếu muốn xuất ra duy nhất 1 file .exe
#    (Bỏ --noconsole ở lần build thử đầu tiên nếu muốn nhìn log lỗi trực tiếp)
pyinstaller `
  --noconfirm `
  --clean `
  --onedir `
  --noconsole `
  --name "SDN-Emulator" `
  --icon "desktop\app.ico" `
  --paths "src" `
  --add-data "desktop\app.ico;desktop" `
  --add-data "src\sdnsim\ui;sdnsim\ui" `
  --add-data "topologies;topologies" `
  --add-data "examples\os_ken_controller.py;examples" `
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
  desktop\main.py

Write-Host "`nBuild xong! Chay ung dung tai: dist\SDN-Emulator\SDN-Emulator.exe" -ForegroundColor Green