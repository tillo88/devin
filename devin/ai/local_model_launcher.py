"""local_model_launcher.py - FASE 3: Local come backup (chat + autocomplete only)

Il rig esterno (51GB VRAM, 7 GPU) e' ora l'hardware PRIMARIO per reasoning/coding pesante.
Il locale (RTX 5070Ti 16GB) NON carica piu' di default il modello reasoning da 14B:
serve solo per autocomplete (Task 16) e come fallback chat leggero quando il rig e' down.

Se serve comunque il reasoning in locale in emergenza, usa ensure_reasoning_emergency() —
non e' piu' nel path di avvio automatico.

FASE 1 (invariato):
- VRAM Watchdog: polling ogni 30s con nvidia-smi
- OOM Detection: rileva crash llama-server (exit code 137/1) e triggera fallback
- Swap esplicito: kill_server_on_port + riavvio con modello diverso
"""

import subprocess
import time
import os
import json
import platform
import signal
import threading
from pathlib import Path
from dataclasses import dataclass, field
import requests

LLAMA_SERVER_BIN = Path.home() / "llama.cpp/build/bin/llama-server"
MODELS_DIR = Path.home() / "devin_ai_ide/devin/devin_models"

# === MODELLI (filename allineati ai file REALI presenti in devin_models/) ===
# Coder locale = Ornith-1.0-9B-Q8 (deepreinforce): piu' capace del vecchio
# Qwen2.5-Coder-7B, addestrato RL self-scaffolding.
CODER_ORNITH = MODELS_DIR / "deepreinforce-ai_Ornith-1.0-9B-Q8_0.gguf"
CODER_QWEN_FALLBACK = MODELS_DIR / "qwen2.5-coder-7b-instruct-q5_k_m.gguf"
CODER_FILE = CODER_ORNITH if CODER_ORNITH.exists() else CODER_QWEN_FALLBACK

# Planner locale (avviato solo in local_test_mode): Qwen3.5-14B MoE.
PLANNER_MOE = MODELS_DIR / "Qwen3.5-14B-A3B-Claude-Opus-Reasoning-Distilled-4.6-MXFP4_MOE.gguf"
PLANNER_FALLBACK = MODELS_DIR / "qwen3-14b-q4_k_m.gguf"
PLANNER_FILE = PLANNER_MOE if PLANNER_MOE.exists() else PLANNER_FALLBACK

# Vision rimosso da DEVIN (deciso 2026-07-09): per codice/debug serve poco, e Hermes
# (ruolo dedicato del rig, ai-rig-iso-build) copre gia' quel caso d'uso. Niente piu'
# --mmproj per coder/planner locali: libera VRAM (era la causa del quasi-OOM al 96%+
# con entrambi i modelli + mmproj caricati insieme su 16GB) e niente piu' fallback
# silenzioso su un modello che comunque riconosceva male le immagini.
MODELS = {
    "coder": {
        "file": CODER_FILE,
        "port": 8000,
        "ctx": 8192,
        "ngl": 99,
        "alias": "coder",
        # Ornith richiede --jinja per il suo chat template (come sul rig). Passato
        # solo se il coder attivo e' effettivamente Ornith (il fallback Qwen no).
        "jinja": CODER_FILE == CODER_ORNITH,
        "mmproj": None,
    },
    # NON avviato di default (vedi AUTO_START_ALIASES) — tenuto per compatibilita' con
    # swap_model()/kill_server_on_port() e per avvio manuale in emergenza.
    "planner": {
        "file": PLANNER_FILE,
        "port": 8001,
        "ctx": 8192,
        "ngl": 99,
        "alias": "planner",
        "mmproj": None,
    },
}

# Alias avviati automaticamente da from_config()/main() — SOLO il coder leggero.
# Il reasoning pesante vive sul rig primario (Qwen3.6-35B-A3B, vedi settings.json).
AUTO_START_ALIASES = ["coder"]

HEALTH_TIMEOUT = 120
HEALTH_INTERVAL = 3

_log_fds = {}

