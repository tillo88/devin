"""
DEVIN AI IDE - Orchestrator
Loop principale: Planner -> Coder -> Patcher -> Runner -> Critic
Con auto-start modelli locali via LocalModelLauncher.

TASK 13: Persistenza stato per recovery da crash.
FASE 1: Serializzazione VRAM (swap Planner/Coder), integrazione VectorStore semantic search
FASE 3: Self-Healing (Critic su errori di tool, non solo di runner) + Zero-Shot Scaffolding
"""

import os
import json
import time
import shutil
import traceback
from pathlib import Path
from typing import Dict, List, Optional, Any
from datetime import datetime

from devin.ai.client import AIClient
from devin.ai.local_model_launcher import (
    LocalModelLauncher, LauncherStatus,
    swap_model, get_vram_status, start_vram_watchdog
)
from devin.core.context_engine import ContextEngine
from devin.core.context_retriever import ContextRetriever
from devin.core.state_persistence import StatePersistence
from devin.agents.planner import Planner
from devin.agents.coder import Coder
from devin.agents.critic import Critic
from devin.engine.patcher import Patcher
from devin.engine.runner import Runner
from devin.engine.git_ops import GitOps
from devin.memory.vector_store import VectorStore

LOG_DIR = Path(__file__).resolve().parents[2] / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

# Stesso principio di client.py: default ancorato alla posizione del file, non alla CWD.
_DEFAULT_CONFIG_PATH = str(Path(__file__).resolve().parents[2] / "config" / "settings.json")


