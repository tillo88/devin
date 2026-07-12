import sys
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

# Carica .env dalla cartella di QUESTO file (devin/ui/.env), non dalla CWD del
# processo — stesso principio del fix su CONFIG_PATH qui sotto. Se il file non
# esiste, load_dotenv() non fa nulla (nessun errore): restano valide le vere
# variabili d'ambiente di sistema, se qualcuno le usa invece del .env.
from dotenv import load_dotenv
_ENV_PATH = Path(__file__).resolve().parent / ".env"
load_dotenv(_ENV_PATH)

print(f"[STARTUP] .env: {'trovato in ' + str(_ENV_PATH) if _ENV_PATH.exists() else 'NON trovato in ' + str(_ENV_PATH)}")
print(f"[STARTUP] TINYFISH_API_KEY: {'presente (' + os.environ['TINYFISH_API_KEY'][:8] + '...)' if os.getenv('TINYFISH_API_KEY') else 'ASSENTE — la web search TinyFish fallira con questo messaggio esatto'}")
# Bump manuale ad ogni consegna: se dopo un riavvio NON vedi questa riga con la
# build attesa, stai eseguendo una copia vecchia del file (cartella sbagliata?).
print("[STARTUP] build fast_app: 2026-07-10c (Progetti+debug_context+picker)")

# FIX: path assoluto, non piu' relativo alla CWD del processo (era la causa diretta
# di "[FATAL] [Errno 2] No such file or directory: 'config/settings.json'" quando
# il server veniva avviato da una directory diversa dalla root del progetto).
CONFIG_PATH = str(ROOT / "config" / "settings.json")

import json
import asyncio
import time
import threading
import base64
import webbrowser
import subprocess
from datetime import datetime
from typing import Optional, Dict, Any

from fastapi import FastAPI, Request, UploadFile, File, Form
from fastapi.responses import StreamingResponse, HTMLResponse, JSONResponse, FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from devin.core.orchestrator import Orchestrator, LOG_DIR
from devin.core.chat_persistence import ChatPersistence
from devin.core.project_space import ProjectSpace
from devin.ai.automem_client import AutoMemClient, project_tags
from devin.ai.client import AIClient
from devin.ai.local_model_launcher import LocalModelLauncher
from devin.ai.autocomplete import Autocomplete
from devin.ai.web_search import get_web_search_provider, format_results_as_context, fetch_top_results
from devin.ai.document_extract import extract_text as extract_document_text

app = FastAPI(title="DEVIN AI IDE")

# FIX: base.html (usato da history.html) referenzia url_for('static', ...) per
# css/js (devin/ui/static/css/style.css e js/app.js, gia' presenti e completi
# sul disco), ma non esisteva nessun app.mount("/static", ...) registrato ->
# ad ogni apertura di /history: 500 Internal Server Error
# (starlette.routing.NoMatchFound: No route exists for name "static").
_STATIC_DIR = ROOT / "devin" / "ui" / "static"
app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

# FIX: Disabilita cache Jinja2
from jinja2 import FileSystemLoader, Environment
jinja_env = Environment(
    loader=FileSystemLoader(str(ROOT / "devin/ui/templates")),
    auto_reload=True,
    cache_size=0
)
templates = Jinja2Templates(env=jinja_env)

# === RUNTIME STATE ===
active_runs = {}
runs_lock = threading.Lock()
_model_launcher = None
_ai_client = None
_autocomplete = None


def _get_launcher():
    global _model_launcher
    if _model_launcher is None:
        try:
            _model_launcher = LocalModelLauncher.from_config(CONFIG_PATH)
        except Exception as e:
            print(f"[WARN] Could not init launcher: {e}")
    return _model_launcher


def _get_ai_client():
    global _ai_client
    if _ai_client is None:
        _ai_client = AIClient()
    return _ai_client


def _get_autocomplete():
    # FIX (bug 1.2 report): riusa il client/istanza singleton invece di ricrearla
    # ad ogni keystroke-trigger (ogni AIClient() nuovo fa 2x health-check + WOL).
    global _autocomplete
    if _autocomplete is None:
        _autocomplete = Autocomplete(ai_client=_get_ai_client())
    return _autocomplete


def _get_vram_info():
    """Ritorna VRAM info da nvidia-smi se disponibile."""
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total,memory.used,memory.free",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5
        )
        lines = [l.strip() for l in result.stdout.strip().split("\n") if l.strip()]
        if lines:
            parts = [p.strip() for p in lines[0].split(",")]
            return {
                "gpu_name": parts[0],
                "total_mb": int(float(parts[1])),
                "used_mb": int(float(parts[2])),
                "free_mb": int(float(parts[3]))
            }
    except Exception:
        pass
    return None


def _detect_mode(message: str) -> str:
    """Rileva se la domanda richiede reasoning o coding."""
    msg_lower = message.lower()
    coding_keywords = [
        "code", "codice", "python", "function", "def ", "class ",
        "bug", "fix", "patch", "diff", "write a", "scrivi", "implementa",
        "crea una funzione", "crea una classe", "refactor", "debug",
        "syntax", "import ", "error", "exception", "traceback",
        "javascript", "html", "css", "sql", "api", "json", "xml",
        "loop", "array", "dict", "list", "tuple", "async", "await"
    ]
    reasoning_keywords = [
        "explain", "spiega", "why", "perche", "how does", "come funziona",
        "architecture", "design", "pattern", "best practice", "approccio",
        "strategia", "piano", "analizza", "compare", "confronta",
        "philosophy", "concept", "theory", "principle"
    ]

    coding_score = sum(1 for k in coding_keywords if k in msg_lower)
    reasoning_score = sum(1 for k in reasoning_keywords if k in msg_lower)

    if coding_score > reasoning_score:
        return "coder"
    elif reasoning_score > coding_score:
        return "reasoning"

    return "coder"


# Chiave riservata per la persistenza della chat "generale" (nessun project_path
# scelto dall'utente). Prima d'ora, in questo caso la history non veniva mai
# salvata server-side: tornando da Dashboard/History a Chat, la conversazione
# spariva (era solo nella variabile JS della pagina precedente, azzerata dal
# reload). Usata SOLO per la persistenza qui sotto — non cambia in nulla il
# routing verso lo Zero-Shot Scaffolding, che continua a girare sul
# project_path originale mandato dal client (vuoto = mai scaffolding, invariato).
# FIX (2026-07-09, modalita' Progetti): path ASSOLUTO ancorato a ROOT — prima era
# relativo alla CWD del processo (sfuggito al giro dei fix sui path assoluti):
# lanciando il server da devin/ui/ la chat generale finiva in
# devin/ui/workspace/_general_chat invece che in <root>/workspace/_general_chat.
GENERAL_CHAT_PROJECT_KEY = str(ROOT / "workspace" / "_general_chat")

_automem_client = None


def _get_automem():
    global _automem_client
    if _automem_client is None:
        _automem_client = AutoMemClient(_get_ai_client().config)
    return _automem_client


def _is_scaffold_request(message: str, project_path: str) -> bool:
    """
    Euristica per il routing chat -> scaffolding (Regola Chat First):
    se il project_path non esiste o e' vuoto (nessun file), e il messaggio ha un
    verbo di creazione, instradiamo verso Zero-Shot Scaffolding invece della chat normale.
    """
    if not project_path:
        return False

    path = Path(project_path).expanduser()
    is_empty_or_missing = (not path.exists()) or (path.is_dir() and not any(path.rglob("*.py")))

    scaffold_verbs = [
        "crea un progetto", "crea una app", "crea un'app", "scaffold", "build a project",
        "create a project", "genera un progetto", "starter", "boilerplate", "da zero"
    ]
    msg_lower = message.lower()
    has_scaffold_intent = any(v in msg_lower for v in scaffold_verbs)

    return is_empty_or_missing and has_scaffold_intent


_RETRY_PHRASES = {"riprova", "riprova adesso", "riprova ora", "ritenta", "prova ancora",
                  "prova di nuovo", "di nuovo", "ancora", "retry", "try again", "riprova pure"}

# Segnali chiari di "mi serve il web": se il toggle 🌐 e' spento ma il messaggio
# contiene uno di questi, la ricerca si attiva DA SOLA (2026-07-10 — l'utente
# chiedeva il meteo col toggle spento e il modello, senza dati, si inventava
# finti curl). Lista volutamente conservativa: meglio un falso negativo che
# ricerche a sorpresa su ogni messaggio.
_WEB_INTENT_PHRASES = [
    "cerca su internet", "cercare su internet", "cerca sul web", "cerca online",
    "su internet", "sul web", "guarda online", "che tempo fa", "che tempo farà",
    "meteo", "previsioni", "notizie", "ultime news", "risultati di oggi",
    "risultati dei", "quanto costa", "prezzo attuale", "quotazione", "classifica di",
    # Focus coding (2026-07-10 — DEVIN non e' la chat generica, quello e' Hermes):
    "documentazione di", "docs di", "documentazione ufficiale", "ultima versione di",
    "versione attuale di", "changelog", "breaking changes", "come si installa",
    "come si usa la libreria", "esempi di utilizzo di", "api di", "release notes",
]


def _wants_web_search(message: str) -> bool:
    msg = message.lower()
    return any(p in msg for p in _WEB_INTENT_PHRASES)


# Messaggi banali (saluti/ack): anche col web search acceso di default NON ha
# senso cercarli sul web — solo latenza sprecata e rumore nel contesto del
# modello. Lista volutamente stretta (match esatto): meglio cercare di più che
# saltare per errore una domanda vera.
_TRIVIAL_MESSAGES = {
    "ciao", "salve", "ehi", "hey", "hello", "buongiorno", "buonasera", "buonanotte",
    "grazie", "grazie mille", "ok", "okay", "perfetto", "va bene", "bene",
    "come stai", "come va", "test", "prova", "ci sei",
}


