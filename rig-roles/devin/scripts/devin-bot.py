#!/usr/bin/env python3
"""
DEVIN Telegram Bot — dedicato al ruolo DEVIN (coding/debug agent).
Vive sullo stesso disco/systemd della dashboard fast_app.py (localhost:5000),
si accende/spegne insieme al ruolo — quando il rig boota Hermes o Teacher,
questo servizio semplicemente non gira.

Config in /etc/devin-bot/config.env (NON nel codice, stesso principio del
bot WOL: vedi devin-bot.env.example).

Comandi: /project <path>, /fixit, /status, /runs, /help, testo libero (chat)
"""

import os
import sys
import time
import json
import logging
import urllib.request
import urllib.error
import urllib.parse
from pathlib import Path
from datetime import datetime, timezone

# =============================================================================
# Config — stesso pattern del bot WOL: caricata da file esterno, MAI hardcoded.
# =============================================================================
CONFIG_PATH = Path(os.environ.get("DEVIN_BOT_CONFIG", "/etc/devin-bot/config.env"))


def load_config(path: Path) -> dict:
    if not path.is_file():
        print(f"Errore: file di config non trovato: {path}", file=sys.stderr)
        sys.exit(1)
    cfg = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        cfg[k.strip()] = v.strip().strip('"').strip("'")
    required = ["BOT_TOKEN", "ALLOWED_CHAT_IDS"]
    missing = [k for k in required if not cfg.get(k) or cfg[k].startswith("CHANGEME")]
    if missing:
        print(f"Errore: compila questi campi in {path}: {missing}", file=sys.stderr)
        sys.exit(1)
    return cfg


CFG = load_config(CONFIG_PATH)
BOT_TOKEN = CFG["BOT_TOKEN"]
ALLOWED_CHAT_IDS = {c.strip() for c in CFG["ALLOWED_CHAT_IDS"].split(",") if c.strip()}
DEVIN_API_URL = CFG.get("DEVIN_API_URL", "http://localhost:5000").rstrip("/")
POLL_INTERVAL_SECONDS = int(CFG.get("POLL_INTERVAL_SECONDS", "15"))
SILENCE_HOURS = float(CFG.get("SILENCE_HOURS", "12"))
CHAT_TIMEOUT_SECONDS = int(CFG.get("CHAT_TIMEOUT_SECONDS", "180"))

_handlers = [logging.StreamHandler()]
try:
    _handlers.append(logging.FileHandler("/var/log/devin-bot.log"))
except (PermissionError, OSError):
    pass
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    handlers=_handlers,
)
logger = logging.getLogger(__name__)

# =============================================================================
# Stato persistito: offset long-poll + focus-progetto per chat_id + run gia'
# notificati + silenzi gia' segnalati (evita di ri-notificare ad ogni poll).
# Fallback nella home se /var/lib non e' scrivibile (stesso motivo del bot WOL:
# l'utente del servizio potrebbe non coincidere con quello previsto a mano).
# =============================================================================
STATE_FILE = Path("/var/lib/devin-bot/state.json")
try:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.touch(exist_ok=True)
except (PermissionError, OSError):
    STATE_FILE = Path.home() / ".devin-bot-state.json"

_DEFAULT_STATE = {
    "offset": 0,
    "focus": {},              # {chat_id_str: project_path}
    "seen_run_status": {},    # {run_id: last_status_visto}
    "notified_silence": {},   # {chat_id_str: updated_at_gia_notificato}
    "last_periodic_check": 0,
}


def load_state() -> dict:
    try:
        data = json.loads(STATE_FILE.read_text())
        merged = dict(_DEFAULT_STATE)
        merged.update(data)
        return merged
    except Exception:
        return dict(_DEFAULT_STATE)


def save_state(state: dict) -> None:
    try:
        STATE_FILE.write_text(json.dumps(state))
    except (PermissionError, OSError) as e:
        logger.warning(f"Impossibile salvare stato in {STATE_FILE}: {e}")


# =============================================================================
# Telegram — stesso approccio stdlib-only del bot WOL (niente python-telegram-bot).
# =============================================================================
MD_ESCAPE_CHARS = set("_*[]()~`>#+-=|{}.!")


def escape_markdown(text: str) -> str:
    return "".join("\\" + c if c in MD_ESCAPE_CHARS else c for c in text)


def send_message(chat_id, text: str, markdown: bool = False) -> None:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text[:4000], "disable_web_page_preview": True}
    if markdown:
        payload["parse_mode"] = "Markdown"
    try:
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status != 200:
                logger.error(f"Errore invio messaggio: {resp.read()}")
    except Exception as e:
        logger.error(f"Errore invio risposta: {e}")


def send_to_all_allowed(text: str) -> None:
    """Notifiche globali (fine-run): a tutti i chat_id autorizzati, non solo a chi ha chiesto."""
    for chat_id in ALLOWED_CHAT_IDS:
        send_message(chat_id, text)


def is_authorized(chat_id) -> bool:
    return str(chat_id) in ALLOWED_CHAT_IDS


