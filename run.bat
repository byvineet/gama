@echo off
REM ============================================================
REM  Gama - Quick Launcher
REM  By Vineet Machchal
REM ============================================================
cd /d "%~dp0"

if not exist ".venv\Scripts\activate.bat" (
    echo [ERROR] Virtual environment not found. Run setup.bat first.
    pause
    exit /b 1
)

call ".venv\Scripts\activate.bat"

REM Cap native math library threads BEFORE any Python import that
REM pulls in numpy / OpenBLAS / MKL. Prevents:
REM   "BLAS : Bad memory allocation"
REM and silent process kills under concurrent AEC + model load.
set OPENBLAS_NUM_THREADS=1
set GOTO_NUM_THREADS=1
set OMP_NUM_THREADS=1
set MKL_NUM_THREADS=1
set NUMEXPR_NUM_THREADS=1
set VECLIB_MAXIMUM_THREADS=1
set ORT_NUM_THREADS=1

REM Unbuffered stdout so a hard crash still shows the last lines
set PYTHONUNBUFFERED=1
if not exist logs mkdir logs

python -u main.py 2> logs\stderr_last.txt
set EXITCODE=%ERRORLEVEL%
if exist logs\startup_crash.txt (
    echo.
    echo [ERROR] startup_crash.txt found:
    type logs\startup_crash.txt
)
if %EXITCODE% NEQ 0 (
    echo.
    echo [ERROR] Gama exited with an error (code %EXITCODE%).
    echo Check logs\gama.log and logs\crashes\ for details.
    pause
)
exit /b %EXITCODE%