def _is_trivial_message(message: str) -> bool:
    m = message.strip().lower().rstrip("!?. ")
    return len(m) <= 2 or m in _TRIVIAL_MESSAGES


def _build_search_query(message: str, history: list) -> str:
    """Query di ricerca CONTESTUALE (2026-07-10): se il messaggio e' un follow-up
    corto/generico ("riprova adesso"), cerca l'argomento vero nell'ultimo
    messaggio utente sostanziale della conversazione — altrimenti si finirebbe
    a cercare letteralmente "riprova adesso" sul web."""
    msg = message.strip()
    normalized = msg.lower().strip("!?.,; ")
    meaningful_words = [w for w in normalized.split() if len(w) > 2]
    is_generic = normalized in _RETRY_PHRASES or len(meaningful_words) < 3
    if not is_generic:
        return msg
    for m in reversed(history or []):
        if m.get("role") != "user":
            continue
        prev = (m.get("content") or "").strip()
        prev_norm = prev.lower().strip("!?.,; ")
        if prev and prev_norm not in _RETRY_PHRASES and len(prev.split()) >= 3:
            # cap: nei messaggi storici possono esserci allegati interi
            return prev[:300]
    return msg


def _get_model_detail(alias: str, info: dict) -> dict:
    """Costruisce dettaglio completo di un modello.

    FONTE DI VERITA': local_model_launcher.MODELS (li' vive la logica reale di
    selezione file/fallback/jinja/mmproj). settings.json e' usato SOLO per la
    descrizione testuale — prima invece il nome file veniva letto da settings.json,
    che poteva divergere da cio' che e' davvero in esecuzione (es. mostrava il
    vecchio 'qwen coder' mentre girava Ornith).
    """
    client = _get_ai_client()
    config = client.config.get("models", {})
    local_models = config.get("local_models", {})
    config_key = "reasoning" if alias in ("planner", "reasoning") else "coder"
    model_cfg = local_models.get(config_key, {})

    detail = {
        "alias": alias,
        "port": info.get("port", "N/A"),
        "status": info.get("status", "unknown"),
        "online": info.get("status") == "running",
        "description": model_cfg.get("description", ""),
        "ctx_size": model_cfg.get("ctx_size", "N/A"),
        "is_fallback_active": False,
        "vision_enabled": False,
    }

    # Leggi il file REALE dal launcher (non da settings.json)
    try:
        from devin.ai import local_model_launcher as lml
        launcher_cfg = lml.MODELS.get(alias, {})
        real_file = launcher_cfg.get("file")
        if real_file is not None:
            detail["file"] = real_file.name
            detail["active_file"] = real_file.name
            detail["ctx_size"] = launcher_cfg.get("ctx", detail["ctx_size"])
            # Vision reale = mmproj presente sul disco per questo alias
            mmproj = launcher_cfg.get("mmproj")
            detail["vision_enabled"] = bool(mmproj and mmproj.exists())
            if detail["vision_enabled"]:
                detail["vision_mmproj"] = mmproj.name
            detail["jinja"] = bool(launcher_cfg.get("jinja"))
            # "Fallback attivo" = il file scelto NON e' il primario preferito.
            # Per il coder: primario = Ornith; per il planner: primario = MoE.
            if alias == "coder":
                detail["is_fallback_active"] = (real_file != lml.CODER_ORNITH)
            elif alias == "planner":
                detail["is_fallback_active"] = (real_file != lml.PLANNER_MOE)
    except Exception as e:
        # Fallback alla vecchia lettura da settings.json se il launcher non e' importabile
        detail["file"] = model_cfg.get("file", "unknown")
        detail["active_file"] = detail["file"]
        print(f"[_get_model_detail] warning: impossibile leggere dal launcher ({e})")

    return detail


def _scan_project_files(project_path: str, max_files: int = 2000, max_walk: int = 50000) -> list:
    """Scansiona i file di un progetto per il file explorer.

    #15 audit: prima faceva sorted(path.rglob('*')) materializzando l'INTERO
    albero (una cartella con venv/node_modules a mano puo' avere 100k+ voci →
    secondi di blocco e MB di JSON). Ora: iterazione senza sort anticipato, tetto
    duro sull'attraversamento (max_walk) e cap sui file restituiti (max_files),
    sort solo sul risultato gia' limitato. Chiamata via asyncio.to_thread dagli
    endpoint async (non blocca l'event loop)."""
    path = Path(project_path).expanduser()
    if not path.exists() or not path.is_dir():
        return []

    files = []
    walked = 0
    try:
        for item in path.rglob("*"):
            walked += 1
            if walked > max_walk:
                print(f"[Explorer] cap attraversamento ({max_walk}) raggiunto in {path}")
                break
            if not item.is_file():
                continue
            if any(p.startswith(".") or p in ("__pycache__", "venv", ".venv", "node_modules") for p in item.parts):
                continue
            try:
                st = item.stat()
            except OSError:
                continue
            rel = item.relative_to(path)
            files.append({
                "name": item.name,
                "path": str(rel),
                "full_path": str(item),
                "size": st.st_size,
                "mtime": datetime.fromtimestamp(st.st_mtime).isoformat(),
                "is_python": item.suffix == ".py",
                "is_text": item.suffix in (".py", ".json", ".yaml", ".yml", ".txt", ".md", ".sh", ".bat")
            })
            if len(files) >= max_files:
                print(f"[Explorer] cap file ({max_files}) raggiunto in {path}")
                break
    except Exception as e:
        print(f"[Explorer] Error scanning {path}: {e}")

    files.sort(key=lambda f: f["path"])
    return files


def _read_file_content(file_path: str, max_chars: int = 10000) -> str:
    """Legge il contenuto di un file di testo."""
    path = Path(file_path).expanduser()
    if not path.exists() or not path.is_file():
        return ""

    try:
        content = path.read_text(encoding="utf-8", errors="ignore")
        if len(content) > max_chars:
            content = content[:max_chars] + "\n\n# [...file truncated...]"
        return content
    except Exception:
        return "# [Error reading file]"


