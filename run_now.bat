@echo off
:: ============================================================
:: Loop Engineer - Manual Trigger (Windows)
:: Double-click to pick a project and run one phase.
:: Arguments are forwarded to run_now.ps1, e.g.:
::   run_now.bat -Project proj-a -NoPause
:: Exit code = the orchestrator's exit code.
:: ============================================================
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run_now.ps1" %*
exit /b %errorlevel%