# === VRAM WATCHDOG GLOBALE ===
_vram_watchdog_thread = None
_vram_watchdog_stop = threading.Event()
_vram_last_check = 0
_vram_critical_threshold = 95  # percentuale VRAM usata


@dataclass
class LauncherStatus:
    rig_available: bool = False
    rig_host: str = ""
    rig_ports: list = field(default_factory=list)
    local_running: dict = field(default_factory=dict)
    model_source: str = "unavailable"
    errors: list = field(default_factory=list)

    def to_dict(self):
        return {
            "rig_available": self.rig_available,
            "rig_host": self.rig_host,
            "rig_ports": self.rig_ports,
            "local_running": self.local_running,
            "model_source": self.model_source,
            "errors": self.errors,
        }


def is_wsl():
    """Rileva se siamo dentro WSL (fix bug 3.3 del report: 'platform' non era importato)."""
    try:
        return "microsoft" in platform.release().lower()
    except Exception:
        return False


def _get_vram_mb():
    """Ritorna VRAM libera in MB dalla GPU 0, o None se nvidia-smi non disponibile."""
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5
        )
        lines = [l.strip() for l in result.stdout.strip().split("\n") if l.strip()]
        if lines:
            return int(float(lines[0]))
    except Exception:
        pass
    return None


def _get_vram_used_percent():
    """Ritorna percentuale VRAM usata dalla GPU 0, o None."""
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5
        )
        lines = [l.strip() for l in result.stdout.strip().split("\n") if l.strip()]
        if lines:
            used, total = map(float, lines[0].split(","))
            return (used / total) * 100
    except Exception:
        pass
    return None


def _resolve_model_file(config):
    """
    Seleziona il file modello in base alla VRAM disponibile.
    Se il primario non entra, cerca un fallback Q4_K_M se il primario e Q5_K_M.
    """
    primary = config["file"]
    free_vram = _get_vram_mb()

    if free_vram is None:
        return primary

    vram_estimates = {
        "Qwen3.5-14B-A3B": 5500,
        "qwen3-14b-q4_k_m": 10000,
        "Ornith-1.0-9B-Q8": 9500,
        "qwen2.5-coder-7b-instruct-q5_k_m": 5800,
    }

    primary_est = 10000
    for key, est in vram_estimates.items():
        if key in primary.name:
            primary_est = est
            break

    if primary_est <= free_vram:
        return primary

    fallback_path = MODELS_DIR / "qwen3-14b-q4_k_m.gguf"
    fallback_est = vram_estimates.get("qwen3-14b-q4_k_m", 10000)

    if fallback_path.exists() and fallback_est <= free_vram:
        print(f"[OOM] Primary {primary.name} needs ~{primary_est}MB, free={free_vram}MB. "
              f"Fallback to {fallback_path.name}")
        return fallback_path

    print(f"[WARN] VRAM free={free_vram}MB may be insufficient for {primary.name} "
          f"(~{primary_est}MB), no suitable fallback found. Proceeding anyway.")
    return primary


def is_model_fully_loaded(port, alias):
    base_url = "http://127.0.0.1:{}".format(port)

    try:
        r = requests.get("{}/health".format(base_url), timeout=5)
        if r.status_code != 200:
            return False, "/health returned {}".format(r.status_code)
        if r.json().get("status") != "ok":
            return False, "/health status != ok"
    except requests.exceptions.ConnectionError:
        return False, "Connection refused on /health"
    except Exception as e:
        return False, "/health exception: {}".format(e)

    try:
        r = requests.get("{}/v1/models".format(base_url), timeout=5)
        if r.status_code != 200:
            return False, "/v1/models returned {}".format(r.status_code)
        data = r.json()
        models = data.get("data", [])
        if not models:
            return False, "/v1/models: no models"
        meta = models[0].get("meta")
        if meta is None:
            return False, "/v1/models: meta is null (still loading)"
    except requests.exceptions.ConnectionError:
        return False, "Connection refused on /v1/models"
    except Exception as e:
        return False, "/v1/models exception: {}".format(e)

    return True, "OK - meta present"


