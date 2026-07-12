import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import os
import json
import time
import threading
from datetime import datetime

from flask import Flask, render_template, request, jsonify, Response, stream_with_context

from devin.core.orchestrator import Orchestrator, LOG_DIR
from devin.ai.client import AIClient
from devin.ai.local_model_launcher import LocalModelLauncher
from devin.ai.stream import stream_chat

app = Flask(__name__)
app.jinja_env.auto_reload = True

# === RUNTIME STATE ===
active_runs = {}
runs_lock = threading.Lock()

_model_launcher = None


def _get_launcher():
    global _model_launcher
    if _model_launcher is None:
        try:
            _model_launcher = LocalModelLauncher.from_config("config/settings.json")
        except Exception as e:
            print(f"[WARN] Could not init launcher: {e}")
    return _model_launcher


def _list_runs(limit=50):
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
        elif "status: no_progress" in content.lower():
            status = "no_progress"
        runs.append({
            "run_id": f.stem,
            "file": str(f.name),
            "size": f.stat().st_size,
            "mtime": datetime.fromtimestamp(stat.st_mtime).isoformat(),
            "status": status,
            "preview": content[:500]
        })
    return runs[:limit]


@app.route("/")
def index():
    client = AIClient()
    health = client.health()
    launcher = _get_launcher()
    models_running = False
    if launcher:
        status = launcher.get_status()
        models_running = bool(status.local_running)
    return render_template("index.html", health=health, models_running=models_running)


@app.route("/chat")
def chat_page():
    return render_template("chat.html")


@app.route("/history")
def history():
    runs = _list_runs()
    return render_template("history.html", runs=runs)


@app.route("/api/health")
def api_health():
    client = AIClient()
    return jsonify(client.health())


@app.route("/api/models/status")
def api_models_status():
    launcher = _get_launcher()
    if not launcher:
        return jsonify({"running": False, "models": []})
    status = launcher.get_status()
    return jsonify({
        "running": bool(status.local_running),
        "models": list(status.local_running.values()),
        "source": status.model_source
    })