# ============================================================
# PAGES
# ============================================================

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Dashboard IDE principale."""
    client = _get_ai_client()
    health = client.health()
    launcher = _get_launcher()
    models_running = False
    models_info = []

    if launcher:
        status = launcher.get_status()
        models_running = bool(status.local_running)
        for alias, info in status.local_running.items():
            models_info.append(_get_model_detail(alias, info))

    # Lista progetti disponibili in workspace/
    workspace_path = ROOT / "workspace"
    projects = []
    if workspace_path.exists():
        for item in sorted(workspace_path.iterdir()):
            if item.is_dir():
                projects.append({
                    "name": item.name,
                    "path": str(item),
                    "has_python": any(item.rglob("*.py"))
                })

    # Run recenti
    recent_runs = []
    if LOG_DIR.exists():
        for f in sorted(LOG_DIR.glob("run_*.log"), reverse=True)[:10]:
            stat = f.stat()
            content = f.read_text(encoding="utf-8", errors="ignore")
            run_status = "unknown"
            if "status: success" in content.lower():
                run_status = "success"
            elif "status: failed" in content.lower():
                run_status = "failed"
            elif "status: timeout" in content.lower():
                run_status = "timeout"
            elif "status: stopped" in content.lower():
                run_status = "stopped"

            recent_runs.append({
                "run_id": f.stem,
                "status": run_status,
                "size": f.stat().st_size,
                "mtime": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                "preview": content[:200]
            })

    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "health": health,
            "models_running": models_running,
            "models_info": models_info,
            "vram": _get_vram_info(),
            "projects": projects,
            "recent_runs": recent_runs,
            "active_runs": list(active_runs.keys())
        }
    )


@app.get("/chat", response_class=HTMLResponse)
async def chat_page(request: Request):
    client = _get_ai_client()
    launcher = _get_launcher()

    models_chat_info = {}
    if launcher:
        status = launcher.get_status()
        for alias, info in status.local_running.items():
            models_chat_info[alias] = _get_model_detail(alias, info)

    return templates.TemplateResponse(
        request=request,
        name="chat.html",
        context={
            "models_info": models_chat_info,
            "vram": _get_vram_info()
        }
    )


@app.get("/history", response_class=HTMLResponse)
async def history_page(request: Request):
    runs = []
    if LOG_DIR.exists():
        for f in sorted(LOG_DIR.glob("run_*.log"), reverse=True):
            stat = f.stat()
            content = f.read_text(encoding="utf-8", errors="ignore")
            status = "unknown"
            if "status: success" in content.lower():
                status = "success"
            elif "status: failed" in content.lower():
                status = "failed"
            elif "status: timeout" in content.lower():
                status = "timeout"
            elif "status: stopped" in content.lower():
                status = "stopped"
            runs.append({
                "run_id": f.stem,
                "file": str(f.name),
                "size": f.stat().st_size,
                "mtime": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                "status": status,
                "preview": content[:500]
            })
    return templates.TemplateResponse(
        request=request,
        name="history.html",
        context={"runs": runs[:50]}
    )


# ============================================================
# API - FILE EXPLORER
# ============================================================

@app.get("/api/explore")
async def api_explore(path: str = ""):
    """Esplora file di un progetto."""
    if not path:
        return {"error": "missing path"}

    safe = _safe_under_allowed(path)
    if safe is None:
        return {"error": "path non consentito: solo progetti in workspace/ o cartelle collegate dal picker"}

    # #10/#15: scansione in thread — su alberi grandi bloccava l'event loop
    files = await asyncio.to_thread(_scan_project_files, str(safe))
    return {
        "path": path,
        "files": files,
        "count": len(files)
    }


@app.get("/api/file")
async def api_file(path: str = ""):
    """Legge contenuto di un file."""
    if not path:
        return {"error": "missing path"}

    safe = _safe_under_allowed(path)
    if safe is None:
        return {"error": "path non consentito: solo progetti in workspace/ o cartelle collegate dal picker"}

    content = _read_file_content(str(safe))
    return {
        "path": path,
        "content": content,
        "language": Path(path).suffix.lstrip(".") or "text"
    }


@app.post("/api/file/save")
async def api_file_save(request: Request):
    """#26 audit: salvataggio REALE dall'editor Monaco (prima il bottone 💾 era
    un alert 'non implementato'). Scrittura ATOMICA (temp + replace) con backup
    .bak della versione precedente. Path validato dalla stessa guardia di #8:
    solo dentro workspace/ o cartelle collegate dal picker."""
    data = await request.json()
    safe = _safe_under_allowed(data.get("path", ""))
    if safe is None:
        return {"error": "path non consentito: solo file in workspace/ o cartelle collegate"}
    content = data.get("content", "")

    def _write():
        if safe.exists():
            try:
                safe.with_suffix(safe.suffix + ".bak").write_bytes(safe.read_bytes())
            except Exception:
                pass  # il backup è best-effort, non deve bloccare il salvataggio
        tmp = safe.with_suffix(safe.suffix + ".tmp")
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(safe)  # atomico: nessun file mezzo-scritto se qualcosa va storto

    try:
        await asyncio.to_thread(_write)
        return {"status": "saved", "path": str(safe), "bytes": len(content.encode("utf-8"))}
    except Exception as e:
        return {"error": f"salvataggio fallito: {e}"}


# ============================================================
# API - MODELS INFO
# ============================================================

@app.get("/api/health")
async def api_health():
    """Health check rig/locale per il polling badge in dashboard (index.html)."""
    return _get_ai_client().health()


@app.get("/api/models/info")
async def api_models_info():
    launcher = _get_launcher()
    if not launcher:
        return {"running": False, "models": [], "vram": _get_vram_info()}

    status = launcher.get_status()
    models = []
    for alias, info in status.local_running.items():
        models.append(_get_model_detail(alias, info))

    return {
        "running": bool(status.local_running),
        "models": models,
        "vram": _get_vram_info(),
        "source": status.model_source
    }


# ============================================================
# API - CHAT (SSE VELOCE) + VISION + WEB SEARCH + SCAFFOLD ROUTING
# ============================================================

class ChatRequest(BaseModel):
    message: str
    mode: str = "auto"
    image_base64: Optional[str] = None
    project_path: Optional[str] = None
    use_web_search: bool = False
    history: Optional[list] = None  # [{"role": "user"/"assistant", "content": "..."}], gestito dal frontend
    chat_id: Optional[str] = None   # modalita' Progetti: conversazione specifica (.devin/chats/<id>.json)


@app.post("/api/chat")
async def api_chat(req: ChatRequest):
    message = req.message.strip()
    if not message:
        return {"error": "empty message"}

    # Regola "Chat First": se sembra una richiesta di scaffolding su workspace vuoto,
    # instrada verso lo Zero-Shot Scaffolding invece della chat normale.
    if req.project_path and _is_scaffold_request(message, req.project_path):
        return await api_chat_scaffold(RunRequest(path=req.project_path, task=message))

    # Vision rimosso da DEVIN (2026-07-09): nessun modello locale/rig di questo
    # progetto ha piu' --mmproj caricato. Un'immagine qui non verrebbe letta dal
    # modello (che risponderebbe a caso ignorandola) - meglio un errore chiaro
    # che una risposta silenziosamente sbagliata. Per immagini: usa Hermes.
    if req.image_base64:
        return {"error": "Vision non disponibile su DEVIN AI IDE — usa Hermes (ruolo dedicato del rig) per immagini."}

    selected_mode = _detect_mode(message) if req.mode == "auto" else req.mode

    launcher = _get_launcher()
    if launcher:
        # #10: ensure_models può avviare/attendere i server modelli (blocking) → thread
        await asyncio.to_thread(launcher.ensure_models)

    ai = _get_ai_client()

    # Persistenza caricata PRIMA del blocco web-search: serve a _build_search_query
    # per ricavare l'argomento della conversazione sui follow-up corti ("riprova").
    persistence_key = req.project_path or GENERAL_CHAT_PROJECT_KEY
    chat_persistence = ChatPersistence(persistence_key, chat_id=req.chat_id)
    persisted_history = chat_persistence.load()
    chat_persistence.append("user", message)  # subito, sopravvive anche a crash mid-stream

    # Auto-attivazione ricerca web su intento esplicito (toggle spento ma la
    # domanda chiede chiaramente dati dal web). L'utente viene avvisato via SSE.
    auto_web_enabled = False
    if not req.use_web_search and _wants_web_search(message):
        req.use_web_search = True
        auto_web_enabled = True

    # Web search acceso di default (toggle ON nel frontend): salta comunque la
    # ricerca sui messaggi banali (saluti/ack) — niente latenza né rumore su "ciao".
    if req.use_web_search and _is_trivial_message(message):
        req.use_web_search = False

    content = message
    web_search_error = None
    if req.use_web_search:
        try:
            provider = get_web_search_provider(ai.config)
            # FIX (2026-07-10): la query era il messaggio LETTERALE — con follow-up
            # tipo "riprova adesso" cercava... "riprova adesso" (risultati: Reverso,
            # gruppi Facebook di linguistica). Se il messaggio e' corto/generico,
            # la query viene costruita dall'ultimo messaggio utente sostanziale
            # della conversazione (l'argomento vero), + il messaggio corrente.
            search_query = _build_search_query(message, persisted_history)
            # #10: la ricerca web (rete) è bloccante → thread
            results = await asyncio.to_thread(provider.search, search_query, max_results=5)
            web_context = format_results_as_context(results)
            # Fetch del CONTENUTO dei top risultati (2026-07-10): senza, il modello
            # ha solo titolo+snippet e improvvisa (caso skysport/mondiali). Fail-soft:
            # pagine bloccate/JS-only vengono saltate, restano gli snippet.
            ws_cfg = ai.config.get("web_search", {})
            if ws_cfg.get("fetch_pages", True):
                # #10: fetch pagine (rete + eventuale Chromium) bloccante → thread
                page_content = await asyncio.to_thread(
                    fetch_top_results,
                    results,
                    max_pages=ws_cfg.get("fetch_max_pages", 2),
                    max_chars_per_page=ws_cfg.get("fetch_chars_per_page", 2500),
                    engine=ws_cfg.get("fetch_engine", "requests"))
                if page_content:
                    web_context = f"{web_context}\n\n{page_content}"
                else:
                    # Niente contenuto pagine (siti bloccati/timeout): dillo al
                    # modello, altrimenti riempie i buchi INVENTANDO risultati
                    # dettagliati (visto coi mondiali: partite mai giocate).
                    web_context += ("\n\n[NOTA: il contenuto delle pagine non era accessibile — "
                                    "hai SOLO i titoli/snippet qui sopra. Rispondi solo con cio' "
                                    "che gli snippet dicono davvero e DICHIARA che i dettagli "
                                    "non sono verificabili. NON inventare risultati, numeri o nomi.]")
            # Cap difensivo (2026-07-11): sul fallback locale il modello ha una
            # finestra piccola; un web_context troppo lungo faceva sforare il
            # contesto → HTTP 400 dal server. Limitiamo il totale iniettato.
            web_ctx_cap = ws_cfg.get("max_context_chars", 5000)
            if len(web_context) > web_ctx_cap:
                web_context = web_context[:web_ctx_cap] + "\n[...risultati web troncati per stare nel contesto...]"
            content = f"Risultati ricerca web:\n{web_context}\n\nDomanda utente: {message}"
        except Exception as e:
            # FIX: prima l'errore finiva "[Web search non disponibile: {e}]" DENTRO
            # il content mandato al modello — che lo ignorava e rispondeva a caso,
            # lasciando l'utente senza alcun segnale visibile del perche'. Ora e'
            # tracciato a parte e mandato come evento SSE distinto (vedi sotto),
            # il messaggio al modello resta quello originale (nessun rumore extra).
            web_search_error = str(e)

    # (audit #18, 2026-07-10: rimosso il ramo vision morto — image_base64 viene
    # gia' rifiutato con errore esplicito a inizio funzione, questo blocco era
    # irraggiungibile dal 2026-07-09, quando la vision e' stata tolta da DEVIN.)

    # Persistenza server-side dello storico: caricata piu' sopra (prima del blocco
    # web-search, a cui serve per costruire la query contestuale). Il server resta
    # la fonte di verita' — req.history del client viene ignorato.

    # Storico conversazione + system prompt configurabile (Regola: elastico, non hardcoded).
    # system_prompt vuoto di default -> comportamento chat generica invariato.
    chat_cfg = ai.config.get("chat", {})
    system_prompt = (chat_cfg.get("system_prompt") or "").strip()
    max_history = chat_cfg.get("max_history_messages", 20)

    # ---- Modalita' Progetti: contesto costruito da _build_project_context ----
    # (estratto in una funzione per il debug endpoint /api/project/debug_context;
    # log riassuntivo a ogni messaggio, cosi' i "non ho accesso" del modello si
    # distinguono subito da un contesto davvero mai iniettato).
    system_parts = []
    if system_prompt:
        system_parts.append(system_prompt)
    project_parts, ctx_debug = _build_project_context(message, persistence_key, req.project_path)
    system_parts.extend(project_parts)
    print(f"[ProjectSpace] contesto: {ctx_debug}")

    # Nota di capacita' SEMPRE presente (2026-07-10): senza, il modello si
    # inventava finti `curl`/`bash` in chat spacciandoli per eseguiti. Deve
    # sapere cosa puo' e cosa NON puo' fare in questo contesto.
    if req.use_web_search:
        system_parts.append(
            "CAPACITA': in questa risposta hai a disposizione risultati/contenuti web "
            "reali forniti nel messaggio. NON puoi eseguire comandi (bash/curl): non "
            "fingere output di comandi.")
    else:
        system_parts.append(
            "CAPACITA': in questa conversazione NON hai accesso a internet (interruttore "
            "🌐 spento) e NON puoi eseguire comandi (bash/curl): non fingere di farlo. "
            "Se servono dati dal web, chiedi all'utente di attivare l'interruttore 🌐.")

    # Lingua (fix 2026-07-10: rispondeva in inglese anche con istruzioni in italiano).
    # Nudge leggero e sempre presente, non hardcoda l'italiano: mirror della lingua utente.
    system_parts.append(
        "LINGUA: rispondi SEMPRE nella stessa lingua del messaggio dell'utente "
        "(se ti scrive in italiano, rispondi in italiano).")

    messages = []
    if system_parts:
        messages.append({"role": "system", "content": "\n\n".join(system_parts)})
    if persisted_history:
        # Tronca ai piu' recenti max_history messaggi: protegge da OOM/contesto
        # locale limitato su run prolungate (vincolo hardware locale).
        messages.extend(persisted_history[-max_history:])
    messages.append({"role": "user", "content": content})

    model_name = (
        ai.local_reasoning_model
        if selected_mode == "reasoning"
        else ai.local_coder_model
    )

    config_key = "reasoning" if selected_mode == "reasoning" else "coder"
    model_cfg = ai.config.get("models", {}).get("local_models", {}).get(config_key, {})
    model_detail = {
        "name": model_name,
        "file": model_cfg.get("file", ""),
        "description": model_cfg.get("description", ""),
        "ctx_size": model_cfg.get("ctx_size", ""),
        "vision": model_cfg.get("vision", {}).get("enabled", False),
        "web_search_used": req.use_web_search,
    }

    async def generate_sse(model_name: str, model_detail: dict):
        token_count = 0
        start_time = time.time()
        full_response = ""

        if auto_web_enabled:
            yield f"event: info\ndata: {json.dumps({'message': '🌐 Ricerca web attivata automaticamente per questa domanda'})}\n\n"

        if web_search_error:
            yield f"event: warning\ndata: {json.dumps({'message': f'Web search non disponibile: {web_search_error}'})}\n\n"

        yield f"event: meta\ndata: {json.dumps({'mode': selected_mode, 'model': model_name, 'detail': model_detail})}\n\n"

        try:
            for chunk in ai.stream(messages, mode=selected_mode):
                token_count += 1
                full_response += chunk
                yield f"data: {json.dumps({'token': chunk})}\n\n"
                await asyncio.sleep(0)

            elapsed = time.time() - start_time
            # #16 audit: token_count conta i CHUNK SSE. Con llama-server è ~1 token
            # per chunk, quindi tps ≈ token/s (approssimazione onesta, non esatta:
            # un chunk può contenere più token in altri backend).
            tps = round(token_count / elapsed, 1) if elapsed > 0 else 0
            yield f"event: done\ndata: {json.dumps({'tokens': token_count, 'tps': tps, 'elapsed': round(elapsed, 1)})}\n\n"

            if chat_persistence and full_response.strip():
                chat_persistence.append("assistant", full_response)

        except Exception as e:
            yield f"event: error\ndata: {json.dumps({'error': str(e)})}\n\n"

    return StreamingResponse(
        generate_sse(model_name, model_detail),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        }
    )


# #14 audit: limiti di dimensione sugli upload in chat (prima illimitati:
# un file enorme veniva letto tutto in RAM prima di qualunque controllo).
MAX_IMAGE_BYTES = 15 * 1024 * 1024      # 15 MB
MAX_DOCUMENT_BYTES = 25 * 1024 * 1024   # 25 MB


async def _read_upload_limited(upload: UploadFile, max_bytes: int):
    """Legge un UploadFile a chunk abortendo appena supera max_bytes (non carica
    in RAM file oltre il limite). Ritorna (bytes|None, error_msg|None)."""
    chunks = []
    total = 0
    while True:
        chunk = await upload.read(1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            return None, f"file troppo grande (max {max_bytes // (1024 * 1024)} MB)"
        chunks.append(chunk)
    return b"".join(chunks), None


@app.post("/api/chat/vision")
async def api_chat_vision(message: str = Form(""), image: UploadFile = File(None),
                           mode: str = Form("auto"), project_path: str = Form(""),
                           use_web_search: bool = Form(False)):
    image_b64 = None
    if image:
        contents, err = await _read_upload_limited(image, MAX_IMAGE_BYTES)
        if err:
            return {"error": err}
        image_b64 = base64.b64encode(contents).decode("utf-8")

    # FIX: project_path non veniva mai inoltrato qui -> ogni messaggio con
    # immagine allegata saltava la persistenza server-side, anche con un
    # progetto impostato (finiva sempre in ChatPersistence(None) = disattivata).
    req = ChatRequest(message=message, mode=mode, image_base64=image_b64,
                       project_path=project_path or None, use_web_search=use_web_search)
    return await api_chat(req)


@app.post("/api/chat/document")
async def api_chat_document(message: str = Form(""), document: UploadFile = File(None),
                             mode: str = Form("auto"), project_path: str = Form(""),
                             use_web_search: bool = Form(False), chat_id: str = Form("")):
    """Documenti binari comuni (PDF/DOCX/XLSX/PPTX) allegati in chat: estrae il
    testo server-side (devin/ai/document_extract.py, nessuna libreria vision
    coinvolta) e lo inietta nel messaggio con lo stesso pattern gia' usato
    client-side per i file di testo semplice (.txt/.md/.py/...), poi prosegue
    come chat normale — stessa persistenza/web-search/scaffold-routing di /api/chat."""
    content = message
    if document:
        raw, err = await _read_upload_limited(document, MAX_DOCUMENT_BYTES)
        if err:
            return {"error": err}
        # estrazione (PDF/DOCX/XLSX/PPTX) è CPU-bound: in thread per non bloccare l'event loop
        extracted = await asyncio.to_thread(extract_document_text, document.filename, raw)
        content = f"[Allegato: {document.filename}]\n```\n{extracted}\n```\n\n{message}".strip()

    req = ChatRequest(message=content, mode=mode, project_path=project_path or None,
                       use_web_search=use_web_search, chat_id=chat_id or None)
    return await api_chat(req)


@app.post("/api/chat/search")
async def api_chat_search(req: ChatRequest):
    """Endpoint esplicito: forza sempre la ricerca web indipendentemente da euristiche."""
    req.use_web_search = True
    return await api_chat(req)


@app.get("/api/chat/history")
async def api_chat_history_get(project_path: str = "", chat_id: str = ""):
    """Storico persistito per un progetto — il frontend lo chiama al caricamento
    pagina o al cambio di project_path, cosi' la conversazione sopravvive a
    refresh/chiusura del browser. updated_at serve al bot Telegram per il check
    'nessuna risposta da N ore' senza dover tracciare timestamp per-messaggio.
    project_path vuoto -> chat generale, sotto GENERAL_CHAT_PROJECT_KEY (prima
    ritornava sempre vuoto: la chat senza progetto non era mai recuperabile).
    chat_id (modalita' Progetti): conversazione specifica in .devin/chats/."""
    cp = ChatPersistence(project_path or GENERAL_CHAT_PROJECT_KEY, chat_id=chat_id or None)
    return {"history": cp.load(), "updated_at": cp.last_updated()}


@app.post("/api/chat/history/clear")
async def api_chat_history_clear(request: Request):
    """Reset della conversazione persistita per un progetto (bottone 'Nuova conversazione')."""
    data = await request.json()
    project_path = data.get("project_path", "")
    chat_id = data.get("chat_id") or None
    ChatPersistence(project_path or GENERAL_CHAT_PROJECT_KEY, chat_id=chat_id).clear()
    return {"status": "cleared"}


# ============================================================
# API - MODALITA' PROGETTI (istruzioni + knowledge + chat multiple + AutoMem)
# ============================================================

# Cache delle istanze ProjectSpace: il VectorStore dentro puo' tenere caricato
# il modello di embedding (sentence-transformers ~80MB) — ricrearlo ad ogni
# messaggio costerebbe secondi di latenza per niente. Le istanze sono condivise:
# un upload/delete di knowledge resetta l'indice sulla STESSA istanza che la
# chat usa per il retrieval (coerenza automatica).
_project_spaces: Dict[str, ProjectSpace] = {}


def _project_space_for(project_path: str) -> ProjectSpace:
    key = str(Path(project_path or GENERAL_CHAT_PROJECT_KEY).expanduser().resolve())
    if key not in _project_spaces:
        _project_spaces[key] = ProjectSpace(key)
    return _project_spaces[key]


@app.get("/api/project/overview")
async def api_project_overview(project_path: str = ""):
    """Tutto quello che serve alla sidebar in una chiamata sola."""
    ps = _project_space_for(project_path)
    return {
        "project": str(ps.project_path),
        "is_general": not project_path,
        "description": ps.get_description(),
        "instructions": ps.get_instructions(),
        "knowledge": ps.list_knowledge(),
        "pins": ps.list_pins(),
        "files": ps.list_files(max_items=300),
        "chats": ps.list_chats(),
        "automem": _get_automem().status(),
    }


@app.post("/api/project/instructions")
async def api_project_instructions(request: Request):
    data = await request.json()
    ps = _project_space_for(data.get("project_path", ""))
    ps.set_instructions(data.get("instructions", ""))
    return {"status": "saved", "chars": len(ps.get_instructions())}


@app.post("/api/project/description")
async def api_project_description(request: Request):
    """'About' del progetto (scopo/stack): header + contesto persistente."""
    data = await request.json()
    ps = _project_space_for(data.get("project_path", ""))
    ps.set_description(data.get("description", ""))
    return {"status": "saved", "chars": len(ps.get_description())}


@app.post("/api/project/pins/add")
async def api_project_pins_add(request: Request):
    """★ Appunta un file del progetto: sarà SEMPRE nel contesto dell'agente."""
    data = await request.json()
    ps = _project_space_for(data.get("project_path", ""))
    ok = ps.add_pin(data.get("path", ""))
    return {"status": "pinned" if ok else "error",
            "error": None if ok else "file non valido o fuori dal progetto",
            "pins": ps.list_pins()}


@app.post("/api/project/pins/remove")
async def api_project_pins_remove(request: Request):
    data = await request.json()
    ps = _project_space_for(data.get("project_path", ""))
    ps.remove_pin(data.get("path", ""))
    return {"status": "unpinned", "pins": ps.list_pins()}


@app.post("/api/project/knowledge/from_url")
async def api_project_knowledge_from_url(request: Request):
    """Aggiunge alla knowledge il testo estratto da un URL (es. doc di una
    libreria): niente download manuale. Usa fetch_page_text (UA browser +
    estrazione testo stdlib), in thread perché è rete."""
    from devin.ai.web_search import fetch_page_text
    from urllib.parse import urlparse
    data = await request.json()
    ps = _project_space_for(data.get("project_path", ""))
    url = (data.get("url") or "").strip()
    if not url.startswith(("http://", "https://")):
        return {"error": "URL non valido (serve http:// o https://)"}
    try:
        text = await asyncio.to_thread(fetch_page_text, url, 20000, 15)
    except Exception as e:
        return {"error": f"fetch fallito: {e}"}
    if not (text or "").strip():
        return {"error": "nessun testo estratto (pagina JS-only o bloccata?)"}
    parsed = urlparse(url)
    base = (parsed.netloc + parsed.path).strip("/").replace("/", "_") or "pagina"
    fname = base[:60] + ".md"
    header = f"# Fonte: {url}\n\n"
    return ps.add_knowledge(fname, (header + text).encode("utf-8"))


@app.get("/api/project/last_run")
async def api_project_last_run(project_path: str = ""):
    """Stato dell'ultimo run del progetto (badge nel pannello). Legge lo stato
    per-progetto (.devin_state via StatePersistence.load_latest)."""
    from devin.core.state_persistence import StatePersistence
    if not project_path:
        return {"has_run": False}
    pp = str(Path(project_path).expanduser().resolve())
    try:
        st = await asyncio.to_thread(lambda: StatePersistence(pp).load_latest())
    except Exception:
        st = None
    if not st:
        return {"has_run": False}
    return {
        "has_run": True,
        "run_id": st.get("_run_id"),
        "status": st.get("final_status") or "interrotto",   # senza final_status = non concluso
        "final": bool(st.get("final_status")),
        "resumable": not bool(st.get("final_status")),        # riprendibile se non concluso
        "saved_at": st.get("_saved_at"),
        "task": (st.get("task") or "")[:200],
    }


@app.post("/api/project/knowledge/upload")
async def api_project_knowledge_upload(project_path: str = Form(""),
                                        file: UploadFile = File(...)):
    ps = _project_space_for(project_path)
    raw = await file.read()
    result = ps.add_knowledge(file.filename, raw)
    return result


@app.post("/api/project/knowledge/delete")
async def api_project_knowledge_delete(request: Request):
    data = await request.json()
    ps = _project_space_for(data.get("project_path", ""))
    ok = ps.delete_knowledge(data.get("filename", ""))
    return {"status": "deleted" if ok else "not_found"}


@app.post("/api/project/chats/new")
async def api_project_chats_new(request: Request):
    data = await request.json()
    ps = _project_space_for(data.get("project_path", ""))
    chat_id = ps.new_chat(data.get("title", ""))
    return {"chat_id": chat_id}


@app.post("/api/project/chats/rename")
async def api_project_chats_rename(request: Request):
    data = await request.json()
    ps = _project_space_for(data.get("project_path", ""))
    ok = ps.rename_chat(data.get("chat_id", ""), data.get("title", ""))
    return {"status": "renamed" if ok else "not_found"}


@app.post("/api/project/chats/delete")
async def api_project_chats_delete(request: Request):
    data = await request.json()
    ps = _project_space_for(data.get("project_path", ""))
    ok = ps.delete_chat(data.get("chat_id", ""))
    return {"status": "deleted" if ok else "not_found"}


@app.post("/api/project/memory/store")
async def api_project_memory_store(request: Request):
    """Store manuale su AutoMem (bottone 'salva in memoria'). Fail-soft: se il
    rig e' spento torna stored=false con motivo leggibile, mai un 500."""
    data = await request.json()
    project_path = data.get("project_path", "") or GENERAL_CHAT_PROJECT_KEY
    content = data.get("content", "")
    automem = _get_automem()
    if not automem.enabled:
        return {"stored": False, "queued": False, "reason": "AutoMem disabilitato in settings.json"}
    outcome = automem.store(content, tags=project_tags(project_path))
    return {
        "stored": outcome == "stored",
        "queued": outcome == "queued",
        "reason": {
            "stored": None,
            "queued": "rig spento: memoria in coda locale, si sincronizza da sola appena il rig risponde",
            "failed": "salvataggio fallito (nemmeno l'outbox locale e' scrivibile?)",
        }[outcome],
    }


WORKSPACE_DIR = ROOT / "workspace"

# === SICUREZZA (#8 audit) — path traversal su /api/explore e /api/file ===
# Senza guardia, ?path=/etc/passwd (o ?path=../../..) legge file arbitrari del
# sistema. Root consentiti: workspace/ (progetti interni) + le cartelle che
# l'utente COLLEGA esplicitamente dal picker "Sfoglia". resolve() normalizza
# '..' e risolve i symlink, quindi blocca anche i link che puntano fuori.
_ALLOWED_ROOTS = {WORKSPACE_DIR.resolve()}


def _register_allowed_root(path_str: str) -> None:
    """Autorizza una cartella collegata dall'utente (dal picker) come root
    leggibile via /api/explore e /api/file per la durata del processo."""
    try:
        p = Path(path_str).expanduser().resolve()
        if p.is_dir():
            _ALLOWED_ROOTS.add(p)
    except Exception:
        pass


def _safe_under_allowed(path_str: str) -> Optional[Path]:
    """Ritorna il path risolto SOLO se cade sotto un root consentito, altrimenti
    None. Da usare come gate in tutti gli endpoint che leggono file su path
    fornito dal client."""
    if not path_str:
        return None
    try:
        p = Path(path_str).expanduser().resolve()
    except Exception:
        return None
    for root in _ALLOWED_ROOTS:
        if p == root or root in p.parents:
            return p
    return None


@app.get("/favicon.ico")
async def favicon():
    # Toglie il 404 ricorrente nei log — nessuna icona, risposta vuota.
    from fastapi import Response
    return Response(status_code=204)


def _pick_folder_windows() -> dict:
    """Apre il dialog nativo di Windows "Sfoglia cartelle" (FolderBrowserDialog)
    via powershell.exe — possibile perche' il server gira in WSL sulla STESSA
    macchina del browser. Il path Windows scelto viene convertito in path WSL
    con wslpath. Bloccante finche' l'utente non chiude il dialog (chiamare via
    asyncio.to_thread). Se non siamo su WSL (es. deploy sul rig), errore pulito."""
    import shutil as _shutil
    import subprocess as _sp

    ps = _shutil.which("powershell.exe") or "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
    if not Path(ps).exists():
        return {"error": "Dialog disponibile solo quando il server gira in WSL sulla stessa macchina "
                          "(powershell.exe non trovato). Inserisci il path a mano."}
    script = (
        "Add-Type -AssemblyName System.Windows.Forms; "
        "$f = New-Object System.Windows.Forms.FolderBrowserDialog; "
        "$f.Description = 'Seleziona la cartella del progetto per DEVIN AI IDE'; "
        "$f.ShowNewFolderButton = $true; "
        "if ($f.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) { Write-Output $f.SelectedPath }"
    )
    try:
        # -STA: i dialog WinForms richiedono un thread STA
        out = _sp.run([ps, "-NoProfile", "-STA", "-Command", script],
                      capture_output=True, text=True, timeout=180)
        win_path = (out.stdout or "").strip().splitlines()[-1].strip() if (out.stdout or "").strip() else ""
        if not win_path:
            return {"cancelled": True}
        wsl = _sp.run(["wslpath", "-u", win_path], capture_output=True, text=True, timeout=10)
        linux_path = (wsl.stdout or "").strip()
        if not linux_path:
            return {"error": f"Conversione path fallita per: {win_path}"}
        return {"path": linux_path, "windows_path": win_path}
    except _sp.TimeoutExpired:
        return {"error": "Dialog scaduto (3 minuti senza scelta)."}
    except Exception as e:
        return {"error": f"Dialog fallito: {e}"}


@app.post("/api/workspace/pick_folder")
async def api_workspace_pick_folder():
    """Apre il dialog cartelle di Windows e ritorna il path WSL scelto.
    In thread separato: il dialog resta aperto anche minuti, non deve
    bloccare l'event loop (l'intera UI si congelerebbe)."""
    result = await asyncio.to_thread(_pick_folder_windows)
    # #8: una cartella scelta dall'utente diventa root leggibile via file explorer
    if isinstance(result, dict) and result.get("path"):
        _register_allowed_root(result["path"])
    return result


@app.get("/api/workspace/projects")
async def api_workspace_projects():
    """Lista dei progetti = sottocartelle di workspace/ (escluse quelle interne).
    Per la sidebar 'Progetti come cartelle' + per il rilevamento dei progetti
    collegati nei messaggi (vedi _detect_linked_projects)."""
    projects = []
    if WORKSPACE_DIR.exists():
        for d in sorted(WORKSPACE_DIR.iterdir()):
            if not d.is_dir() or d.name.startswith(("_", ".")) or d.name == "sandbox":
                continue
            ps = ProjectSpace(str(d))
            projects.append({
                "name": d.name,
                "path": str(d),
                "chats": len(ps.list_chats()),
                "knowledge": len(ps.list_knowledge()),
                "has_instructions": bool(ps.get_instructions()),
            })
    return {"projects": projects, "workspace": str(WORKSPACE_DIR)}


@app.post("/api/workspace/projects/new")
async def api_workspace_projects_new(request: Request):
    """Crea una nuova cartella-progetto in workspace/. Nome sanitizzato, niente
    path traversal; se esiste gia' torna quella esistente (idempotente)."""
    import re as _re
    data = await request.json()
    name = _re.sub(r"[^\w\-. ]", "_", Path(data.get("name", "")).name).strip()
    if not name:
        return {"error": "nome progetto vuoto o non valido"}
    target = WORKSPACE_DIR / name
    target.mkdir(parents=True, exist_ok=True)
    return {"name": name, "path": str(target), "created": True}


def _build_project_context(message: str, persistence_key: str,
                            req_project_path: Optional[str]) -> tuple:
    """Costruisce i blocchi di contesto della modalita' Progetti (istruzioni,
    knowledge, file, progetti collegati, AutoMem). Ritorna (parts, debug_str).
    Fail-soft: qualsiasi errore degrada a lista vuota, mai eccezioni.
    Usata da api_chat E da /api/project/debug_context (stessa identica logica:
    quello che vedi nel debug e' quello che riceve il modello)."""
    parts = []
    dbg = {"instructions": 0, "description": 0, "pinned": 0, "knowledge": 0, "files": 0, "linked": [], "automem": 0, "errors": []}
    ai = _get_ai_client()
    try:
        ps = _project_space_for(persistence_key)
        ps_cfg = ai.config.get("project_space", {})

        description = ps.get_description()
        if description:
            parts.append(f"DESCRIZIONE DEL PROGETTO (scopo/stack):\n{description}")
            dbg["description"] = len(description)

        instructions = ps.get_instructions()
        if instructions:
            parts.append(f"ISTRUZIONI DEL PROGETTO:\n{instructions}")
            dbg["instructions"] = len(instructions)

        # FILE PINNATI (★): SEMPRE nel contesto, contenuto attuale (troncato). A
        # differenza della knowledge (retrieval per rilevanza) questi ci sono
        # SEMPRE — servono a non far "dimenticare" al modello com'è fatto un file
        # chiave (modulo principale, spec, contratto API). Esattamente il buco
        # visto nel run Pint (self.display_text allucinato).
        try:
            pinned = ps.read_pinned(max_chars_per_file=ps_cfg.get("pin_max_chars", 4000))
            if pinned:
                blocks = [f"### {p['path']}\n{p['content']}" for p in pinned]
                parts.append("FILE PINNATI (★ sempre nel contesto, contenuto attuale):\n"
                             + "\n\n".join(blocks))
                dbg["pinned"] = len(pinned)
        except Exception as e:
            dbg["errors"].append(f"pinned: {e}")

        knowledge = ps.retrieve_context(
            message,
            top_k=ps_cfg.get("knowledge_top_k", 4),
            max_chars=ps_cfg.get("knowledge_retrieve_chars", 3500),
        )
        if knowledge:
            parts.append(
                "CONOSCENZA DEL PROGETTO (estratti rilevanti alla domanda, "
                f"potrebbero essere parziali):\n{knowledge}")
            dbg["knowledge"] = len(knowledge)

        # FILE del progetto corrente: SEMPRE consultati insieme alla knowledge
        # (2026-07-10: prima era un fallback "solo se knowledge vuota" — bastava
        # una knowledge irrilevante, es. un CHECKSUMS.txt caricato per prova,
        # per oscurare del tutto i file veri del progetto).
        if req_project_path:
            files_ctx = ps.retrieve_from_files(message, top_k=3, max_chars=1500)
            if files_ctx:
                parts.append(
                    f"CONTENUTO DAI FILE DEL PROGETTO (estratti rilevanti):\n{files_ctx}")
                dbg["files"] = len(files_ctx)

        # Progetti connessi (citati per nome nel messaggio)
        for linked_path in _detect_linked_projects(message, persistence_key):
            linked_name = Path(linked_path).name
            try:
                linked_ps = _project_space_for(linked_path)
                # Knowledge E file, COMBINATI (2026-07-10): con l'`or` di prima una
                # knowledge irrilevante (es. CHECKSUMS.txt) faceva corto circuito e
                # i file veri (note_progetto.txt) non venivano mai letti.
                linked_blocks = []
                kn = linked_ps.retrieve_context(message, top_k=2, max_chars=800)
                if kn:
                    linked_blocks.append(kn)
                fl = linked_ps.retrieve_from_files(message, top_k=2, max_chars=800)
                if fl:
                    linked_blocks.append(fl)
                linked_ctx = "\n\n---\n\n".join(linked_blocks)
                if linked_ctx:
                    parts.append(
                        f"CONOSCENZA DAL PROGETTO COLLEGATO '{linked_name}' "
                        f"(citato nel messaggio):\n{linked_ctx}")
                    dbg["linked"].append(f"{linked_name}:kn{len(kn)}+file{len(fl)}ch")
                else:
                    file_list = linked_ps.list_files()
                    if file_list:
                        parts.append(
                            f"IL PROGETTO COLLEGATO '{linked_name}' (citato nel messaggio) "
                            f"contiene questi file: {', '.join(file_list)}. "
                            "Nessun estratto rilevante alla domanda e' stato trovato nel loro contenuto; "
                            "puoi elencare i file all'utente e chiedere quale approfondire.")
                        dbg["linked"].append(f"{linked_name}:lista-{len(file_list)}file")
                    else:
                        parts.append(
                            f"IL PROGETTO COLLEGATO '{linked_name}' (citato nel messaggio) "
                            "esiste ma e' vuoto o non contiene file di testo leggibili.")
                        dbg["linked"].append(f"{linked_name}:vuoto")
            except Exception as le:
                dbg["errors"].append(f"linked {linked_name}: {le}")

        automem = _get_automem()
        memories = automem.recall(message, tags=project_tags(persistence_key), limit=3)
        if memories:
            mem_budget = ai.config.get("automem", {}).get("recall_max_chars", 800)
            mem_text = "\n- ".join(m.strip() for m in memories)[:mem_budget]
            parts.append(f"MEMORIE RILEVANTI (AutoMem):\n- {mem_text}")
            dbg["automem"] = len(mem_text)
    except Exception as e:
        dbg["errors"].append(str(e))

    # Elenco progetti del workspace SEMPRE nel contesto (2026-07-10): senza,
    # il modello nega l'esistenza di progetti che non hanno prodotto estratti
    # ("non ho nessun test_project") — e noi non distinguiamo un retrieval
    # fallito da un progetto davvero inesistente.
    try:
        names = [d.name for d in WORKSPACE_DIR.iterdir()
                 if d.is_dir() and not d.name.startswith(("_", ".")) and d.name != "sandbox"]
        if names:
            parts.append("PROGETTI DI CODICE NEL WORKSPACE (NON è il limite di ciò che sai: "
                         "la KNOWLEDGE e i risultati WEB sono fonti separate, non ristrette a "
                         "questa lista): " + ", ".join(sorted(names))
                         + ". Se l'utente ne cita uno, i suoi estratti (se trovati) sono qui sopra.")
            dbg["workspace_projects"] = names
    except Exception as e:
        dbg["errors"].append(f"lista workspace: {e}")

    # Preambolo anti-"non ho accesso": se c'e' QUALSIASI contesto di progetto,
    # dillo esplicitamente al modello — i modelli piccoli tendono a rispondere
    # "non posso accedere ai tuoi file" anche con gli estratti davanti.
    if parts:
        parts.insert(0,
            "Hai davanti diverse FONTI di contesto qui sotto: file dei progetti, documenti "
            "di KNOWLEDGE del progetto, file PINNATI e (se presenti) risultati dal WEB. Usa "
            "tutto ciò che è rilevante e, se puoi, di' da quale fonte viene. NON dire che non "
            "hai accesso quando il contesto rilevante è presente qui sotto. Se un'informazione "
            "NON è in nessuna fonte qui, dillo onestamente invece di inventarla — e non "
            "contraddire ciò che hai appena affermato basandoti sul contesto.")
    return parts, json.dumps(dbg, ensure_ascii=False)


@app.get("/api/project/debug_context")
async def api_project_debug_context(q: str, project_path: str = ""):
    """DEBUG: mostra esattamente cosa verrebbe iniettato nel contesto per il
    messaggio `q` — stessa funzione usata dalla chat vera. Aprire nel browser:
    /api/project/debug_context?q=qual e il codice segreto nel progetto test_project"""
    persistence_key = project_path or GENERAL_CHAT_PROJECT_KEY
    parts, dbg = _build_project_context(q, persistence_key, project_path or None)
    return {
        "query": q,
        "persistence_key": persistence_key,
        "detected_linked_projects": _detect_linked_projects(q, persistence_key),
        "debug": json.loads(dbg),
        "injected_parts": parts,
    }


def _detect_linked_projects(message: str, current_key: str) -> list:
    """Progetti 'connessi' (2026-07-10): se il messaggio nomina un altro progetto
    del workspace, la sua knowledge viene consultata insieme a quella corrente.
    Match per nome cartella (word-boundary, case-insensitive, nomi >= 4 char per
    evitare falsi positivi), max 2 progetti per non sfondare il contesto."""
    import re as _re
    linked = []
    if not WORKSPACE_DIR.exists():
        return linked
    current = Path(current_key).name.lower()
    msg_lower = message.lower()
    for d in WORKSPACE_DIR.iterdir():
        if len(linked) >= 2:
            break
        if not d.is_dir() or d.name.startswith(("_", ".")) or d.name == "sandbox":
            continue
        name = d.name.lower()
        if name == current or len(name) < 4:
            continue
        # match sul nome esatto o sul nome con separatori normalizzati ("mio_prog" ~ "mio prog")
        pattern = _re.escape(name).replace(r"\_", r"[\s_\-]").replace(r"\-", r"[\s_\-]")
        if _re.search(rf"(?<![\w]){pattern}(?![\w])", msg_lower):
            linked.append(str(d))
    return linked


@app.post("/api/project/export_dataset")
async def api_project_export_dataset(request: Request):
    """Esporta tutte le chat del progetto in JSONL (formato OpenAI chat) in
    .devin/export/ — pronto per l'harness/LoRA futuro. Ritorna il path."""
    data = await request.json()
    ps = _project_space_for(data.get("project_path", ""))
    out = ps.export_dataset()
    if out is None:
        return {"status": "empty", "path": None}
    return {"status": "exported", "path": str(out)}


@app.post("/api/chat/generate_patch")
async def api_chat_generate_patch(request: Request):
    """
    'Genera patch da questa conversazione e riprova': prende la conversazione
    chat persistita per il progetto e la usa come piano (salta il Planner),
    poi Coder->Patcher->Runner->Critic come nel Mantenimento normale. Stesso
    streaming SSE via /stream/{run_id} gia' usato da /api/run e /api/chat/scaffold.
    """
    data = await request.json()
    project_path = data.get("project_path", "")
    if not project_path:
        return {"error": "missing project_path — imposta un progetto prima di generare codice dalla chat"}

    # chat_id (modalita' Progetti, 2026-07-10): usa la conversazione SELEZIONATA
    # in sidebar, non piu' solo la sessione legacy.
    history = ChatPersistence(project_path, chat_id=data.get("chat_id") or None).load()
    if not history:
        return {"error": "nessuna conversazione salvata per questo progetto/chat"}

    conversation_text = "\n\n".join(f"[{m['role'].upper()}]: {m['content']}" for m in history)

    # Progetto senza codice -> la cosa giusta e' lo Zero-Shot Scaffolding dalla
    # conversazione (creare i file), non il ciclo di patch (che presuppone
    # codice esistente da modificare).
    _proj = Path(project_path).expanduser()
    _is_empty_project = (not _proj.exists()) or not any(
        f for f in _proj.rglob("*.py")
        if not any(part in (".devin", ".devin_chat", "workspace", "venv", ".git", "__pycache__")
                   for part in f.relative_to(_proj).parts))
    mode = "scaffold" if _is_empty_project else "patch"

    run_id = datetime.now().strftime("run_%Y%m%d_%H%M%S_%f")
    log_path = LOG_DIR / f"{run_id}.log"
    log_path.write_text(f"{'Scaffold' if mode == 'scaffold' else 'Patch'} da conversazione: {run_id}\n",
                        encoding="utf-8")

    def sse_callback(msg, level):
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"[{level.upper()}] {msg}\n")

    def _bg():
        try:
            with Orchestrator(
                config_path=CONFIG_PATH,
                project_path=project_path,
                sse_callback=sse_callback
            ) as orch:
                with runs_lock:
                    active_runs[run_id] = orch
                try:
                    if mode == "scaffold":
                        # Progetto vuoto: crea i file da zero usando la
                        # conversazione come specifica (stesso run_scaffold
                        # del routing "Chat First").
                        result = orch.run_scaffold(
                            task=("Realizza il progetto descritto in questa conversazione. "
                                  "Segui le decisioni prese e le correzioni piu' recenti.\n\n"
                                  + conversation_text),
                            project_path=project_path,
                            run_id=run_id
                        )
                        with open(log_path, "a", encoding="utf-8") as f:
                            f.write(f"status: {'success' if result.get('success') else 'failed'}\n")
                    else:
                        result = orch.run_from_conversation(
                            conversation_text=conversation_text,
                            project_path=project_path,
                            run_id=run_id
                        )
                        # Niente scrittura qui: run_from_conversation() scrive gia' il
                        # footer 'status: X' internamente in ogni return path.
                finally:
                    with runs_lock:
                        active_runs.pop(run_id, None)
        except Exception as e:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(f"\n[FATAL] {e}\nstatus: failed\n")

    threading.Thread(target=_bg, daemon=True).start()
    return {"run_id": run_id, "status": "started", "mode": mode}