# =============================================================================
# DEVIN API (localhost:5000) — helper HTTP minimali, stesso stile urllib.
# =============================================================================
def devin_get(path: str, timeout: int = 15):
    try:
        req = urllib.request.Request(f"{DEVIN_API_URL}{path}")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        logger.error(f"GET {path} fallita: {e}")
        return None


def devin_post_json(path: str, payload: dict, timeout: int = 15):
    try:
        req = urllib.request.Request(
            f"{DEVIN_API_URL}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        logger.error(f"POST {path} fallita: {e}")
        return None


def consume_chat_stream(project_path: str, message: str) -> str:
    """
    POST /api/chat e' SSE streaming. Per Telegram vogliamo un messaggio
    completo (deciso cosi': niente editing progressivo, troppa complessita'
    per poco valore in v1) — consumiamo tutto lo stream e concateniamo.
    """
    url = f"{DEVIN_API_URL}/api/chat"
    payload = {"message": message, "mode": "auto", "project_path": project_path, "use_web_search": False}
    full_text = []
    warning = None
    try:
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=CHAT_TIMEOUT_SECONDS) as resp:
            buffer = ""
            for raw_line in resp:
                line = raw_line.decode("utf-8", errors="ignore")
                buffer += line
                if buffer.endswith("\n\n") or line.strip() == "":
                    for chunk_line in buffer.strip().split("\n"):
                        if chunk_line.startswith("data:"):
                            try:
                                data = json.loads(chunk_line[5:].strip())
                            except json.JSONDecodeError:
                                continue
                            if "token" in data:
                                full_text.append(data["token"])
                            elif "message" in data and "event: warning" in buffer:
                                warning = data["message"]
                    buffer = ""
    except Exception as e:
        logger.error(f"Errore stream chat: {e}")
        return f"❌ Errore comunicando con DEVIN: {e}"

    result = "".join(full_text).strip() or "(nessuna risposta generata)"
    if warning:
        result = f"⚠️ {warning}\n\n{result}"
    return result


# =============================================================================
# Comandi
# =============================================================================
HELP_TEXT = (
    "DEVIN Bot — comandi disponibili:\n\n"
    "/project <path> - imposta il progetto a fuoco per questa chat\n"
    "/fixit - genera patch dalla conversazione salvata sul progetto a fuoco e riprova\n"
    "/status - stato rig/locale + modelli attivi\n"
    "/runs - ultimi run (Mantenimento/Scaffolding)\n"
    "/help - questo messaggio\n\n"
    "Testo libero (senza /) viene inoltrato come chat sul progetto a fuoco."
)


def handle_project(chat_id: str, text: str, state: dict) -> str:
    parts = text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        current = state["focus"].get(chat_id)
        return f"Progetto a fuoco attuale: {current}" if current else "Nessun progetto a fuoco. Uso: /project workspace/nome_progetto"
    project_path = parts[1].strip()
    state["focus"][chat_id] = project_path
    save_state(state)
    return f"📌 Progetto a fuoco impostato: {project_path}"


def handle_fixit(chat_id: str, state: dict) -> str:
    project_path = state["focus"].get(chat_id)
    if not project_path:
        return "Nessun progetto a fuoco. Usa prima /project <path>."
    result = devin_post_json("/api/chat/generate_patch", {"project_path": project_path}, timeout=15)
    if not result:
        return "❌ Errore contattando DEVIN (dashboard offline?)."
    if result.get("error"):
        return f"❌ {result['error']}"
    return f"🔧 Patch in corso (run {result['run_id']}). Ti aggiorno con /runs o aspetta la notifica automatica."


def handle_status(state: dict) -> str:
    health = devin_get("/api/health", timeout=10)
    models_info = devin_get("/api/models/info", timeout=10)
    if health is None and models_info is None:
        return "❌ Dashboard DEVIN non raggiungibile su " + DEVIN_API_URL

    lines = []
    if health:
        rig_ok = health.get("remote_coder") and health.get("remote_reasoning")
        lines.append(f"Rig: {'🟢 OK' if rig_ok else '🔴 offline'}")
    if models_info:
        lines.append(f"Locale: {'🟢 attivo' if models_info.get('running') else '⚪ spento'}")
        vram = models_info.get("vram")
        if vram:
            lines.append(f"VRAM: {vram.get('used_mb')}/{vram.get('total_mb')} MB")
    return "\n".join(lines) if lines else "Nessuna informazione disponibile."


def handle_runs() -> str:
    runs = devin_get("/api/runs", timeout=10)
    if not runs:
        return "Nessun run trovato."
    lines = []
    icon = {"success": "✅", "failed": "❌", "timeout": "⏱️", "stopped": "🛑", "unknown": "❔"}
    for r in runs[:10]:
        lines.append(f"{icon.get(r['status'], '❔')} {r['run_id']} — {r['status']}")
    return "\n".join(lines)


