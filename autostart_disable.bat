@echo off
rem Выключить автозапуск FABLES Trading Lab (удалить ярлык из папки «Автозагрузка»).
chcp 65001 >nul
set "LNK=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\FABLES Trading Lab.lnk"
if exist "%LNK%" (del "%LNK%" && echo   Автозапуск выключен.) else (echo   Автозапуск не был включён.)
pause
