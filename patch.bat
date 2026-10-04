@echo off
setlocal DisableDelayedExpansion
set "OTIUM_PATCH_SCRIPT=%~dp0otium_patch.py"
if not exist "%OTIUM_PATCH_SCRIPT%" (
    echo Error: Missing otium_patch.py next to this launcher. 1>&2
    exit /b 1
)
rem cmd treats a backslash before a closing quote literally, but Python's
rem Windows argument parser treats it as an escaped quote. Re-quote cmd's
rem individual arguments and double only their trailing backslashes.
set "OTIUM_PATCH_ARGS="
:collect_arguments
if [%1]==[] goto find_python
set "OTIUM_PATCH_ARG=%~1"
set "OTIUM_PATCH_TRAILING="
:escape_trailing
if not "%OTIUM_PATCH_ARG:~-1%"=="\" goto append_argument
set "OTIUM_PATCH_ARG=%OTIUM_PATCH_ARG:~0,-1%"
set "OTIUM_PATCH_TRAILING=%OTIUM_PATCH_TRAILING%\\"
goto escape_trailing
:append_argument
set OTIUM_PATCH_ARGS=%OTIUM_PATCH_ARGS% "%OTIUM_PATCH_ARG%%OTIUM_PATCH_TRAILING%"
shift
goto collect_arguments

:find_python
where py >nul 2>&1
if errorlevel 1 goto try_python
py -3 -B "%OTIUM_PATCH_SCRIPT%" %OTIUM_PATCH_ARGS%
exit /b %errorlevel%

:try_python
where python >nul 2>&1
if errorlevel 1 goto missing_python
python -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>&1
if errorlevel 1 goto missing_python
python -B "%OTIUM_PATCH_SCRIPT%" %OTIUM_PATCH_ARGS%
exit /b %errorlevel%

:missing_python
echo Error: Python 3.10 or newer is required. Install Python and add py or python to PATH. 1>&2
exit /b 1
