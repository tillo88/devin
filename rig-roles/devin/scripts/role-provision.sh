#!/bin/bash
# =============================================================================
# STAGE 4/4 — Provisioning ruolo: devin
# =============================================================================
set -e
exec >> /var/log/ai-rig-stage-role.log 2>&1
echo "=== Stage 4 (ruolo devin) - $(date) ==="

mkdir -p /var/lib/ai-rig /etc/ai-rig /opt/models/devin /opt/ai-rig/kv-sessions
if [ -f /var/lib/ai-rig/stage-role-done ]; then
    echo "Stage ruolo gia' completato, esco."
    exit 0
fi

echo "devin" > /etc/ai-rig/role

MODEL_SRC="/opt/cache/models/devin/Ornith-1.0-35B-A3B-MXFP4_MOE_Q8_0_F16-Imatrix.gguf"
MODEL_DST="/opt/models/devin/Ornith-1.0-35B-A3B-MXFP4_MOE_Q8_0_F16-Imatrix.gguf"
if [ -f "$MODEL_SRC" ]; then
    cp "$MODEL_SRC" "$MODEL_DST"
else
    echo "!!! Modello non trovato in $MODEL_SRC — copialo in cache/models/devin/ e ricostruisci la ISO." >&2
fi

MMPROJ_DST=""
if [ -n "" ]; then
    MMPROJ_SRC="/opt/cache/models/devin/"
    MMPROJ_DST="/opt/models/devin/"
    if [ -f "$MMPROJ_SRC" ]; then
        cp "$MMPROJ_SRC" "$MMPROJ_DST"
    else
        echo "!!! mmproj non trovato in $MMPROJ_SRC" >&2
        MMPROJ_DST=""
    fi
fi

cat > "/etc/ai-rig/devin.env" << EOFENV
ROLE_NAME=devin
ROLE_MODEL_PATH=${MODEL_DST}
ROLE_MMPROJ_PATH=${MMPROJ_DST}
ROLE_LLAMA_PORT=8080
ROLE_CTX_SIZE=12288
ROLE_TEMP=0.7
ROLE_TOP_P=0.9
ROLE_REPEAT_PENALTY=1.0
ROLE_EXTRA_ARGS="--top-k 20 --min-p 0.01 --slot-save-path /opt/ai-rig/kv-sessions"
# Derivati da LLAMA_FLAVOR (config/rig.env) — riscritti da swap-llama-flavor.sh
LLAMA_FLAVOR=beellama
ROLE_CACHE_TYPE_K=turbo4
ROLE_CACHE_TYPE_V=turbo3_tcq
ROLE_FLASH_ATTN=on
EOFENV

# Python venv dedicato al ruolo (requirements/devin.txt + requirements/common.txt)
if [ -f /opt/cache/requirements/devin.txt ]; then
    python3 -m venv "/opt/venv-devin"
    "/opt/venv-devin/bin/pip" install --upgrade pip -q
    [ -f /opt/cache/requirements/common.txt ] && "/opt/venv-devin/bin/pip" install -q -r /opt/cache/requirements/common.txt
    "/opt/venv-devin/bin/pip" install -q -r /opt/cache/requirements/devin.txt
    chown -R tillo:tillo "/opt/venv-devin"
fi

chmod +x /usr/local/bin/start-llama-devin.sh 2>/dev/null || true
systemctl daemon-reload
systemctl enable "llama-server@devin.service"
systemctl start "llama-server@devin.service" || echo "!!! avvio llama-server fallito, controlla journalctl" >&2

# Dashboard DEVIN AI IDE (fast_app.py): il CODICE non viaggia con la ISO (progetto
# separato) — prepara solo la cartella di destinazione. Deploy reale via
# scripts/deploy-devin-webapp.sh (rsync dalla build machine), poi:
#   systemctl enable --now devin-webapp
# Enable si', start no: senza codice il servizio fallirebbe in loop ad ogni boot,
# riempiendo i log inutilmente finche' non fai il deploy.
mkdir -p /opt/devin-ai-ide
chown tillo:tillo /opt/devin-ai-ide
systemctl enable devin-webapp.service 2>/dev/null || echo "!!! devin-webapp.service non trovato, verra' installato dal deploy script" >&2

touch /var/lib/ai-rig/stage-role-done
echo "=== Stage 4 completato - $(date) ==="

# Verifica finale (best effort, non blocca il boot se fallisce)
/usr/local/bin/90-verify.sh || true
