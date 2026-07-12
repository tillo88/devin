#!/bin/bash
# Si posiziona sempre nella root del progetto, indipendentemente da dove/come
# viene lanciato questo script (doppio click, altra cartella, symlink, ecc.) —
# stesso principio dei fix sui path assoluti nel resto del progetto.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_ROOT" || { echo "Impossibile raggiungere $PROJECT_ROOT"; exit 1; }

echo "Starting DEVIN AI IDE..."
echo "Root progetto: $PROJECT_ROOT"

# Attiva il venv se presente, senza obbligare l'utente a farlo a mano ogni volta
if [ -f "venv/bin/activate" ]; then
    source venv/bin/activate
    echo "venv attivato"
else
    echo "ATTENZIONE: venv/bin/activate non trovato, uso il Python di sistema"
fi

python3 launcher.py