def handle_command(text: str, chat_id: str, state: dict) -> str:
    cmd_full = text.split(maxsplit=1)
    cmd = cmd_full[0].split("@")[0]

    if cmd == "/project":
        return handle_project(chat_id, text, state)
    if cmd == "/fixit":
        return handle_fixit(chat_id, state)
    if cmd == "/status":
        return handle_status(state)
    if cmd == "/runs":
        return handle_runs()
    if cmd in ("/help", "/start"):
        return HELP_TEXT

    # Testo libero -> chat sul progetto a fuoco
    project_path = state["focus"].get(chat_id)
    if not project_path:
        return "Nessun progetto a fuoco. Usa /project <path> prima di chattare, oppure /help."
    return consume_chat_stream(project_path, text)


# =============================================================================
# Task periodici (fold nel loop principale, niente thread separati — stesso
# stile single-thread del bot WOL, piu' semplice da ragionarci sopra).
# =============================================================================
def check_run_notifications(state: dict) -> None:
    """Notifica GLOBALE (tutti i chat_id) quando un run passa a uno stato finale."""
    runs = devin_get("/api/runs", timeout=10)
    if runs is None:
        return

    seen = state["seen_run_status"]
    final_statuses = {"success", "failed", "timeout", "stopped"}
    current_run_ids = set()

    for r in runs:
        run_id = r["run_id"]
        status = r["status"]
        current_run_ids.add(run_id)
        prev_status = seen.get(run_id)

        if status in final_statuses and prev_status != status:
            icon = {"success": "✅", "failed": "❌", "timeout": "⏱️", "stopped": "🛑"}.get(status, "❔")
            send_to_all_allowed(f"{icon} Run {run_id}: {status}")

        seen[run_id] = status

    # Pota gli id non piu' tra gli ultimi 50 (evita crescita illimitata)
    state["seen_run_status"] = {k: v for k, v in seen.items() if k in current_run_ids}


def check_silence(state: dict) -> None:
    """Per ogni chat con un progetto a fuoco: se l'ultimo turno e' dell'assistente
    e sono passate piu' di SILENCE_HOURS, un solo ping (non ripetuto ad ogni poll)."""
    now = datetime.now(timezone.utc)
    for chat_id, project_path in state["focus"].items():
        data = devin_get(f"/api/chat/history?project_path={urllib.parse.quote(project_path, safe='')}", timeout=10)
        if not data or not data.get("history") or not data.get("updated_at"):
            continue

        history = data["history"]
        updated_at_str = data["updated_at"]
        if history[-1]["role"] != "assistant":
            continue  # l'utente ha gia' risposto per ultimo, nessun ping

        if state["notified_silence"].get(chat_id) == updated_at_str:
            continue  # gia' notificato per questo preciso aggiornamento

        try:
            updated_at = datetime.fromisoformat(updated_at_str)
            if updated_at.tzinfo is None:
                updated_at = updated_at.replace(tzinfo=timezone.utc)
        except Exception:
            continue

        hours_silent = (now - updated_at).total_seconds() / 3600
        if hours_silent >= SILENCE_HOURS:
            send_message(chat_id, f"⏳ Nessuna risposta da {int(hours_silent)}h su {project_path}. "
                                   f"Il modello aspetta un tuo input (o /fixit se vuoi che riprovi da solo).")
            state["notified_silence"][chat_id] = updated_at_str


# =============================================================================
# Main loop — long-poll Telegram con timeout breve, cosi' i task periodici
# (notifiche run, check silenzio) girano regolarmente nello stesso ciclo,
# senza bisogno di thread separati.
# =============================================================================
def main():
    logger.info("DEVIN Bot avviato.")
    state = load_state()
    poll_timeout = min(20, POLL_INTERVAL_SECONDS)

    while True:
        try:
            url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates?offset={state['offset']}&limit=5&timeout={poll_timeout}"
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=poll_timeout + 5) as response:
                data = json.loads(response.read().decode("utf-8"))

            if not data.get("ok"):
                logger.error(f"API error: {data}")
                time.sleep(5)
            else:
                for update in data.get("result", []):
                    state["offset"] = update["update_id"] + 1
                    save_state(state)

                    if "message" not in update:
                        continue
                    msg = update["message"]
                    chat_id = str(msg["chat"]["id"])
                    text = msg.get("text", "").strip()
                    username = msg["from"].get("username", "unknown")

                    if not is_authorized(chat_id):
                        logger.warning(f"Comando rifiutato da chat_id non autorizzato {chat_id} (@{username}): {text}")
                        send_message(chat_id, "⛔ Non autorizzato.")
                        continue
                    if not text:
                        continue

                    logger.info(f"Comando da @{username} ({chat_id}): {text}")
                    try:
                        reply = handle_command(text, chat_id, state)
                    except Exception as e:
                        logger.exception("Errore gestendo il comando")
                        reply = f"❌ Errore interno: {e}"
                    if reply:
                        send_message(chat_id, reply)

        except Exception as e:
            logger.error(f"Errore nel loop Telegram: {e}")
            time.sleep(5)

        # Task periodici, folded nello stesso loop
        now_ts = time.time()
        if now_ts - state.get("last_periodic_check", 0) >= POLL_INTERVAL_SECONDS:
            try:
                check_run_notifications(state)
                check_silence(state)
            except Exception as e:
                logger.error(f"Errore nei task periodici: {e}")
            state["last_periodic_check"] = now_ts
            save_state(state)


if __name__ == "__main__":
    main()
