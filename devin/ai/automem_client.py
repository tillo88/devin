"""
automem_client.py - Client REST per AutoMem (memoria a lungo termine condivisa sul rig).

Contesto (vedi ai-rig-iso-build/docs/DESIGN.md): il servizio AutoMem gira su TUTTI
e 3 i ruoli del rig (devin/hermes/teacher) su localhost:8001, con i dati sul 4°
disco condiviso -> la memoria e' UNA e sopravvive al cambio di ruolo. llama.cpp
non parla MCP: da DEVIN AI IDE si usa l'API REST diretta ("Opzione A").

Uso nella modalita' Progetti:
- recall automatico: prima di ogni risposta in una chat di progetto, le memorie
  rilevanti vengono recuperate e iniettate nel contesto (budget limitato).
- store manuale: bottone "salva in memoria" / endpoint dedicato. Niente store
  automatico di ogni turno: sarebbe rumore, e la selezione di cosa vale la pena
  ricordare e' una decisione (harness futuro potra' automatizzarla).
- tag per progetto (project:<nome>) + tag "devin": l'harness futuro puo'
  filtrare/esportare per progetto, e Hermes vede le stesse memorie via MCP.

FAIL-SOFT BY DESIGN: il rig e' spesso spento (WOL) o in un altro ruolo... no,
AutoMem gira su tutti i ruoli, ma il rig puo' essere SPENTO. Ogni chiamata ha
timeout corto e ogni errore degrada in silenzio (lista vuota / False): la chat
non deve MAI bloccarsi o rompersi perche' la memoria non risponde.

⚠️ NOTA ONESTA da verificare al primo uso reale (rig acceso): i nomi esatti di
endpoint/campi (POST /memory {content,tags,importance}, GET /recall?query=&limit=)
vengono dal riassunto dell'API AutoMem discusso in chat (github.com/verygoodplugins/
automem, docs/API.md) e non sono ancora stati testati contro il servizio vero.
Se divergono, questo e' l'UNICO file da correggere.
"""

import json
import time
from pathlib import Path
from typing import List, Dict, Optional

import requests

# Outbox offline (2026-07-10): il rig e' spesso SPENTO — senza coda locale ogni
# "ricorda" a rig spento andava perso. Le memorie finiscono qui e vengono
# inviate automaticamente alla prima occasione in cui il rig risponde (flush
# opportunistico su recall/store/status). Stesso pattern harness_outbox
# prescritto per ForgeStudio nel riassunto di progetto.
OUTBOX_PATH = Path(__file__).resolve().parents[2] / ".automem_outbox.jsonl"

# Non spammare log ad ogni messaggio quando il rig e' spento:
# segnala l'irraggiungibilita' al massimo una volta ogni N secondi.
_UNREACHABLE_LOG_INTERVAL = 300


