@echo off
setlocal
chcp 65001 >nul
set "AVAT_RECOVERY_CSV=%~1"
set "AVAT_RECOVERY_ROOT=%~2"
if not defined AVAT_RECOVERY_CSV set /p "AVAT_RECOVERY_CSV=복구할 조사 CSV 경로: "
if not defined AVAT_RECOVERY_ROOT set /p "AVAT_RECOVERY_ROOT=작업 폴더 경로 (01_사고동영상의 상위 폴더): "
set "AVAT_RECOVERY_CSV=%AVAT_RECOVERY_CSV:"=%"
set "AVAT_RECOVERY_ROOT=%AVAT_RECOVERY_ROOT:"=%"
if not defined AVAT_RECOVERY_CSV exit /b 1
if not defined AVAT_RECOVERY_ROOT exit /b 1
set "AVAT_RECOVERY_PYTHON=python"
if exist "%~dp0.venv\Scripts\python.exe" set "AVAT_RECOVERY_PYTHON=%~dp0.venv\Scripts\python.exe"
echo AVAT를 종료한 상태에서 실행하세요. 기존 DB는 덮어쓰지 않습니다.
"%AVAT_RECOVERY_PYTHON%" "%~dp0recover_investigation_csv.py" --source "%AVAT_RECOVERY_CSV%" --workspace "%AVAT_RECOVERY_ROOT%"
set "AVAT_RECOVERY_RESULT=%ERRORLEVEL%"
echo.
if "%AVAT_RECOVERY_RESULT%"=="0" (echo CSV 복구 완료) else (echo 복구가 중단되었습니다. 위 메시지를 확인하세요.)
pause
exit /b %AVAT_RECOVERY_RESULT%