# ============================================================
# API - MODELS
# ============================================================

@app.get("/api/models/status")
async def api_models_status():
    launcher = _get_launcher()
    if not launcher:
        return {"running": False, "models": []}
    status = launcher.get_status()
    return {
        "running": bool(status.local_running),
        "models": list(status.local_running.values()),
        "source": status.model_source
    }


@app.post("/api/models/kill")
async def api_models_kill():
    launcher = _get_launcher()
    if not launcher:
        return {"error": "launcher not available"}
    try:
        launcher.shutdown_all()
        return {"status": "killed"}
    except Exception as e:
        return {"error": str(e)}


# ============================================================
# API - RUNS (orchestrator, Modalita' 1: Mantenimento)
# ============================================================

class RunRequest(BaseModel):
    path: str
    task: str = "trova e correggi eventuali bug"
    entrypoint: Optional[str] = None
    max_attempts: int = 3
    max_seconds: int = 300


@app.post("/api/run")
async def api_run(req: RunRequest):
    if not req.path:
        return {"error": "missing path"}

    run_id = datetime.now().strftime("run_%Y%m%d_%H%M%S_%f")
    # Crea il file di log SUBITO (come /api/chat/scaffold): l'inizializzazione
    # dell'Orchestrator (launcher + health-check) puo' richiedere secondi, e
    # /stream/{run_id} si arrende dopo ~10s se non trova il file -> "Waiting for
    # log file..." all'infinito. Scriverlo qui garantisce che lo stream lo trovi.
    log_path_init = LOG_DIR / f"{run_id}.log"
    log_path_init.write_text(f"Run started: {run_id}\nTask: {req.task}\n", encoding="utf-8")

    def _bg():
        try:
            def sse_callback(msg, level):
                log_path = LOG_DIR / f"{run_id}.log"
                with open(log_path, "a", encoding="utf-8") as f:
                    f.write(f"[{level.upper()}] {msg}\n")

            with Orchestrator(
                config_path=CONFIG_PATH,
                project_path=req.path,
                sse_callback=sse_callback
            ) as orch:
                with runs_lock:
                    active_runs[run_id] = orch
                try:
                    result = orch.run(
                        task=req.task,
                        project_path=req.path,
                        entrypoint=req.entrypoint,
                        max_attempts=req.max_attempts,
                        max_seconds=req.max_seconds,
                        run_id=run_id
                    )
                    # FIX: niente piu' scrittura qui — orchestrator.run() scrive GIA'
                    # il footer 'status: X' internamente (in ogni return path, vedi
                    # write_status_footer() in orchestrator.py). Scriverlo anche qui
                    # duplicava la riga.
                finally:
                    with runs_lock:
                        active_runs.pop(run_id, None)
        except Exception as e:
            log_path = LOG_DIR / f"{run_id}.log"
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(f"\n[FATAL] {e}\n")
                f.write("status: failed\n")

    t = threading.Thread(target=_bg, daemon=True)
    t.start()

    return {"run_id": run_id, "status": "started"}