class Orchestrator:
    MAX_RETRIES = 3

    def __init__(
        self,
        config_path: str = _DEFAULT_CONFIG_PATH,
        project_path: str = None,
        sse_callback=None
    ):
        self.config_path = config_path
        self.project_path = project_path or os.getcwd()
        self.sse_callback = sse_callback
        self._should_stop = False
        self._state_persistence = None
        self._degraded_mode = False  # True se il rig (primario) è down e giriamo su locale

        with open(config_path, "r") as f:
            self.config = json.load(f)

        # FASE 1: Configurazione serializzazione VRAM
        self.serialize_vram = self.config.get("models", {}).get("serialize_vram_heavy_models", False)
        self.vram_swap_threshold_mb = self.config.get("models", {}).get("vram_swap_threshold_mb", 2048)

        self.model_launcher = None
        self.model_status = None
        try:
            self.model_launcher = LocalModelLauncher.from_config(
                config_path,
                sse_callback=sse_callback
            )
            self._log("LocalModelLauncher initialized", "info")
        except Exception as e:
            self._log(f"LocalModelLauncher init warning: {e}", "warning")

        self.context_engine = ContextEngine(
            max_chars=self.config.get("context", {}).get("max_chars", 100000)
        )
        self.context_retriever = ContextRetriever(
            enabled=self.config.get("context", {}).get("semantic_search_enabled", True)
        )

        self.ai_client = AIClient()
        self.planner = Planner(self.ai_client)
        self.coder = Coder(self.ai_client)
        self.critic = Critic(self.ai_client)

        self.patcher = Patcher()
        self.runner = Runner()
        self.git_ops = GitOps(self.project_path)
        self.vector_store = VectorStore()

        self._log("Orchestrator initialized", "info")

    def _log(self, message: str, level: str = "info"):
        if self.sse_callback:
            try:
                self.sse_callback(message, level)
            except Exception:
                pass
        print(f"[{level.upper()}] {message}")

    def stop(self):
        """Richiede l'arresto del run in corso."""
        self._should_stop = True
        self._log("Stop requested by user", "warning")

    def _sync_sandbox_to_project(self, sandbox_path: str, project_path: str):
        """Copia i file .py dalla sandbox al progetto originale."""
        sandbox = Path(sandbox_path)
        project = Path(project_path)

        for src_file in sandbox.rglob("*.py"):
            rel_parts = src_file.relative_to(sandbox).parts
            if "workspace" in rel_parts or "venv" in rel_parts or "__pycache__" in rel_parts:
                continue

            rel_path = src_file.relative_to(sandbox)
            dest_file = project / rel_path

            if dest_file.exists():
                current = dest_file.read_text(encoding="utf-8", errors="ignore")
                new = src_file.read_text(encoding="utf-8", errors="ignore")
                if current == new:
                    continue

            dest_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(src_file), str(dest_file))

    # ============================================================
    # SELF-HEALING (Regola di sviluppo #2)
    # Il Critic tenta l'auto-correzione degli errori di TOOL (Coder/Patcher/
    # GitOps/scaffold write) PRIMA che l'errore raw venga propagato al retry
    # cieco o notificato all'utente. Se il Critic stesso è irraggiungibile
    # (es. rig down durante il fallimento), fallback silenzioso all'errore raw:
    # non deve mai far crashare l'orchestratore.
    # ============================================================

    def _self_heal(
        self,
        stage: str,
        error: str,
        patch: str = "",
        context: str = "",
        sandbox_files: Optional[Dict[str, str]] = None
    ) -> str:
        self._log(f"[SELF-HEAL] {stage} failed: {error} — invio al Critic per auto-correzione", "warning")
        context = context + self._maybe_web_reference(error)
        try:
            critique = self.critic.analyze(error, patch, context, sandbox_files=sandbox_files)
            self._log(f"[SELF-HEAL] Critic feedback: {critique.feedback[:200]}", "info")
            return critique.feedback
        except Exception as e:
            self._log(f"[SELF-HEAL] Critic offline ({e}), fallback a errore raw", "error")
            return error

    def _maybe_web_reference(self, error: str) -> str:
        """Ricerca web AL SERVIZIO DEL CODING (2026-07-10): se l'errore e'
        "cercabile" (modulo mancante, API cambiata, versioni incompatibili —
        vedi SEARCHABLE_ERROR_PATTERNS in devin/ai/web_search.py), cerca la
        prima riga dell'errore e ritorna un blocco di riferimento REALE da
        accodare al contesto del Critic — invece di farlo ragionare a memoria
        su API che magari sono cambiate. Max N ricerche per run (config
        web_search.agent_search.max_per_run) per non trasformare il debug in
        navigazione. Fail-soft: stringa vuota su qualsiasi problema."""
        try:
            from devin.ai.web_search import is_searchable_error, search_coding_context
            config = getattr(self.ai_client, "config", {}) if hasattr(self, "ai_client") else {}
            agent_cfg = config.get("web_search", {}).get("agent_search", {})
            if not agent_cfg.get("enabled", True):
                return ""
            max_per_run = int(agent_cfg.get("max_per_run", 2))
            done = getattr(self, "_web_searches_done", 0)
            if done >= max_per_run or not is_searchable_error(error):
                return ""
            first_line = (error or "").strip().splitlines()[0][:140]
            self._log(f"[WEB-REF] Errore cercabile, consulto il web: {first_line}", "info")
            block = search_coding_context(f"python {first_line}", config)
            self._web_searches_done = done + 1
            if not block:
                return ""
            return ("\n\nWEB REFERENCE (cercato ora per questo errore — usalo come "
                    f"fonte, non inventare API):\n{block}")
        except Exception as e:
            self._log(f"[WEB-REF] ricerca fallita (proseguo senza): {e}", "warning")
            return ""

    # ============================================================
    # FASE 1: SERIALIZZAZIONE VRAM
    # ============================================================

    def _check_vram_and_swap(self, needed_alias: str, release_alias: str = None):
        """
        Se serialize_vram è abilitato e la VRAM libera è sotto soglia,
        killa il modello 'release_alias' per liberare spazio per 'needed_alias'.
        Rilevante solo per il locale (backup/chat): il rig primario (51GB) non
        necessita di serializzazione per il modello MoE scelto.

        Args:
            needed_alias: 'planner' o 'coder' — il modello che serve ora
            release_alias: alias da rilasciare, o None per auto-detect
        """
        if not self.serialize_vram:
            return True

        vram = get_vram_status()
        free_mb = vram.get("free_mb")

        if free_mb is None:
            self._log("VRAM check non disponibile, proseguo senza swap", "warning")
            return True

        if free_mb >= self.vram_swap_threshold_mb:
            return True  # Abbastanza VRAM, nessun swap necessario

        # Determina quale rilasciare
        if release_alias is None:
            release_alias = "planner" if needed_alias == "coder" else "coder"

        self._log(
            f"VRAM low ({free_mb}MB free < {self.vram_swap_threshold_mb}MB). "
            f"Swapping: kill '{release_alias}' to load '{needed_alias}'",
            "warning"
        )

        try:
            from devin.ai.local_model_launcher import kill_server_on_port, MODELS
            kill_server_on_port(MODELS[release_alias]["port"])
            self._log(f"Released '{release_alias}' from VRAM", "info")
            time.sleep(3)  # Attendi deallocazione VRAM
            return True
        except Exception as e:
            self._log(f"Swap failed: {e}", "error")
            return False

    def ensure_models(self) -> LauncherStatus:
        if not self.model_launcher:
            self._log("No model launcher, skipping", "warning")
            return LauncherStatus(
                rig_available=False, rig_host="", rig_ports=[],
                local_running={}, model_source="unavailable",
                errors=["Launcher not initialized"]
            )

        self._log("Checking model availability...", "info")
        status = self.model_launcher.ensure_models()
        self.model_status = status
        self.ai_client.refresh()

        # Rig = hardware primario. Se non risponde, siamo in degraded mode:
        # il locale (16GB, backup/chat) resta disponibile ma va segnalato.
        rig_up = bool(getattr(self.ai_client, "remote_coder_ok", False) and
                      getattr(self.ai_client, "remote_reasoning_ok", False))
        self._degraded_mode = not rig_up

        if rig_up:
            self._log(f"Rig primario OK ({self.ai_client.remote_host}) — uso modelli rig", "success")
        elif status.model_source == "local":
            self._log(
                "⚠️ Rig esterno (primario) non disponibile. Uso modelli locali di backup "
                "(16GB VRAM) — sconsigliato per scaffolding/debug pesante, ok per chat.",
                "warning"
            )
        else:
            self._log("No models available!", "error")

        return status

    def get_model_status(self) -> LauncherStatus:
        if self.model_launcher:
            return self.model_launcher.get_status()
        return LauncherStatus(
            rig_available=False, rig_host="", rig_ports=[],
            local_running={}, model_source="unavailable", errors=[]
        )

    def shutdown_models(self):
        if self.model_launcher:
            self.model_launcher.shutdown_all()
            self._log("Models shutdown complete", "info")

    # ============================================================
    # MODALITÀ 2 — ZERO-SHOT SCAFFOLDING
    # Creazione progetto da zero ESCLUSIVAMENTE via tool (scrittura file diretta),
    # nessuna diff pipeline. Feedback SSE continuo file-per-file (Chat First).
    # ============================================================

    def run_scaffold(self, task: str, project_path: str = None, run_id: str = None) -> Dict[str, Any]:
        if project_path:
            self.project_path = project_path
        start_time = time.time()

        Path(self.project_path).mkdir(parents=True, exist_ok=True)
        self._log("Zero-Shot Scaffolding avviato", "info")

        model_status = self.ensure_models()
        if model_status.model_source == "unavailable":
            self._log("Nessun modello disponibile, impossibile procedere", "error")
            return {"success": False, "error": "No models available", "duration": time.time() - start_time}

        if self._degraded_mode:
            self._log(
                "⚠️ Scaffolding in degraded mode (rig down, uso locale). "
                "Qualità/velocità ridotte su progetti multi-file.",
                "warning"
            )

        file_plan = self.planner.plan_scaffold(task)
        if not file_plan:
            self._log("Planner non ha prodotto un piano file valido (JSON non parsabile o task ambiguo)", "error")
            return {"success": False, "error": "empty file plan", "duration": time.time() - start_time}

        self._log(f"Piano: {len(file_plan)} file da creare", "info")

        written = []
        failed = []
        running_context = ""

        for i, item in enumerate(file_plan, 1):
            if self._should_stop:
                self._log("Scaffolding interrotto dall'utente", "warning")
                break

            fname, spec = item["filename"], item["spec"]
            self._log(f"[{i}/{len(file_plan)}] Creating {fname}...", "info")

            try:
                content = self.coder.generate_file(fname, spec, project_context=running_context)
                if not content.strip():
                    raise ValueError(f"Coder ha restituito contenuto vuoto per {fname}")

                target = Path(self.project_path) / fname
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")

                written.append(fname)
                running_context += f"\n# FILE: {fname}\n{content[:1500]}\n"
                self._log(f"✓ {fname} ({len(content)} chars)", "success")

            except Exception as e:
                # Self-Healing: prova auto-correzione via Critic prima di segnare fallito
                healed_feedback = self._self_heal(f"scaffold_file:{fname}", str(e), context=spec)
                self._log(f"✗ {fname} fallito — feedback Critic: {healed_feedback[:150]}", "error")
                failed.append({"filename": fname, "error": str(e), "critic_feedback": healed_feedback})

        if written:
            try:
                self.git_ops.commit("", f"Zero-Shot Scaffold: {task}")
                self._log("Progetto committato", "info")
            except Exception as e:
                self._log(f"Git commit warning: {e}", "warning")

        result = {
            "success": bool(written) and not failed,
            "files_written": written,
            "files_failed": failed,
            "total_planned": len(file_plan),
            "duration": time.time() - start_time,
            "degraded_mode": self._degraded_mode,
        }
        self._log(f"Scaffolding completato: {len(written)}/{len(file_plan)} file scritti", "success" if not failed else "warning")
        return result

    # ============================================================
    # MODALITÀ 1 — MANTENIMENTO
    # Loop principale: Planner -> Coder -> Patcher -> Runner -> Critic
    # ============================================================

    def run_from_conversation(self, conversation_text: str, project_path: str = None,
                               run_id: str = None) -> Dict[str, Any]:
        """'Realizza dalla chat' su progetto CON codice (2026-07-10): il metodo
        era referenziato da fast_app.py ma non e' mai arrivato in questo file
        (perso in una delle consegne zip) — ogni click finiva in
        [FATAL] 'Orchestrator' object has no attribute 'run_from_conversation'.

        Implementazione: wrapper su run() con la conversazione come specifica.
        Il Planner ri-pianifica leggendo la discussione (decisioni e correzioni
        piu' RECENTI pesano di piu': si tiene la coda, il ctx locale e' 8192).
        Footer 'status: X' garantito dai return path di run()."""
        MAX_CONV_CHARS = 6000
        tail = conversation_text[-MAX_CONV_CHARS:]
        if len(conversation_text) > MAX_CONV_CHARS:
            tail = "[...conversazione precedente troncata...]\n" + tail
            self._log(f"Conversazione lunga ({len(conversation_text)} char): uso gli ultimi {MAX_CONV_CHARS}", "info")

        # DISTILLAZIONE (2026-07-10): la conversazione grezza (tabelle markdown,
        # esempi, divagazioni, fraintendimenti corretti) mandava in tilt il
        # Planner — visto sul campo: 29 step e un diff da 9146 righe per
        # "integra Pint". Un passaggio di reasoning la riduce a un task
        # operativo corto; la coda grezza resta solo come fallback.
        task = None
        try:
            distill_msgs = [
                {"role": "system", "content":
                    "Distilla dalla conversazione un TASK di sviluppo conciso e operativo. "
                    "Massimo 8 righe: cosa implementare/correggere, in quali file, vincoli. "
                    "Le decisioni piu' RECENTI prevalgono su quelle vecchie (le correzioni "
                    "dell'utente annullano i fraintendimenti precedenti). "
                    "Rispondi SOLO col task, niente premesse, niente codice."},
                {"role": "user", "content": tail},
            ]
            distilled = (self.ai_client.local(distill_msgs, mode="reasoning", timeout=90) or "").strip()
            if len(distilled) > 20:
                task = f"TASK (distillato dalla conversazione in chat):\n{distilled[:2500]}"
                self._log(f"Task distillato: {distilled[:250]}", "info")
        except Exception as e:
            self._log(f"Distillazione fallita ({e}), uso la conversazione grezza", "warning")

        if not task:
            task = ("Applica al progetto le modifiche discusse/concordate in questa conversazione "
                    "utente-assistente. Le decisioni e correzioni piu' recenti hanno precedenza "
                    "su quelle vecchie.\n\n=== CONVERSAZIONE ===\n" + tail)
        return self.run(task=task, project_path=project_path, run_id=run_id)

    def _small_project(self, max_lines: int) -> bool:
        """True se OGNI file di codice del progetto è sotto max_lines righe.
        In quel caso conviene la modalità WHOLE-FILE (il Coder riscrive i file
        interi, niente unified diff): più affidabile sui modelli piccoli. Su file
        grandi resta il diff (riscrivere 2000 righe è spreco e rischio)."""
        try:
            files = self.context_engine.collect_project_files()
        except Exception:
            return False
        if not files:
            return False
        for f in files:
            if (f.get("content", "").count("\n") + 1) > max_lines:
                return False
        return True

    def run(
        self,
        task: str,
        project_path: str = None,
        entrypoint: str = None,
        max_attempts: int = None,
        max_seconds: int = None,
        run_id: str = None
    ) -> Dict[str, Any]:
        # === TASK 13: Inizializza persistenza stato ===
        self._state_persistence = StatePersistence(self.project_path, run_id)

        # Prova a riprendere da stato precedente
        resume_info = self._state_persistence.get_resume_info()
        if resume_info and resume_info.get("can_resume"):
            self._log(
                f"Resuming previous run {resume_info['run_id']} "
                f"(attempt {resume_info['attempt']+1}/{resume_info.get('max_retries', 3)})",
                "warning"
            )

        if project_path:
            self.project_path = project_path

        start_time = time.time()
        logs = []
        max_retries = max_attempts or self.MAX_RETRIES

        # === TASK 13: Se c'è uno stato da riprendere, carica i dati salvati ===
        plan = None
        context = ""
        last_error = None
        attempt = 0

        if resume_info:
            task = resume_info.get("task", task)
            attempt = resume_info.get("attempt", 0)
            last_error = resume_info.get("last_error")
            saved_plan = resume_info.get("plan")
            if saved_plan:
                from devin.agents.planner import Plan
                plan = Plan(
                    steps=saved_plan.get("steps", []),
                    raw_response=saved_plan.get("raw_response", "")
                )

        log_file = None
        if run_id:
            log_file = LOG_DIR / f"{run_id}.log"
            log_file.write_text(f"Run started: {run_id}\nTask: {task}\n", encoding="utf-8")

        def log(msg, level="info"):
            logs.append({"time": time.time() - start_time, "level": level, "msg": msg})
            self._log(msg, level)
            # FIX: self._log() sopra gia' inoltra a self.sse_callback, che in fast_app.py
            # scrive esattamente su LOG_DIR/{run_id}.log (stesso path di log_file qui sotto).
            # Scrivere ANCHE qui duplicava ogni riga nel file (e quindi nella dashboard,
            # che lo streamma via /stream/{run_id}). Scriviamo diretto SOLO come fallback
            # per chi istanzia Orchestrator con run_id ma senza sse_callback.
            if log_file and not self.sse_callback:
                with open(log_file, "a", encoding="utf-8") as f:
                    f.write(f"[{level.upper()}] {msg}\n")

        # FIX: helper UNICO per il footer 'status: X' del log file, chiamato da OGNI
        # return path di run() (prima 2 su 6 non lo scrivevano affatto — "No models
        # available" e "Planner failed" — mentre gli altri 4 lo scrivevano ad-hoc E
        # fast_app.py lo riscriveva UNA SECONDA VOLTA dopo che run() tornava, causando
        # "status: failed" duplicato in fondo al log). Ora e' scritto esattamente una
        # volta, qui, per qualunque esito — fast_app.py non deve piu' scriverlo.
        def write_status_footer(status: str):
            if log_file:
                with open(log_file, "a", encoding="utf-8") as f:
                    f.write(f"\nstatus: {status}\n")

        # === TASK 13: Helper per salvare stato ===
        def save_state(**kwargs):
            """Salva lo stato corrente su disco."""
            state = {
                "task": task,
                "attempt": attempt,
                "last_error": last_error,
                "last_patch": kwargs.get("patch", ""),
                "plan": plan.to_dict() if plan else None,
                "context_length": len(context),
                "max_retries": max_retries,
                "model_source": getattr(self.model_status, "model_source", "unknown"),
            }
            state.update(kwargs)
            self._state_persistence.save(state)

        log("DEVIN starting...", "info")
        model_status = self.ensure_models()
        if model_status.model_source == "unavailable":
            log("No AI models available. Cannot proceed.", "error")
            save_state(final_status="failed")
            write_status_footer("failed")
            return {
                "success": False,
                "error": "No models available",
                "logs": logs,
                "duration": time.time() - start_time
            }


        log(f"Models ready (source: {model_status.model_source})", "success")

        # === FASE 1: SWAP PER PLANNER ===
        if self.serialize_vram:
            self._check_vram_and_swap("planner", release_alias="coder")

        log("Building context...", "info")
        # === TASK 13: Se abbiamo ripreso, usa context salvato ===
        if not context:
            try:
                context = self.context_engine.build(
                    project_path=self.project_path,
                    query=task
                )
                # === FASE 1: VECTOR STORE — indicizza il progetto con persistenza ===
                if self.config.get("context", {}).get("semantic_search_enabled"):
                    try:
                        files = self.context_engine.collect_project_files()
                        # Usa cache persistente in workspace/.devin_cache/
                        cache_dir = Path(self.project_path) / ".devin_cache"
                        cache_dir.mkdir(parents=True, exist_ok=True)
                        self.vector_store.index_project(
                            self.project_path, files,
                            cache_path=cache_dir / "semantic_index.pkl"
                        )
                        semantic = self.context_retriever.retrieve(task, self.project_path)
                        context = self.context_engine.prioritize(context, semantic, task)
                        log(f"Semantic context: {len(semantic)} chars", "info")
                    except Exception as e:
                        log(f"Semantic search warning: {e}", "warning")
                log(f"Context: {len(context)} chars", "info")
            except Exception as e:
                log(f"Context build warning: {e}", "warning")
                context = ""
        else:
            log(f"Resumed context: {len(context)} chars", "info")

        log("Planner analyzing...", "info")
        # === TASK 13: Se abbiamo ripreso, usa plan salvato ===
        if not plan:
            try:
                plan = self.planner.plan(task, context)
                log(f"Plan: {len(plan.steps)} steps", "info")
                save_state(step="planner_done")
            except Exception as e:
                log(f"Planner failed: {e}", "error")
                save_state(final_status="failed")
                write_status_footer("failed")
                return {"success": False, "error": str(e), "logs": logs, "duration": time.time() - start_time}
        else:
            log(f"Resumed plan: {len(plan.steps)} steps", "info")

        # === Modalità edit: WHOLE-FILE per progetti piccoli, unified diff per i grandi ===
        # Il fallimento sistematico su modelli piccoli è l'unified diff con righe di
        # contesto allucinate (non applicabile). Su progetti piccoli il Coder riscrive
        # i file interi: niente patcher, niente fuzzy, gioca sul punto forte del modello.
        coder_cfg = self.config.get("coder", {}) or {}
        whole_file_enabled = coder_cfg.get("whole_file_enabled", True)
        whole_file_max_lines = int(coder_cfg.get("whole_file_max_lines", 300))
        use_whole_file = whole_file_enabled and self._small_project(whole_file_max_lines)
        if use_whole_file:
            log(f"Edit mode: WHOLE-FILE (progetto piccolo, file <= {whole_file_max_lines} righe) — bypass diff/patcher", "info")
        else:
            log("Edit mode: unified diff", "info")

        while attempt < max_retries:
            # === TIMEOUT CHECK ===
            if max_seconds and (time.time() - start_time) > max_seconds:
                log("Timeout: max_seconds exceeded", "error")
                write_status_footer("timeout")
                save_state(final_status="timeout")
                return {
                    "success": False,
                    "error": "Timeout: max_seconds exceeded",
                    "logs": logs,
                    "duration": time.time() - start_time,
                    "model_source": model_status.model_source
                }

            # === STOP CHECK ===
            if self._should_stop:
                log("Run stopped by user", "warning")
                write_status_footer("stopped")
                save_state(final_status="stopped")
                return {
                    "success": False,
                    "error": "Run stopped by user",
                    "logs": logs,
                    "duration": time.time() - start_time,
                    "model_source": model_status.model_source
                }

            attempt += 1
            log(f"\nAttempt {attempt}/{max_retries}", "info")
            save_state(attempt=attempt, step="attempt_start")

            # === FASE 1: SWAP PER CODER ===
            if self.serialize_vram:
                self._check_vram_and_swap("coder", release_alias="planner")

            log("Coder generating patch...", "info")
            if use_whole_file:
                # === WHOLE-FILE: il Coder riscrive i file interi, niente diff/patcher ===
                try:
                    full_files = self.coder.generate_full_files(plan, context, last_error)
                except Exception as e:
                    log(f"Coder failed: {e}", "error")
                    last_error = self._self_heal("coder", str(e), context=context)
                    save_state(last_error=last_error, step="coder_failed")
                    continue
                # `patch` sintetico (per commit/log/critic): serve solo come riepilogo testuale.
                patch = "\n\n".join(f"### FILE: {p}\n{c}" for p, c in full_files.items())
                total_chars = sum(len(c) for c in full_files.values())
                log(f"Whole-file: {len(full_files)} file, {total_chars} caratteri", "info")
                save_state(patch=patch, step="coder_done")
                if run_id:
                    debug_patch_file = LOG_DIR / f"{run_id}_attempt{attempt}_files.txt"
                    debug_patch_file.write_text(patch or "(nessun file)", encoding="utf-8")
                    log(f"Debug: file interi salvati in {debug_patch_file.name}", "info")
                if not full_files:
                    last_error = ("You returned NO files in the required format. For each file to "
                                  "create or modify, output a line '### FILE: <path>' then a fenced "
                                  "code block with the COMPLETE new file content.")
                    save_state(last_error=last_error, step="coder_no_files")
                    continue

                if self.serialize_vram:
                    vram = get_vram_status()
                    if vram.get("is_critical"):
                        log("VRAM critical before write, releasing coder", "warning")
                        from devin.ai.local_model_launcher import kill_server_on_port, MODELS
                        kill_server_on_port(MODELS["coder"]["port"])

                log("Patcher applying (whole-file)...", "info")
                try:
                    sandbox_path = self.patcher.apply_full_files(full_files, self.project_path)
                    log("File interi scritti nel sandbox", "info")
                    save_state(patch=patch, step="patcher_done")
                except Exception as e:
                    log(f"Whole-file write failed: {e}", "error")
                    last_error = self._self_heal("patcher", str(e), patch=patch, context=context)
                    save_state(last_error=last_error, step="patcher_failed")
                    continue
            else:
                # === UNIFIED DIFF (progetti grandi) ===
                try:
                    patch = self.coder.generate(plan, context, last_error)
                    # FIX metrica (2026-07-10): prima loggava len(patch) come "lines"
                    # ma sono CARATTERI (il "9146 lines" era un diff da ~200 righe).
                    patch_lines = (patch.count("\n") + 1) if patch else 0
                    log(f"Patch: {patch_lines} righe ({len(patch)} caratteri)", "info")
                    save_state(patch=patch, step="coder_done")
                    # DEBUG: salva il diff grezzo COMPLETO per attempt, su file separato.
                    if run_id:
                        debug_patch_file = LOG_DIR / f"{run_id}_attempt{attempt}_patch.diff"
                        debug_patch_file.write_text(patch or "(patch vuota)", encoding="utf-8")
                        log(f"Debug: patch grezza salvata in {debug_patch_file.name}", "info")

                    # GUARDIA diff giganti (2026-07-10): un diff enorme non si
                    # applichera' mai (fuzzy match su centinaia di hunk imprecisi
                    # di un modello piccolo = minuti persi). Meglio rigettarlo subito.
                    if patch_lines > 800:
                        log(f"Patch enorme ({patch_lines} righe): rigettata prima del patcher", "warning")
                        last_error = (
                            f"Your previous diff had {patch_lines} lines — far too large to apply. "
                            "Regenerate a MINIMAL unified diff: touch ONLY the files that must "
                            "change, small hunks with exact context lines, never rewrite whole "
                            "files. If the task is big, implement only the FIRST concrete step.")
                        save_state(last_error=last_error, step="coder_patch_too_big")
                        continue
                except Exception as e:
                    log(f"Coder failed: {e}", "error")
                    # SELF-HEALING: Critic tenta auto-correzione prima del retry cieco
                    last_error = self._self_heal("coder", str(e), context=context)
                    save_state(last_error=last_error, step="coder_failed")
                    continue

                # === FASE 1: SWAP PER PATCHER/RUNNER (non serve GPU) ===
                if self.serialize_vram:
                    vram = get_vram_status()
                    if vram.get("is_critical"):
                        log("VRAM critical before patch, releasing coder", "warning")
                        from devin.ai.local_model_launcher import kill_server_on_port, MODELS
                        kill_server_on_port(MODELS["coder"]["port"])

                log("Patcher applying...", "info")
                try:
                    sandbox_path = self.patcher.apply(patch, self.project_path)
                    log("Patch applied to sandbox", "info")
                    save_state(patch=patch, step="patcher_done")
                except Exception as e:
                    log(f"Patch failed: {e}", "error")
                    # SELF-HEALING: Critic tenta auto-correzione prima del retry cieco
                    last_error = self._self_heal("patcher", str(e), patch=patch, context=context)
                    save_state(last_error=last_error, step="patcher_failed")
                    continue

            log("Runner executing...", "info")
            try:
                result = self.runner.run(sandbox_path, entrypoint=entrypoint)
                if result.success:
                    log("Execution successful!", "success")

                    try:
                        self._sync_sandbox_to_project(sandbox_path, self.project_path)
                        log("Sandbox synced to project", "info")
                    except Exception as e:
                        log(f"Sandbox sync warning: {e}", "warning")

                    try:
                        self.git_ops.commit(patch, task)
                        log("Changes committed", "info")
                    except Exception as e:
                        log(f"Git commit warning: {e}", "warning")

                    final_result = {
                        "success": True,
                        "plan": plan.to_dict(),
                        "patch": patch,
                        "logs": logs,
                        "duration": time.time() - start_time,
                        "model_source": model_status.model_source
                    }
                    save_state(final_status="success", patch=patch)
                    # Pulisci stato dopo successo
                    self._state_persistence.delete()
                    write_status_footer("success")
                    return final_result
                else:
                    log(f"Execution failed: {result.error}", "error")
                    last_error = result.error
                    save_state(last_error=last_error, step="runner_failed")
            except Exception as e:
                log(f"Runner error: {e}", "error")
                log(f"Runner traceback: {traceback.format_exc()}", "error")
                last_error = str(e)
                save_state(last_error=last_error, step="runner_exception")

            # === FASE 1: SWAP PER CRITIC (reasoning) ===
            if self.serialize_vram:
                self._check_vram_and_swap("planner", release_alias="coder")

            log("Critic analyzing...", "info")
            try:
                sandbox_files = {}
                for py_file in Path(sandbox_path).rglob("*.py"):
                    try:
                        rel = str(py_file.relative_to(sandbox_path))
                        sandbox_files[rel] = py_file.read_text(encoding="utf-8", errors="ignore")
                    except Exception:
                        pass

                # Errore di runtime "cercabile" (modulo/API/versioni)? Dai al
                # Critic un riferimento web REALE invece di farlo andare a memoria.
                critique = self.critic.analyze(
                    last_error, patch, context + self._maybe_web_reference(last_error),
                    sandbox_files=sandbox_files)
                log(f"Critic feedback: {critique.feedback[:200]}...", "info")
                last_error = critique.feedback
                save_state(last_error=last_error, step="critic_done")
            except Exception as e:
                log(f"Critic warning: {e}", "warning")

        log("Max retries exceeded", "error")
        write_status_footer("failed")
        save_state(final_status="failed")
        return {
            "success": False,
            "error": f"Max retries exceeded. Last error: {last_error}",
            "logs": logs,
            "duration": time.time() - start_time,
            "model_source": model_status.model_source
        }

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        # NON spegniamo più i modelli automaticamente
        # L'utente li gestisce dalla Web UI
        self._log("Orchestrator finished. Models still running.", "info")
        return False


if __name__ == "__main__":
    print("=" * 60)
    print("DEVIN Orchestrator - Test Mode")
    print("=" * 60)
    try:
        with Orchestrator(config_path=_DEFAULT_CONFIG_PATH) as orch:
            print(f"\nModel status: {orch.get_model_status().to_dict()}")
            print("\nOrchestrator ready. Use orch.run(task) or orch.run_scaffold(task).")
    except Exception as e:
        print(f"\nERROR: {e}")
        traceback.print_exc()
