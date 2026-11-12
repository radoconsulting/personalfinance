@echo off
rem Runs centen with its data folder next to this file. Example: centen.cmd sync
set "CENTEN_HOME=%~dp0data"
"%~dp0.venv\Scripts\centen.exe" %*