# ============================================================
# API - SCAFFOLD (Modalita' 2: Zero-Shot Scaffolding, "Chat First")
# ============================================================

@app.post("/api/chat/scaffold")
async def api_chat_scaffold(req: RunRequest):
    """
    Avvia la creazione di un progetto da zero, esclusivamente via tool (no diff pipeline).
    Il frontend fa subito subscribe a /stream/{run_id}: nessun tempo morto silenzioso,
    ogni file creato emette un evento SSE (regola Chat First).
    """
    if not req.path:
        return {"error": "missing path"}

    run_id = datetime.now().strftime("run_%Y%m%d_%H%M%S_%f")
    log_path = LOG_DIR / f"{run_id}.log"
    log_path.write_text(f"Scaffold started: {run_id}\nTask: {req.task}\n", encoding="utf-8")

    def sse_callback(msg, level):
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"[{level.upper()}] {msg}\n")

    def _bg():
        try:
            with Orchestrator(
                config_path=CONFIG_PATH,
                project_path=req.path,
                sse_callback=sse_callback
            ) as orch:
                with runs_lock:
                    active_runs[run_id] = orch
                try:
                    result = orch.run_scaffold(task=req.task, project_path=req.path, run_id=run_id)
                    with open(log_path, "a", encoding="utf-8") as f:
                        f.write(f"\nstatus: {'success' if result.get('success') else 'failed'}\n")
                finally:
                    with runs_lock:
                        active_runs.pop(run_id, None)
        except Exception as e:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(f"\n[FATAL] {e}\n")
                f.write("status: failed\n")

    t = threading.Thread(target=_bg, daemon=True)
    t.start()

    return {"run_id": run_id, "status": "started", "mode": "scaffold"}