@app.route("/api/models/kill", methods=["POST"])
def api_models_kill():
    launcher = _get_launcher()
    if not launcher:
        return jsonify({"error": "launcher not available"}), 500
    try:
        launcher.shutdown_all()
        return jsonify({"status": "killed"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/run", methods=["POST"])
def api_run():
    data = request.get_json(force=True, silent=True) or request.form
    project_path = data.get("path", "")
    task = data.get("task", "trova e correggi eventuali bug")
    entrypoint = data.get("entrypoint") or None
    max_attempts = int(data.get("max_attempts", 3))
    max_seconds = int(data.get("max_seconds", 300))

    if not project_path:
        return jsonify({"error": "missing path"}), 400

    run_id = datetime.now().strftime("run_%Y%m%d_%H%M%S")

    def _bg():
        try:
            def sse_callback(msg, level):
                print(f"[SSE][{level}] {msg}")

            with Orchestrator(
                config_path="config/settings.json",
                project_path=project_path,
                sse_callback=sse_callback
            ) as orch:
                with runs_lock:
                    active_runs[run_id] = orch
                try:
                    orch.run(
                        task=task,
                        project_path=project_path,
                        entrypoint=entrypoint,
                        max_attempts=max_attempts,
                        max_seconds=max_seconds,
                        run_id=run_id
                    )
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

    return jsonify({"run_id": run_id, "status": "started"})


@app.route("/api/stop", methods=["POST"])
def api_stop():
    data = request.get_json(force=True, silent=True) or {}
    run_id = data.get("run_id")
    if not run_id:
        return jsonify({"error": "missing run_id"}), 400

    with runs_lock:
        orch = active_runs.get(run_id)

    if orch:
        orch.stop()
        return jsonify({"status": "stop_requested", "run_id": run_id})
    return jsonify({"error": "run not found or already finished"}), 404


@app.route("/api/autocomplete", methods=["POST"])
def api_autocomplete():
    data = request.get_json(force=True, silent=True) or {}
    code = data.get("code", "")
    if not code:
        return jsonify({"suggestion": ""})

    try:
        from devin.ai.autocomplete import Autocomplete
        auto = Autocomplete()
        suggestion = auto.suggest(code)
        return jsonify({"suggestion": suggestion or ""})
    except Exception as e:
        return jsonify({"suggestion": "", "error": str(e)})


# ============================================================
# CHAT API
# ============================================================

def _detect_mode(message):
    """Rileva se la domanda richiede reasoning o coding."""
    msg_lower = message.lower()
    coding_keywords = [
        "code", "codice", "python", "function", "def ", "class ",
        "bug", "fix", "patch", "diff", "write a", "scrivi", "implementa",
        "crea una funzione", "crea una classe", "refactor", "debug",
        "syntax", "import ", "error", "exception", "traceback"
    ]
    reasoning_keywords = [
        "explain", "spiega", "why", "perché", "how does", "come funziona",
        "architecture", "design", "pattern", "best practice", "approccio",
        "strategia", "piano", "analizza", "compare", "confronta"
    ]
    
    coding_score = sum(1 for k in coding_keywords if k in msg_lower)
    reasoning_score = sum(1 for k in reasoning_keywords if k in msg_lower)
    
    if coding_score > reasoning_score:
        return "coder"
    elif reasoning_score > coding_score:
        return "reasoning"
    # Default: se contiene codice o backtick, vai coder
    if "```" in message or "    " in message[:50]:
        return "coder"
    return "reasoning"


@app.route("/api/chat", methods=["POST"])
def api_chat():
    data = request.get_json(force=True, silent=True) or {}
    message = data.get("message", "").strip()
    mode = data.get("mode", "auto")
    
    if not message:
        return jsonify({"error": "empty message"}), 400
    
    selected_mode = _detect_mode(message) if mode == "auto" else mode
    
    # Assicurati che i modelli siano up
    launcher = _get_launcher()
    if launcher:
        launcher.ensure_models()
    
    def generate_sse():
        ai = AIClient()
        messages = [{"role": "user", "content": message}]
        
        # Meta come primo evento
        model_name = "qwen3-14b" if selected_mode == "reasoning" else "qwen2.5-coder-7b"
        yield f"event: meta\ndata: {json.dumps({'mode': selected_mode, 'model': model_name})}\n\n"
        
        token_count = 0
        start_time = time.time()
        last_flush = start_time
        
        try:
            for chunk in ai.stream(messages, mode=selected_mode):
                token_count += len(chunk)  # chunk può contenere più token
                yield f"data: {json.dumps({'token': chunk})}\n\n"
                
                # Flush forzato ogni 500ms per evitare buffering Nagle
                now = time.time()
                if now - last_flush > 0.5:
                    yield ""  # SSE keep-alive, forza flush
                    last_flush = now
                    
            elapsed = time.time() - start_time
            tps = round(token_count / elapsed, 1) if elapsed > 0 else 0
            yield f"event: done\ndata: {json.dumps({'tokens': token_count, 'tps': tps, 'elapsed': round(elapsed, 1)})}\n\n"
            
        except Exception as e:
            yield f"event: error\ndata: {json.dumps({'error': str(e)})}\n\n"
    
    return Response(
        stream_with_context(generate_sse()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Content-Type": "text/event-stream",
        }
    )

@app.route("/api/runs")
def api_runs():
    return jsonify(_list_runs())


@app.route("/api/run/<run_id>/log")
def api_run_log(run_id):
    log_path = LOG_DIR / f"{run_id}.log"
    if not log_path.exists():
        return jsonify({"error": "not found"}), 404
    return jsonify({
        "run_id": run_id,
        "content": log_path.read_text(encoding="utf-8", errors="ignore")
    })


@app.route("/stream/<run_id>")
def stream_log(run_id):
    log_path = LOG_DIR / f"{run_id}.log"

    def generate():
        for _ in range(20):
            if log_path.exists():
                break
            time.sleep(0.5)
            wait_payload = json.dumps({"type": "wait", "msg": "Waiting for log file..."})
            yield f"data: {wait_payload}\n\n"
        else:
            err_payload = json.dumps({"type": "error", "msg": "Log file not found"})
            yield f"data: {err_payload}\n\n"
            return

        with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
            f.seek(0, 2)
            while True:
                line = f.readline()
                if not line:
                    time.sleep(0.3)
                    continue
                payload = json.dumps({"type": "log", "line": line.rstrip("\n")})
                yield f"data: {payload}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no"
        }
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True, threaded=True, use_reloader=False)