#!/bin/bash
# =============================================================================
# scripts/deploy-devin-webapp.sh — deploy del progetto DEVIN AI IDE sul rig
# =============================================================================
# Il CODICE di devin_ai_ide non viaggia dentro la ISO (e' un progetto separato,
# che evolve indipendentemente da questo). Questo script lo copia sul disco
# DEVIN gia' installato, via rsync, e (ri)avvia la dashboard.
#
# Uso (dalla tua build machine/PC, con il rig gia' installato e raggiungibile):
#   bash scripts/deploy-devin-webapp.sh [percorso-locale-devin-ai-ide] [utente@host]
#
# Esempio:
#   bash scripts/deploy-devin-webapp.sh ~/devin_ai_ide tillo@192.168.1.100
#
# Ripetibile quanto vuoi: ogni aggiornamento del progetto DEVIN AI IDE si
# ridistribuisce ri-lanciando questo stesso comando (rsync --delete tiene il
# rig allineato a quello che hai in locale, comprese le rimozioni di file).
# =============================================================================
set -e

LOCAL_PATH="${1:-$HOME/devin_ai_ide}"
REMOTE="${2:-tillo@192.168.1.100}"
REMOTE_PATH="/opt/devin-ai-ide"

if [ ! -d "$LOCAL_PATH" ]; then
    echo "Errore: $LOCAL_PATH non esiste. Uso: $0 <percorso-locale> [utente@host]" >&2
    exit 1
fi

echo "=== Deploy DEVIN AI IDE -> ${REMOTE}:${REMOTE_PATH} ==="
echo "Sorgente: $LOCAL_PATH"
echo ""

# --delete: il rig resta identico a quello che hai in locale (comprese le
# rimozioni). Escludi cio' che NON deve mai viaggiare: config con segreti gia'
# scritti sul rig (l'utente potrebbe averli personalizzati li'), venv locali,
# cache/log del PC di sviluppo.
rsync -avz --delete \
    --exclude '.git' \
    --exclude '__pycache__' \
    --exclude '*.pyc' \
    --exclude 'venv/' \
    --exclude '.venv/' \
    --exclude 'logs/' \
    --exclude '.devin_state/' \
    --exclude '.devin_chat/' \
    --exclude 'devin/ui/.env' \
    "$LOCAL_PATH/" "${REMOTE}:${REMOTE_PATH}/"

echo ""
echo "=== File copiati. Verifico config/settings.json e .env sul rig... ==="
ssh "$REMOTE" "test -f ${REMOTE_PATH}/config/settings.json && echo 'OK: settings.json presente' || echo 'ATTENZIONE: settings.json mancante, la dashboard non partira'"
ssh "$REMOTE" "test -f ${REMOTE_PATH}/devin/ui/.env && echo 'OK: .env presente (TinyFish key)' || echo 'NOTA: devin/ui/.env non trovato — crealo sul rig se vuoi la web search (vedi README progetto DEVIN AI IDE)'"

echo ""
echo "=== Riavvio servizio dashboard sul rig... ==="
ssh "$REMOTE" "sudo systemctl daemon-reload && sudo systemctl enable --now devin-webapp.service"
sleep 3
ssh "$REMOTE" "sudo systemctl is-active devin-webapp.service" || echo "!!! Servizio non attivo, controlla: ssh $REMOTE 'journalctl -u devin-webapp -n 50'" >&2

echo ""
echo "=== Fatto. Dashboard su http://${REMOTE#*@}:5000 ==="