@app.post("/api/stop")
async def api_stop(request: Request):
    data = await request.json()
    run_id = data.get("run_id")
    if not run_id:
        return {"error": "missing run_id"}

    with runs_lock:
        orch = active_runs.get(run_id)

    if orch:
        orch.stop()
        return {"status": "stop_requested", "run_id": run_id}
    return {"error": "run not found or already finished"}


# ============================================================
# API - LOGS
# ============================================================

@app.get("/api/runs/active")
async def api_runs_active():
    """
    Run realmente in esecuzione ORA (oggetti Orchestrator vivi in memoria), non una
    euristica sul contenuto del log. Un run vecchio/crashato che non ha scritto la
    riga finale 'status: ...' ha status='unknown' in /api/runs ma NON e' qui dentro:
    evita che la dashboard lo mostri come 'in esecuzione' per sempre.
    """
    with runs_lock:
        return {"active_run_ids": list(active_runs.keys())}


@app.get("/api/runs")
async def api_runs():
    if not LOG_DIR.exists():
        return []
    runs = []
    for f in sorted(LOG_DIR.glob("run_*.log"), reverse=True):
        stat = f.stat()
        content = f.read_text(encoding="utf-8", errors="ignore")
        status = "unknown"
        if "status: success" in content.lower():
            status = "success"
        elif "status: failed" in content.lower():
            status = "failed"
        elif "status: timeout" in content.lower():
            status = "timeout"
        elif "status: stopped" in content.lower():
            status = "stopped"
        runs.append({
            "run_id": f.stem,
            "file": str(f.name),
            "size": f.stat().st_size,
            "mtime": datetime.fromtimestamp(stat.st_mtime).isoformat(),
            "status": status,
            "preview": content[:500]
        })
    return runs[:50]