def wait_for_model_loaded(port, alias, timeout=HEALTH_TIMEOUT, interval=HEALTH_INTERVAL):
    start = time.time()
    last_reason = "unknown"

    while time.time() - start < timeout:
        loaded, reason = is_model_fully_loaded(port, alias)
        if loaded:
            return True, reason
        last_reason = reason
        print("    [WAIT] {} - retrying in {}s...".format(reason, interval))
        time.sleep(interval)

    return False, "Timeout after {}s - last: {}".format(timeout, last_reason)


def start_llama_server(config):
    port = config["port"]
    model_file = _resolve_model_file(config)
    alias = config["alias"]

    if not model_file.exists():
        raise FileNotFoundError("Model file not found: {}".format(model_file))

    if not LLAMA_SERVER_BIN.exists():
        raise FileNotFoundError("llama-server not found: {}".format(LLAMA_SERVER_BIN))

    cmd = [
        str(LLAMA_SERVER_BIN),
        "-m", str(model_file),
        "--host", "0.0.0.0",
        "--port", str(port),
        "-c", str(config["ctx"]),
        "-ngl", str(config["ngl"]),
        "--alias", alias,
    ]

    if config.get("jinja"):
        cmd.append("--jinja")
        print(f"[JINJA] chat template jinja abilitato per '{alias}' (richiesto da Ornith)")

    mmproj = config.get("mmproj")
    if mmproj and mmproj.exists():
        cmd.extend(["--mmproj", str(mmproj)])
        print(f"[VISION] mmproj loaded: {mmproj.name}")
    elif mmproj:
        print(f"[VISION] ATTENZIONE: mmproj configurato ma file non trovato: {mmproj} "
              f"— '{alias}' partira' SENZA supporto vision. Verifica il nome file in devin_models/.")

    print("[START] Avvio '{}' su porta {}".format(alias, port))
    print("        Model: {}".format(model_file.name))

    env = os.environ.copy()
    cuda_lib = "/usr/local/cuda-12.8/lib64"
    if "LD_LIBRARY_PATH" in env:
        if cuda_lib not in env["LD_LIBRARY_PATH"]:
            env["LD_LIBRARY_PATH"] = cuda_lib + ":" + env["LD_LIBRARY_PATH"]
    else:
        env["LD_LIBRARY_PATH"] = cuda_lib

    log_dir = Path.home() / "devin_ai_ide/logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "llama-server-{}.log".format(alias)

    fd = os.open(str(log_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
    _log_fds[alias] = fd

    return subprocess.Popen(
        cmd,
        stdout=fd,
        stderr=subprocess.STDOUT,
        env=env,
        start_new_session=True,
    )


def kill_server_on_port(port):
    try:
        result = subprocess.run(
            ["lsof", "-ti", ":{}".format(port)],
            capture_output=True, text=True
        )
        for pid in result.stdout.strip().split("\n"):
            pid = pid.strip()
            if pid:
                try:
                    os.kill(int(pid), signal.SIGKILL)
                    print("[KILL] Terminato PID {} su porta {}".format(pid, port))
                except ProcessLookupError:
                    pass
        time.sleep(2)
    except Exception as e:
        print("[WARN] Errore kill porta {}: {}".format(port, e))


def ensure_model_running(alias, config, max_retries=2):
    port = config["port"]

    loaded, reason = is_model_fully_loaded(port, alias)
    if loaded:
        print("[OK] '{}' gia caricato - {}".format(alias, reason))
        return True

    print("[INFO] '{}' non caricato - {}".format(alias, reason))
    kill_server_on_port(port)
    time.sleep(1)

    for attempt in range(max_retries):
        print("[RETRY] Tentativo {}/{} per '{}'".format(attempt + 1, max_retries, alias))

        try:
            process = start_llama_server(config)
            time.sleep(5)
            loaded, reason = wait_for_model_loaded(port, alias)

            if loaded:
                print("[SUCCESS] '{}' caricato - {}".format(alias, reason))
                return True
            else:
                print("[FAIL] '{}' non caricato - {}".format(alias, reason))
                try:
                    process.terminate()
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                if attempt < max_retries - 1:
                    backoff = 2 ** attempt
                    print("[WAIT] Backoff {}s prima del prossimo retry...".format(backoff))
                    time.sleep(backoff)

        except Exception as e:
            print("[ERROR] Eccezione avvio '{}': {}".format(alias, e))
            if attempt < max_retries - 1:
                backoff = 2 ** attempt
                print("[WAIT] Backoff {}s prima del prossimo retry...".format(backoff))
                time.sleep(backoff)

    print("[FATAL] Impossibile caricare '{}'".format(alias))
    return False


# ============================================================
# VRAM WATCHDOG + OOM DETECTION + SWAP
# ============================================================

def _vram_watchdog_loop(interval_seconds=30):
    global _vram_last_check
    print(f"[VRAM-WATCHDOG] Avviato (interval={interval_seconds}s)")

    while not _vram_watchdog_stop.is_set():
        try:
            used_pct = _get_vram_used_percent()
            if used_pct is not None:
                _vram_last_check = time.time()
                if used_pct > _vram_critical_threshold:
                    print(f"[VRAM-WATCHDOG] CRITICAL: {used_pct:.1f}% VRAM used")
                    for alias, cfg in MODELS.items():
                        loaded, reason = is_model_fully_loaded(cfg["port"], alias)
                        if not loaded and "Connection refused" in reason:
                            print(f"[VRAM-WATCHDOG] Detected crash on '{alias}' (likely OOM)")
        except Exception as e:
            print(f"[VRAM-WATCHDOG] Error: {e}")

        _vram_watchdog_stop.wait(timeout=interval_seconds)


def start_vram_watchdog(interval_seconds=30):
    global _vram_watchdog_thread
    if _vram_watchdog_thread is not None and _vram_watchdog_thread.is_alive():
        return

    _vram_watchdog_stop.clear()
    _vram_watchdog_thread = threading.Thread(
        target=_vram_watchdog_loop,
        args=(interval_seconds,),
        daemon=True,
        name="VRAM-Watchdog"
    )
    _vram_watchdog_thread.start()


def stop_vram_watchdog():
    global _vram_watchdog_thread
    _vram_watchdog_stop.set()
    if _vram_watchdog_thread is not None:
        _vram_watchdog_thread.join(timeout=5)
        _vram_watchdog_thread = None


def swap_model(alias, new_config=None):
    if alias not in MODELS:
        raise ValueError(f"Alias sconosciuto: {alias}")

    config = MODELS[alias].copy()
    if new_config:
        config.update(new_config)

    print(f"[SWAP] Swapping '{alias}'...")
    kill_server_on_port(config["port"])
    time.sleep(2)

    ok = ensure_model_running(alias, config)
    if ok:
        print(f"[SWAP] '{alias}' swap completato")
    else:
        print(f"[SWAP] '{alias}' swap FALLITO")
    return ok


def get_vram_status():
    free_mb = _get_vram_mb()
    used_pct = _get_vram_used_percent()
    return {
        "free_mb": free_mb,
        "used_percent": used_pct,
        "critical_threshold": _vram_critical_threshold,
        "is_critical": used_pct is not None and used_pct > _vram_critical_threshold,
        "last_check": _vram_last_check,
    }


class LocalModelLauncher:
    def __init__(self):
        self.processes = {}
        self.status = "stopped"
        # Copia per-istanza: from_config() puo' estenderla con "planner" se local_test_mode=true
        self.auto_start_aliases = list(AUTO_START_ALIASES)

    @classmethod
    def from_config(cls, config_path, sse_callback=None):
        """
        FASE 3: avvia SOLO gli alias in AUTO_START_ALIASES (di default: solo 'coder').
        Il reasoning pesante non parte piu' in automatico — vive sul rig primario.

        ECCEZIONE TEMPORANEA (rig non ancora arrivato): se settings.json ha
        models.local_test_mode = true, avvia ANCHE 'planner' in locale, cosi'
        Mantenimento (Planner/Critic) e Zero-Shot Scaffolding sono testabili
        end-to-end senza rig. Ricordati di rimettere false quando il rig arriva.
        """
        instance = cls()

        try:
            with open(config_path, "r") as f:
                models_cfg = json.load(f).get("models", {})
            if models_cfg.get("local_test_mode") and "planner" not in instance.auto_start_aliases:
                instance.auto_start_aliases.append("planner")
                print("[TEST MODE] local_test_mode=true — avvio anche 'planner' in locale "
                      "(rig non ancora disponibile). VRAM: coder+planner ~10-15GB su 16GB, ok.")
        except Exception as e:
            print(f"[WARN] Impossibile leggere local_test_mode da {config_path}: {e}")

        for alias in instance.auto_start_aliases:
            config = MODELS[alias]
            ok = ensure_model_running(alias, config)
            if ok:
                instance.processes[alias] = True
                instance.status = "running"
        start_vram_watchdog()
        return instance

    def ensure_models(self):
        """Verifica/avvia gli alias di auto-start per questa istanza (rispetta local_test_mode)."""
        running = {}
        for alias in self.auto_start_aliases:
            config = MODELS[alias]
            loaded, _ = is_model_fully_loaded(config["port"], alias)
            if loaded:
                running[alias] = {"name": alias, "port": config["port"], "status": "running"}
                self.processes[alias] = True
            else:
                ok = ensure_model_running(alias, config)
                if ok:
                    running[alias] = {"name": alias, "port": config["port"], "status": "running"}
                    self.processes[alias] = True

        if running:
            self.status = "running"
            return LauncherStatus(
                rig_available=False, rig_host="", rig_ports=[],
                local_running=running, model_source="local", errors=[],
            )
        else:
            self.status = "stopped"
            return LauncherStatus(
                rig_available=False, rig_host="", rig_ports=[],
                local_running={}, model_source="unavailable",
                errors=["No local models running"],
            )

    def ensure_reasoning_emergency(self):
        """
        Avvio MANUALE (non automatico) del reasoning locale 14B, da chiamare solo se
        il rig resta down a lungo e serve reasoning anche in locale. Consuma ~10GB
        aggiuntivi sui 16GB della 5070Ti: verificare VRAM libera prima di chiamarlo.
        """
        return ensure_model_running("planner", MODELS["planner"])

    def get_status(self):
        running = {}
        for alias in self.processes:
            if alias in MODELS:
                running[alias] = {
                    "name": alias,
                    "port": MODELS[alias]["port"],
                    "status": "running"
                }
        return LauncherStatus(
            rig_available=False, rig_host="", rig_ports=[],
            local_running=running,
            model_source="local" if self.processes else "unavailable",
            errors=[],
        )

    def shutdown_all(self):
        for alias in list(self.processes.keys()):
            if alias in MODELS:
                kill_server_on_port(MODELS[alias]["port"])
        self.processes.clear()
        self.status = "stopped"
        stop_vram_watchdog()


def main():
    print("=" * 60)
    print("DEVIN AI IDE - Local Model Launcher (backup: chat + autocomplete only)")
    print("=" * 60)

    try:
        result = subprocess.run(["nvidia-smi"], capture_output=True, text=True, timeout=5)
        print("[CUDA] nvidia-smi:\n" + result.stdout[:500] + "\n")
    except Exception as e:
        print("[WARN] nvidia-smi non disponibile: {}\n".format(e))

    results = {}
    for alias in AUTO_START_ALIASES:
        results[alias] = ensure_model_running(alias, MODELS[alias])
        print()

    start_vram_watchdog()

    print("=" * 60)
    print("RIEPILOGO (solo modelli auto-start locali — il rig e' primario)")
    print("=" * 60)
    all_ok = True
    for alias, ok in results.items():
        status = "UP & LOADED" if ok else "FAILED"
        print("  {:12} -> {}".format(alias, status))
        if not ok:
            all_ok = False

    if all_ok:
        print("\nModelli locali di backup pronti!")
        for alias in AUTO_START_ALIASES:
            print("  curl http://127.0.0.1:{}/v1/models".format(MODELS[alias]["port"]))
    else:
        print("\nAlcuni modelli non caricati. Controlla i log.")
        exit(1)


if __name__ == "__main__":
    main()
