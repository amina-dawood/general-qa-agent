@echo off
taskkill /FI "WINDOWTITLE eq General QA Agent*" /T /F >nul 2>&1
echo General QA Agent stopped.
