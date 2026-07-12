@echo off
title DEVIN AI IDE

echo ============================================================
echo   DEVIN AI IDE - Avvio da Windows verso WSL2
echo ============================================================
echo.

REM Esegue scripts/run.sh dentro WSL (distro di default). run.sh si occupa
REM di posizionarsi nella root del progetto e attivare il venv da solo.
REM -l  = login shell (carica .bashrc/.profile)
REM -i  = interattiva
REM -c  = comando da eseguire
wsl bash -lic "cd ~/devin_ai_ide && bash scripts/run.sh"

if %ERRORLEVEL% NEQ 0 (
    echo.
    echo ============================================================
    echo   Errore durante l'avvio. Controlla che:
    echo   - WSL sia installato e funzionante ^(prova: wsl --status^)
    echo   - il progetto sia in ~/devin_ai_ide dentro la distro WSL
    echo   - il venv esista in ~/devin_ai_ide/venv
    echo ============================================================
)

pause