class AutoMemClient:
    def __init__(self, config: dict):
        cfg = (config or {}).get("automem", {})
        self.enabled = bool(cfg.get("enabled", False))
        self.base_url = (cfg.get("url") or "http://192.168.1.100:8001").rstrip("/")
        self.timeout = float(cfg.get("timeout_seconds", 2.5))
        self._last_unreachable_log = 0.0

    def _log_unreachable(self, e: Exception) -> None:
        now = time.time()
        if now - self._last_unreachable_log > _UNREACHABLE_LOG_INTERVAL:
            print(f"[AutoMem] Non raggiungibile ({self.base_url}): {e} — "
                  f"normale se il rig e' spento, la chat continua senza memoria.")
            self._last_unreachable_log = now

    # ------------------------------------------------------------------
    # Outbox offline
    # ------------------------------------------------------------------

    def _queue_offline(self, payload: Dict) -> bool:
        try:
            payload = dict(payload)
            payload["_queued_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            with open(OUTBOX_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(payload, ensure_ascii=False) + "\n")
            return True
        except Exception as e:
            print(f"[AutoMem] Impossibile accodare in outbox: {e}")
            return False

    def flush_outbox(self) -> int:
        """Invia al rig le memorie accodate offline. Ritorna quante ne ha
        sincronizzate. Chiamata opportunisticamente (recall/store/status):
        costo ~zero quando l'outbox non esiste."""
        if not self.enabled or not OUTBOX_PATH.exists():
            return 0
        try:
            lines = [l for l in OUTBOX_PATH.read_text(encoding="utf-8").splitlines() if l.strip()]
        except Exception:
            return 0
        if not lines:
            try:
                OUTBOX_PATH.unlink()
            except Exception:
                pass
            return 0
        sent, remaining = 0, []
        for line in lines:
            try:
                payload = json.loads(line)
                payload.pop("_queued_at", None)
            except Exception:
                continue  # riga corrotta: scartala
            try:
                r = requests.post(f"{self.base_url}/memory", json=payload, timeout=self.timeout)
                r.raise_for_status()
                sent += 1
            except Exception:
                remaining.append(line)
        try:
            if remaining:
                tmp = OUTBOX_PATH.with_suffix(".tmp")
                tmp.write_text("\n".join(remaining) + "\n", encoding="utf-8")
                tmp.replace(OUTBOX_PATH)
            else:
                OUTBOX_PATH.unlink()
        except Exception as e:
            print(f"[AutoMem] Errore riscrittura outbox: {e}")
        if sent:
            print(f"[AutoMem] Outbox sincronizzata: {sent} memorie inviate al rig"
                  + (f" ({len(remaining)} ancora in coda)" if remaining else ""))
        return sent

    def outbox_size(self) -> int:
        if not OUTBOX_PATH.exists():
            return 0
        try:
            return sum(1 for l in OUTBOX_PATH.read_text(encoding="utf-8").splitlines() if l.strip())
        except Exception:
            return 0

    def recall(self, query: str, tags: Optional[List[str]] = None,
               limit: int = 3) -> List[str]:
        """Memorie rilevanti alla query (lista di stringhe). [] su qualsiasi errore.
        Effetto collaterale utile: se il rig risponde e c'e' un'outbox, la svuota
        (sincronizzazione automatica appena il rig torna raggiungibile)."""
        if not self.enabled or not query.strip():
            return []
        try:
            params = {"query": query, "limit": limit}
            if tags:
                params["tags"] = ",".join(tags)
            r = requests.get(f"{self.base_url}/recall", params=params, timeout=self.timeout)
            r.raise_for_status()
            self.flush_outbox()  # rig raggiungibile: svuota eventuali memorie accodate offline
            data = r.json()
            # Difensivo sulle due forme piu' plausibili della risposta
            if isinstance(data, list):
                items = data
            elif isinstance(data, dict):
                items = data.get("memories") or data.get("results") or []
            else:
                items = []
            out = []
            for item in items[:limit]:
                if isinstance(item, str):
                    out.append(item)
                elif isinstance(item, dict):
                    text = item.get("content") or item.get("memory") or item.get("text")
                    if text:
                        out.append(str(text))
            return out
        except Exception as e:
            self._log_unreachable(e)
            return []

    def store(self, content: str, tags: Optional[List[str]] = None,
              importance: float = 0.5, queue_if_offline: bool = True) -> str:
        """Salva una memoria. Ritorna: 'stored' (inviata al rig), 'queued'
        (rig spento: accodata in outbox, si sincronizza da sola), 'failed'.
        Mai eccezioni."""
        if not self.enabled or not content.strip():
            return "failed"
        payload = {"content": content.strip()[:4000],
                   "tags": tags or [],
                   "importance": importance}
        try:
            self.flush_outbox()  # prima le vecchie: preserva l'ordine cronologico
            r = requests.post(f"{self.base_url}/memory", json=payload, timeout=self.timeout)
            r.raise_for_status()
            return "stored"
        except Exception as e:
            self._log_unreachable(e)
            if queue_if_offline and self._queue_offline(payload):
                return "queued"
            return "failed"

    def status(self) -> Dict:
        """Per la UI: {'enabled', 'reachable', 'outbox'}. Mai eccezioni.
        Se il rig risponde, svuota anche l'outbox (sync automatica)."""
        if not self.enabled:
            return {"enabled": False, "reachable": False, "outbox": self.outbox_size()}
        try:
            r = requests.get(f"{self.base_url}/health", timeout=self.timeout)
            reachable = r.status_code == 200
            if reachable:
                self.flush_outbox()
            return {"enabled": True, "reachable": reachable, "outbox": self.outbox_size()}
        except Exception:
            return {"enabled": True, "reachable": False, "outbox": self.outbox_size()}


def project_tags(project_path: str) -> List[str]:
    """Tag standard per le memorie di un progetto DEVIN — stesso formato che
    l'harness futuro usera' per filtrare (e che Hermes vede via MCP)."""
    from pathlib import Path
    name = Path(project_path).name or "general"
    return ["devin", f"project:{name}"]