@app.get("/api/run/{run_id}/log")
async def api_run_log(run_id: str, download: int = 0):
    """#27: con ?download=1 restituisce il .log come file (Content-Disposition),
    altrimenti testo leggibile nel browser (prima tornava sempre JSON, che il tab
    'View'/'Log' mostrava grezzo). Containment su LOG_DIR contro traversal via run_id."""
    log_path = (LOG_DIR / f"{run_id}.log").resolve()
    if LOG_DIR.resolve() not in log_path.parents or not log_path.exists():
        return {"error": "not found"}
    if download:
        return FileResponse(str(log_path), media_type="text/plain; charset=utf-8",
                            filename=f"{run_id}.log")
    return PlainTextResponse(log_path.read_text(encoding="utf-8", errors="ignore"))


@app.get("/stream/{run_id}")
async def stream_log(run_id: str):
    log_path = LOG_DIR / f"{run_id}.log"

    async def generate():
        for _ in range(20):
            if log_path.exists():
                break
            await asyncio.sleep(0.5)
            yield f"data: {json.dumps({'type': 'wait', 'msg': 'Waiting for log file...'})}\n\n"
        else:
            yield f"data: {json.dumps({'type': 'error', 'msg': 'Log file not found'})}\n\n"
            return

        # FIX audit #9 (2026-07-10): prima c'era f.seek(0, 2) — scartava TUTTE le
        # righe gia' scritte (su run veloci si perdeva anche il footer 'status:',
        # la UI restava appesa) e il while non terminava MAI: connessioni SSE
        # zombie che si accumulavano (era il vero motivo per cui Ctrl+C richiedeva
        # os._exit). Ora: lettura da inizio file, chiusura sul footer di stato o
        # quando il run non e' piu' attivo.
        import re as _re
        status_re = _re.compile(r"^status:\s*(success|failed|timeout|stopped)\s*$")
        dead_polls = 0
        with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
            while True:
                line = f.readline()
                if line:
                    payload = json.dumps({"type": "log", "line": line.rstrip("\n")})
                    yield f"data: {payload}\n\n"
                    if status_re.match(line.strip()):
                        yield f"data: {json.dumps({'type': 'done'})}\n\n"
                        return
                    continue
                # nessuna riga nuova: il run e' ancora vivo?
                with runs_lock:
                    alive = run_id in active_runs
                if not alive:
                    dead_polls += 1
                    if dead_polls > 10:  # ~3s di grazia per l'ultimo flush su disco
                        yield f"data: {json.dumps({'type': 'done'})}\n\n"
                        return
                else:
                    dead_polls = 0
                await asyncio.sleep(0.3)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no"
        }
    )


