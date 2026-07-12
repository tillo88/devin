import re
import subprocess
from pathlib import Path
from devin.engine.shell import run_shell

# Nomi import != nome pacchetto pip (i casi comuni). Fallback: usa il nome import.
_PIP_ALIASES = {
    "cv2": "opencv-python",
    "PIL": "pillow",
    "yaml": "pyyaml",
    "bs4": "beautifulsoup4",
    "sklearn": "scikit-learn",
    "dotenv": "python-dotenv",
    "dateutil": "python-dateutil",
    "OpenSSL": "pyopenssl",
    "Crypto": "pycryptodome",
    "serial": "pyserial",
    "usb": "pyusb",
}
_MODULE_RE = re.compile(r"No module named ['\"]([\w\.]+)['\"]")


def _find_likely_entrypoint(sandbox_path: Path):
    """
    FIX: quando ci sono PIU' file .py e nessun main.py/entrypoint esplicito,
    prima si arrendeva subito ("no entrypoint trovato") a meno che non ci fosse
    ESATTAMENTE un solo file .py in tutto il progetto — capitava spesso con
    progetti scaffoldati a 2+ file (es. calculator.py + calculator_logic.py).

    Euristica aggiuntiva: cerca tra i file .py di primo livello (non in
    sottocartelle tipo test/, venv/) quello che contiene un blocco
    `if __name__ == "__main__"` — segnale forte che quel file e' pensato per
    essere eseguito direttamente, anche se ce ne sono altri (moduli di supporto).
    Se ne trova esattamente uno, lo usa. Se ne trova piu' di uno o zero, non
    decide (ambiguo) e lascia che il chiamante segnali l'errore come prima.
    """
    candidates = []
    for f in sandbox_path.glob("*.py"):
        try:
            content = f.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        if "__main__" in content and "if __name__" in content:
            candidates.append(f)

    if len(candidates) == 1:
        return candidates
    return []


def run_project(sandbox_path, entrypoint=None, args=None):
    sandbox_path = Path(sandbox_path).resolve()
    args = args or []

    req_file = sandbox_path / "requirements.txt"
    if req_file.exists():
        print("📦 Trovato requirements.txt, installo dipendenze...")
        setup_result = run_shell(
            f"pip install -r {req_file} --break-system-packages -q",
            cwd=sandbox_path,
            timeout=120
        )
        if not setup_result["success"]:
            print(f"⚠️ Installazione dipendenze fallita: {setup_result['stderr']}")

    candidates = []

    if entrypoint:
        candidates = list(sandbox_path.rglob(entrypoint))

    if not candidates:
        candidates = list(sandbox_path.rglob("main.py"))

    if not candidates:
        py_files = list(sandbox_path.glob("*.py"))
        if len(py_files) == 1:
            candidates = py_files

    if not candidates:
        # FIX: fallback euristico prima di arrendersi (vedi _find_likely_entrypoint)
        candidates = _find_likely_entrypoint(sandbox_path)
        if candidates:
            print(f"ℹ️ Entrypoint non specificato, individuato per euristica: {candidates[0].name}")

    if candidates:
        f = candidates[0]
        try:
            proc = subprocess.run(
                ["python3", str(f), *args],
                cwd=str(sandbox_path),
                capture_output=True,
                text=True
            )
            # AUTO-INSTALL dipendenze mancanti (2026-07-10): il Coder scrive
            # `import pint` ma spesso NON aggiorna requirements.txt, quindi il
            # codice si applica e gira ma esplode su ModuleNotFoundError. Invece
            # di bruciare i retry rigenerando codice gia' corretto, installiamo il
            # pacchetto e rilanciamo. A catena (una dep alla volta), con guardia
            # anti-loop se il nome import != nome pip e non e' tra gli alias noti.
            seen = set()
            for _ in range(5):
                if proc.returncode == 0:
                    break
                m = _MODULE_RE.search(proc.stderr or "")
                if not m:
                    break
                mod = m.group(1).split(".")[0]
                if mod in seen:
                    break  # gia' provato: install "riuscito" ma import ancora KO (alias mancante)
                seen.add(mod)
                pkg = _PIP_ALIASES.get(mod, mod)
                print(f"📦 ModuleNotFoundError '{mod}' → pip install '{pkg}'...")
                inst = run_shell(
                    f"pip install {pkg} --break-system-packages -q",
                    cwd=str(sandbox_path), timeout=180)
                if not inst.get("success"):
                    print(f"⚠️ install '{pkg}' fallito: {(inst.get('stderr') or '')[:200]}")
                    break
                proc = subprocess.run(
                    ["python3", str(f), *args],
                    cwd=str(sandbox_path), capture_output=True, text=True)
            return proc
        except Exception as e:
            return subprocess.CompletedProcess(
                args=["python3", str(f)],
                returncode=1,
                stdout="",
                stderr=f"Runner exception: {type(e).__name__}: {e}"
            )

    return subprocess.CompletedProcess(
        args=[],
        returncode=1,
        stdout="",
        stderr="no entrypoint trovato"
    )


class RunnerResult:
    def __init__(self, success: bool, error: str):
        self.success = success
        self.error = error


class Runner:
    def __init__(self):
        pass

    def run(self, sandbox_path: str, entrypoint: str = None) -> RunnerResult:
        """Esegue il progetto e mappa l'output nel formato richiesto."""
        try:
            proc = run_project(sandbox_path, entrypoint=entrypoint)
            success = proc.returncode == 0
            error_msg = proc.stderr if not success else ""
            return RunnerResult(success=success, error=error_msg)
        except Exception as e:
            return RunnerResult(success=False, error=f"Runner.run() exception: {type(e).__name__}: {e}")
