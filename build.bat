@echo off
rem Builds dist\NFC-Credit.exe  (needs: pip install -r requirements.txt pyinstaller)
cd /d "%~dp0"
if not exist build mkdir build
python -c "import nfc_credit as n; n.make_icon('ok', 256).save('build\icon.ico', sizes=[(16,16),(24,24),(32,32),(48,48),(64,64),(128,128),(256,256)])" || exit /b 1
python -m PyInstaller --onefile --windowed --name NFC-Credit --icon icon.ico --distpath dist --workpath build --specpath build --noconfirm nfc_credit.py
