import sys
from pathlib import Path

# Ancorato alla posizione di QUESTO file, non alla CWD del processo — stesso
# principio applicato a config/settings.json e logs/ nel resto del progetto.
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# FIX: prima importava devin.main.main() -> devin/ui/app.py, cioe' la vecchia UI
# Tkinter legacy (v19), NON la dashboard FastAPI su cui e' stato fatto tutto il
# lavoro (Monaco+autocomplete, Zero-Shot Scaffolding, Self-Healing, chat, ecc.).
# Il doppio-click su questo file deve aprire QUELLA dashboard, non una finestra
# Tkinter scollegata dal resto del progetto.
from devin.ui.fast_app import run_server

if __name__ == "__main__":
    run_server()
