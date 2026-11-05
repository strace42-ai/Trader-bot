@echo off
rem Включить автозапуск FABLES Trading Lab при входе в Windows (ярлык в папке «Автозагрузка» текущего пользователя).
rem Не служба и не задание планировщика: удаляется autostart_disable.bat. Запускайте только если это нужно.
chcp 65001 >nul
cd /d "%~dp0"
set "LNK=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\FABLES Trading Lab.lnk"
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:LNK); $s.TargetPath='%~dp0start.bat'; $s.Arguments='--no-browser'; $s.WorkingDirectory='%~dp0'; $s.WindowStyle=7; $s.Description='FABLES Trading Lab (paper)'; $s.Save()"
if exist "%LNK%" (
  echo   Автозапуск включён: "%LNK%"
  echo   После входа в Windows бот запустится в свёрнутом окне; панель: http://127.0.0.1:8770/
) else (
  echo   Не удалось создать ярлык.
)
pause
