@echo off
rem SHADOW LAB — отдельный процесс виртуального (paper) трейдера SHADOW-P2-4H 1.0.0.
rem Не заменяет start.bat: основной бот (start.bat) должен быть запущен — SHADOW читает его данные (только чтение).
rem Своя база data\shadow_lab.db, свой журнал data\logs\shadow.log. Реальных транзакций нет.
rem Запуск процесса НЕ начинает эксперимент: START — только вручную на странице SHADOW LAB (http://127.0.0.1:8770/shadow).
rem Проверка готовности без запуска цикла:  shadow.bat --check
chcp 65001 >nul
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo.
  echo   Окружение .venv не найдено. Сначала запустите start.bat ^(он создаёт окружение и запускает основной бот^).
  echo.
  pause
  exit /b 1
)
if not exist "data\trading_lab.db" (
  echo.
  echo   Нет data\trading_lab.db. Сначала запустите основной бот start.bat — SHADOW использует его рыночные данные.
  echo.
  pause
  exit /b 1
)

".venv\Scripts\python.exe" -m tradinglab shadow %*
echo.
echo   SHADOW LAB остановлен. Состояние сохранено в data\shadow_lab.db ^(основной бот не затронут^).
pause
exit /b 0
