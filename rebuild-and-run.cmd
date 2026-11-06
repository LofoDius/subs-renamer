@echo off
setlocal
pushd "%~dp0"
if errorlevel 1 exit /b 1

where uv >nul 2>&1
if errorlevel 1 (
    echo ERROR: uv is not installed or is not available in PATH.
    goto :failed
)

echo Installing project dependencies...
uv sync --frozen --group dev
if errorlevel 1 goto :failed

echo Building SubsRenamer.exe...
uv run --no-sync python scripts/build.py
if errorlevel 1 goto :failed

if not exist "dist\SubsRenamer.exe" (
    echo ERROR: The build did not produce dist\SubsRenamer.exe.
    goto :failed
)

echo Starting SubsRenamer...
start "" /D "%CD%\dist" "%CD%\dist\SubsRenamer.exe"
if errorlevel 1 goto :failed

popd
exit /b 0

:failed
echo.
echo Build or launch failed. See the error above.
echo If SubsRenamer is already running, close it before rebuilding.
pause
popd
exit /b 1
