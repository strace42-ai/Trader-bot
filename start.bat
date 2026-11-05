@echo off
rem FABLES Trading Lab — фоновый бот + панель в одном окне. Двойной щелчок по этому файлу.
rem Бот работает, пока открыто это окно (закрытие вкладки браузера его не останавливает).
chcp 65001 >nul
setlocal
cd /d "%~dp0"

set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (
  where python >nul 2>nul && set "PY=python"
)
if not defined PY (
  echo.
  echo   Python не найден. Установите Python 3.11 или новее с https://www.python.org/downloads/
  echo   и при установке отметьте "Add python.exe to PATH". Затем запустите start.bat снова.
  echo.
  pause
  exit /b 1
)

%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)"
if errorlevel 1 (
  echo.
  echo   Нужен Python 3.11 или новее. Установленная версия:
  %PY% --version
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo   Первый запуск: создаю окружение .venv ...
  %PY% -m venv .venv
  if errorlevel 1 goto :fail
)

echo   Проверяю зависимости ...
if not exist "data\logs" mkdir "data\logs"
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements.txt > "data\logs\install.log" 2>&1
if errorlevel 1 goto :fail

".venv\Scripts\python.exe" -m tradinglab serve %*
echo.
echo   Бот и панель остановлены. Состояние сохранено в data\trading_lab.db
pause
exit /b 0

:fail
echo.
echo   Ошибка установки зависимостей. Подробности: data\logs\install.log
echo   Частые причины: нет интернета (нужен только при первом запуске), антивирус/прокси блокирует pip,
echo   повреждено окружение .venv (удалите папку .venv и запустите снова). Диагностика: diagnose.bat
pause
exit /b 1