# ============================================================
# API - AUTOCOMPLETE (Task 16, client condiviso — fix bug 1.2)
# ============================================================

class AutocompleteRequest(BaseModel):
    code: str


@app.post("/api/autocomplete")
async def api_autocomplete(req: AutocompleteRequest):
    if not req.code:
        return {"suggestion": ""}

    try:
        auto = _get_autocomplete()
        suggestion = auto.suggest(req.code)
        return {"suggestion": suggestion or ""}
    except Exception as e:
        return {"suggestion": "", "error": str(e)}


# ============================================================
# API - AUTOCOMPLETE STREAMING (Task 16 — Monaco Editor)
# ============================================================

class AutocompleteStreamRequest(BaseModel):
    code: str
    language: str = "python"
    cursor_position: int = None


@app.post("/api/autocomplete/stream")
async def api_autocomplete_stream(req: AutocompleteStreamRequest):
    """
    Autocomplete con streaming SSE per Monaco Editor.
    Usa il modello Coder locale (backup leggero) per suggerimenti rapidi.
    """
    if not req.code:
        return {"error": "empty code"}

    auto = _get_autocomplete()

    async def generate_sse():
        try:
            yield f"event: meta\ndata: {json.dumps({'language': req.language, 'mode': 'coder'})}\n\n"

            token_count = 0
            for chunk in auto.suggest_stream(req.code, language=req.language, cursor_position=req.cursor_position):
                token_count += 1
                yield f"data: {json.dumps({'token': chunk})}\n\n"
                await asyncio.sleep(0)

            yield f"event: done\ndata: {json.dumps({'tokens': token_count})}\n\n"

        except Exception as e:
            yield f"event: error\ndata: {json.dumps({'error': str(e)})}\n\n"

    return StreamingResponse(
        generate_sse(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        }
    )


# ============================================================
# MAIN + AUTO-OPEN
# ============================================================

def _is_wsl() -> bool:
    try:
        return "microsoft" in Path("/proc/version").read_text().lower()
    except Exception:
        return False


def run_server():
    """
    Avvio completo del server (browser auto-open + uvicorn con shutdown pulito).
    Estratto in funzione richiamabile cosi' launcher.py puo' avviare QUESTA dashboard
    (non la vecchia UI Tkinter in devin/ui/app.py) con un semplice import + call.
    """
    import uvicorn

    URL = "http://localhost:5000"

    def open_browser():
        time.sleep(2)
        if _is_wsl():
            # L'interop WSL->Windows per aprire il browser (via rundll32.exe) e'
            # inaffidabile e scrive errori direttamente su stderr, non intercettabili
            # da un try/except Python. Piu' robusto saltarlo del tutto su WSL.
            print(f"\n-- WSL rilevato: apri manualmente {URL} nel browser")
            return
        print(f"\n-- Opening browser at {URL}")
        try:
            webbrowser.open(URL)
        except Exception as e:
            print(f"\n-- Impossibile aprire il browser automaticamente ({e}). Apri manualmente: {URL}")

    browser_thread = threading.Thread(target=open_browser, daemon=True)
    browser_thread.start()

    # FIX: le connessioni SSE aperte (/stream/{run_id}, polling ogni 5s da piu' tab)
    # impedivano allo shutdown grazioso di uvicorn di completarsi entro tempi ragionevoli
    # su Ctrl+C (richiedeva pkill). timeout_graceful_shutdown basso + os._exit di
    # sicurezza garantiscono che il processo termini sempre entro ~3s.
    # FIX audit #7 (2026-07-10): default 127.0.0.1 — prima 0.0.0.0 esponeva a
    # TUTTA la LAN (senza auth) lettura file, avvio agenti, stop modelli. Per
    # l'accesso da altre macchine (es. dashboard sul rig raggiunta dalla
    # workstation) imposta "ui": {"host": "0.0.0.0"} in settings.json — scelta
    # esplicita, non default.
    try:
        _ui_cfg = json.loads(Path(CONFIG_PATH).read_text(encoding="utf-8")).get("ui", {})
    except Exception:
        _ui_cfg = {}
    _host = _ui_cfg.get("host", "127.0.0.1")
    if _host == "0.0.0.0":
        print("[SECURITY] UI esposta su tutta la rete (ui.host=0.0.0.0 in settings.json): "
              "assicurati di essere su una LAN fidata.")
    config = uvicorn.Config(app, host=_host, port=int(_ui_cfg.get("port", 5000)),
                            log_level="info", timeout_graceful_shutdown=3)
    server = uvicorn.Server(config)
    try:
        server.run()
    except KeyboardInterrupt:
        pass
    finally:
        print("\n[SHUTDOWN] Chiusura forzata (i modelli locali/rig restano attivi in background)...")
        os._exit(0)


if __name__ == "__main__":
    run_server()
