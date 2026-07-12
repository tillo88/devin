"""
state_persistence.py - Persistenza stato orchestratore per recovery da crash.

Salva lo stato dopo ogni step del loop principale in un file JSON atomico.
Al riavvio, l'orchestratore può riprendere da dove era interrotto.

Stato salvato:
- task: descrizione del task
- attempt: numero tentativo corrente (0-based)
- last_error: ultimo errore / feedback del critic
- last_patch: ultima patch generata
- plan: piano step-by-step (dict)
- context_length: lunghezza contesto
- max_retries: max tentativi configurati
- step: step corrente (coder_done, patcher_done, critic_done)
- final_status: success/failed/timeout/stopped (se completato)
- model_source: origine modelli usati
"""

import json
import os
import time
from pathlib import Path
from typing import Dict, Any, Optional
from datetime import datetime


class StatePersistence:
    """Gestisce il salvataggio e il ripristino dello stato dell'orchestratore."""

    def __init__(self, project_path: str, run_id: Optional[str] = None):
        self.project_path = Path(project_path).resolve()
        self.run_id = run_id or datetime.now().strftime("run_%Y%m%d_%H%M%S")
        self.state_dir = self.project_path / ".devin_state"
        self.state_file = self.state_dir / f"{self.run_id}.json"

        # Crea directory stato se non esiste
        self.state_dir.mkdir(parents=True, exist_ok=True)

    def save(self, state: Dict[str, Any]) -> None:
        """Salva lo stato corrente su disco in modo atomico."""
        state["_saved_at"] = datetime.now().isoformat()
        state["_run_id"] = self.run_id
        state["_project_path"] = str(self.project_path)

        # Scrivi su file temporaneo poi rinomina per atomicità
        temp_file = self.state_file.with_suffix(".tmp")
        try:
            with open(temp_file, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2, ensure_ascii=False, default=str)
            # Atomic rename: garantisce che il file sia sempre valido
            temp_file.replace(self.state_file)
        except Exception as e:
            print(f"[StatePersistence] Errore salvataggio: {e}")

    def load(self) -> Optional[Dict[str, Any]]:
        """Carica lo stato salvato se esiste."""
        if not self.state_file.exists():
            return None
        try:
            with open(self.state_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"[StatePersistence] Errore caricamento: {e}")
            return None

    def load_latest(self) -> Optional[Dict[str, Any]]:
        """Carica lo stato più recente per questo progetto."""
        if not self.state_dir.exists():
            return None

        state_files = sorted(
            self.state_dir.glob("run_*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True
        )

        for f in state_files:
            try:
                with open(f, "r", encoding="utf-8") as fobj:
                    state = json.load(fobj)
                # Verifica che sia dello stesso progetto
                if state.get("_project_path") == str(self.project_path):
                    self.run_id = state.get("_run_id", self.run_id)
                    self.state_file = f
                    return state
            except Exception:
                continue
        return None

    def cleanup(self, max_age_hours: int = 24) -> int:
        """Rimuove stati più vecchi di max_age_hours. Ritorna numero file rimossi."""
        if not self.state_dir.exists():
            return 0

        removed = 0
        cutoff = datetime.now().timestamp() - (max_age_hours * 3600)

        for f in self.state_dir.glob("run_*.json"):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
                    removed += 1
            except Exception:
                pass
        return removed

    def delete(self) -> None:
        """Elimina il file stato corrente (chiamare dopo successo)."""
        if self.state_file.exists():
            self.state_file.unlink()

    def get_resume_info(self) -> Optional[Dict[str, Any]]:
        """
        Ritorna informazioni per il resume se lo stato permette di riprendere.
        Ritorna None se non c'è nulla da riprendere.
        """
        state = self.load_latest()
        if not state:
            return None

        # Non riprendere se il run era già completato
        final_status = state.get("final_status")
        if final_status in ("success", "failed", "timeout", "stopped"):
            return None

        return {
            "run_id": state.get("_run_id"),
            "task": state.get("task"),
            "attempt": state.get("attempt", 0),
            "last_error": state.get("last_error"),
            "last_patch": state.get("last_patch"),
            "plan": state.get("plan"),
            "context_length": state.get("context_length", 0),
            "model_source": state.get("model_source"),
            "saved_at": state.get("_saved_at"),
            "can_resume": state.get("attempt", 0) < state.get("max_retries", 3)
        }
