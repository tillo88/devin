import json
import os
import time
import subprocess
import requests
from pathlib import Path

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None

# FIX: path assoluto ancorato alla posizione di QUESTO file (devin/ai/client.py),
# non alla CWD del processo. Prima era "config/settings.json" (relativo): se il
# server veniva avviato da una directory diversa dalla root del progetto, il file
# non veniva trovato. _load_config() cattura l'eccezione e ripiega silenziosamente
# sui default (rig_host 192.168.1.100, ecc.) — comportamento "funzionante ma con
# la config sbagliata", diagnosticabile solo dal print di warning in console.
_DEFAULT_CONFIG_PATH = str(Path(__file__).resolve().parents[2] / "config" / "settings.json")


class AIClient:
    """
    AIClient con:
    - Retry esponenziale su timeout/errore/connection (Task 12 — COMPLETO)
    - WOL + health check rig con retry, wake-up e throttling (Task 17 — COMPLETO)
    - Circuit Breaker per rig dopo 3 fallimenti consecutivi (Task 12 — NUOVO)
    """

    # Configurazione retry
    MAX_RETRIES = 3
    BASE_BACKOFF = 2  # secondi

    # Configurazione rig WOL
    WOL_MAX_WAIT = 90  # secondi massimi attesa rig dopo WOL
    WOL_POLL_INTERVAL = 5  # secondi tra un ping e l'altro
    WOL_MAC_FALLBACK = "00:00:00:00:00:00"  # da configurare in settings.json

    # Circuit Breaker config
    CIRCUIT_BREAKER_THRESHOLD = 3
    CIRCUIT_BREAKER_COOLDOWN = 60  # secondi

    def __init__(self, config_path: str = _DEFAULT_CONFIG_PATH):
        # --- CARICA CONFIG ---
        self.config = self._load_config(config_path)
        models_cfg = self.config.get("models", {})
        local_cfg = models_cfg.get("local_models", {})

        # RIG ESTERNO (progetto ai-rig-iso-build): UN SOLO llama-server attivo alla
        # volta (ruolo DEVIN = Ornith 35B MoE self-scaffolding su porta 8080),
        # niente split coder/reasoning su porte separate come ipotizzato prima —
        # lo stesso modello serve entrambi i ruoli in un'unica istanza.
        self.remote_host = os.getenv("DEVIN_REMOTE_HOST", models_cfg.get("rig_host", "192.168.1.100"))
        rig_port = models_cfg.get("rig_port", 8080)
        self.remote_coder_url = f"http://{self.remote_host}:{rig_port}/v1/chat/completions"
        self.remote_reasoning_url = self.remote_coder_url  # stesso endpoint, stesso modello

        # WOL config
        self.rig_mac = models_cfg.get("rig_mac", self.WOL_MAC_FALLBACK)
        self.wol_enabled = models_cfg.get("wol_enabled", True)
        self.wol_port = models_cfg.get("wol_port", 9)
        self.wol_throttle_seconds = models_cfg.get("wol_throttle_seconds", 300)  # 5 min

        # WOL ha senso in UN SOLO scenario: DEVIN sul PC che usa un rig REMOTO
        # separato, da accendere su richiesta. Va disattivato quando:
        # - rig_self_hosted: DEVIN gira SUL rig (localhost:8080) — svegliare se
        #   stessi via WOL e' assurdo;
        # - local_test_mode: il rig non esiste ancora, aspettarlo 90s ad ogni
        #   avvio e' solo tempo perso.
        self.rig_self_hosted = models_cfg.get("rig_self_hosted", False)
        local_test_mode = models_cfg.get("local_test_mode", False)
        if self.rig_self_hosted or local_test_mode:
            self.wol_enabled = False

        # Circuit Breaker config
        self.circuit_breaker_enabled = models_cfg.get("rig_circuit_breaker", {}).get("enabled", True)

        # MODELLO REMOTO (rig) - un solo nome, usato sia per coder che reasoning
        rig_models = models_cfg.get("rig_models", {})
        unified_model = rig_models.get("unified", "ornith-1.0-35b-a3b")
        self.remote_reasoning_model = unified_model
        self.remote_coder_model = unified_model

        # ENDPOINT LOCALI (fallback) — invariati: il PC locale ha davvero 2 modelli
        # separati (coder 7B + reasoning MoE/dense), a differenza del rig.
        self.local_coder_url = "http://localhost:8000/v1/chat/completions"
        self.local_reasoning_url = "http://localhost:8001/v1/chat/completions"

        # MODELLI LOCALI - dai nomi file GGUF nel config
        reasoning_cfg = local_cfg.get("reasoning", {}) or {}
        coder_cfg = local_cfg.get("coder", {}) or {}

        # FIX (bug segnalato in test locale, 2026-07-04): usare `.get(key) or default` invece di
        # `.get(key, default)`. Se la chiave "file" esiste nel JSON con valore esplicito null
        # (es. per marcare "nessun modello reasoning locale di default"), `.get(key, default)`
        # ritorna comunque None -> Path(None).stem esplode con TypeError all'avvio di ogni
        # richiesta HTTP (perche' _get_ai_client() istanzia AIClient() lazy). `.get(key) or default`
        # copre sia chiave assente SIA chiave presente-ma-null, senza dipendere da cosa scrive
        # settings.json in un dato momento (difesa anche se qualcuno rimette null in futuro).
        self.local_reasoning_model = self._extract_model_name(
            reasoning_cfg.get("file") or "qwen3-14b-q4_k_m.gguf"
        )
        self.local_coder_model = self._extract_model_name(
            coder_cfg.get("file") or "qwen2.5-coder-7b-instruct-q4_k_m.gguf"
        )

        # OPENAI fallback
        self.use_openai = bool(os.getenv("OPENAI_API_KEY"))
        self.openai = None
        if self.use_openai and OpenAI:
            self.openai = OpenAI()

        # Stato connessioni
        self.remote_coder_ok = False
        self.remote_reasoning_ok = False
        self._rig_was_awakened = False  # traccia se abbiamo già inviato WOL
        self._last_wol_time = 0  # throttling WOL

        # Circuit Breaker stato
        self._rig_health = {
            "failures": 0,
            "last_fail": 0,
            "state": "closed",  # closed, open, half-open
        }

        self.refresh()

    def _load_config(self, path: str) -> dict:
        """Carica config con fallback a default."""
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"[AIClient] Config load failed ({e}), using defaults")
            return {}

    def _extract_model_name(self, filename) -> str:
        """
        Estrae un nome modello pulito dal filename GGUF.
        Es: 'qwen2.5-coder-7b-instruct-q4_k_m.gguf' -> 'qwen2.5-coder-7b-instruct'
            'Qwen3.5-14B-A3B-Claude-Opus-Reasoning-Distilled-4.6-MXFP4_MOE.gguf' -> 'qwen3.5-14b-a3b'

        FIX difensivo: filename puo' arrivare None se una config a monte ha un bug
        (vedi nota nel chiamante). Non deve mai far crashare l'AIClient all'avvio.
        """
        if not filename:
            print("[AIClient] WARNING: _extract_model_name ricevuto filename vuoto/None, uso placeholder")
            filename = "unknown-model.gguf"

        name = Path(filename).stem  # rimuovi .gguf

        # Rimuovi suffissi di quantizzazione comuni
        quant_suffixes = [
            "-q4_k_m", "-q5_k_m", "-q6_k", "-q8_0",
            "-f16", "-f32", "-MXFP4_MOE", "-q4_0", "-q5_0"
        ]
        for suffix in quant_suffixes:
            if name.lower().endswith(suffix.lower()):
                name = name[:-len(suffix)]
                break

        # Normalizza: lowercase, rimuovi 'claude-opus-reasoning-distilled' etc per alias breve
        name = name.lower()

        # Se troppo lungo, tronca a parti significative
        if len(name) > 40:
            parts = name.split("-")
            filtered = []
            for p in parts:
                if p in ("claude", "opus", "reasoning", "distilled"):
                    continue
                filtered.append(p)
            name = "-".join(filtered)

        return name

    # ============================================================
    # TASK 17: WOL + HEALTH CHECK RIG CON RETRY
    # ============================================================

    def _send_wol(self, mac_address: str, port: int = 9) -> bool:
        """Invia magic packet Wake-on-LAN al rig."""
        try:
            import socket
            import struct

            # Normalizza MAC address
            mac = mac_address.replace(":", "").replace("-", "")
            if len(mac) != 12:
                print(f"[WOL] MAC address invalido: {mac_address}")
                return False

            data = b'\xff' * 6 + bytes.fromhex(mac) * 16

            # Broadcast
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.sendto(data, ('<broadcast>', port))
            sock.close()

            print(f"[WOL] Magic packet inviato a {mac_address} (port {port})")
            return True

        except Exception as e:
            print(f"[WOL] Errore invio magic packet: {e}")
            return False

    def _wait_for_rig(self, timeout: int = None, interval: int = None) -> bool:
        """Attende che il rig risponda al health check, con polling."""
        timeout = timeout or self.WOL_MAX_WAIT
        interval = interval or self.WOL_POLL_INTERVAL

        print(f"[RIG] Attendo che il rig risponda (max {timeout}s)...")
        start = time.time()
        attempts = 0

        while time.time() - start < timeout:
            attempts += 1
            coder_ok = self._health_check(self.remote_coder_url)
            reasoning_ok = self._health_check(self.remote_reasoning_url)

            if coder_ok and reasoning_ok:
                elapsed = round(time.time() - start, 1)
                print(f"[RIG] Rig online dopo {elapsed}s ({attempts} tentativi)")
                return True

            print(f"[RIG] Tentativo {attempts}: coder={coder_ok}, reasoning={reasoning_ok} — retry in {interval}s...")
            time.sleep(interval)

        print(f"[RIG] Timeout dopo {timeout}s, rig non raggiungibile")
        return False

    # ============================================================
    # TASK 12: CIRCUIT BREAKER PER RIG
    # ============================================================

    def _circuit_breaker_should_trip(self) -> bool:
        """Verifica se il circuit breaker deve aprirsi."""
        if not self.circuit_breaker_enabled:
            return False
        return self._rig_health["failures"] >= self.CIRCUIT_BREAKER_THRESHOLD

    def _circuit_breaker_is_open(self) -> bool:
        """Verifica se il circuit breaker è aperto (rig bannato)."""
        if not self.circuit_breaker_enabled:
            return False
        if self._rig_health["state"] == "open":
            # Verifica se il cooldown è scaduto
            elapsed = time.time() - self._rig_health["last_fail"]
            if elapsed > self.CIRCUIT_BREAKER_COOLDOWN:
                print(f"[CIRCUIT] Cooldown scaduto ({elapsed:.0f}s), passo a half-open")
                self._rig_health["state"] = "half-open"
                self._rig_health["failures"] = 0
                return False
            return True
        return False

    def _circuit_breaker_record_success(self):
        """Registra un successo, resetta contatori."""
        if self._rig_health["state"] in ("open", "half-open"):
            print("[CIRCUIT] Rig tornato healthy, chiudo circuit breaker")
        self._rig_health["failures"] = 0
        self._rig_health["state"] = "closed"

    def _circuit_breaker_record_failure(self):
        """Registra un fallimento, incrementa contatore."""
        self._rig_health["failures"] += 1
        self._rig_health["last_fail"] = time.time()

        if self._rig_health["failures"] >= self.CIRCUIT_BREAKER_THRESHOLD:
            self._rig_health["state"] = "open"
            print(f"[CIRCUIT] Circuit breaker APERTO per {self.CIRCUIT_BREAKER_COOLDOWN}s "
                  f"({self._rig_health['failures']} fallimenti consecutivi)")
        elif self._rig_health["state"] == "half-open":
            # Un solo fallimento in half-open riapre immediatamente
            self._rig_health["state"] = "open"
            print("[CIRCUIT] Fallimento in half-open, circuit breaker RIAPERTO")

    def refresh(self, try_wake: bool = True, wait_after_wake: bool = False):
        """
        Ricontrolla lo stato del rig esterno.
        Se il rig è offline e WOL è abilitato, invia magic packet e attende.
        """
        # Se circuit breaker è aperto, skip completamente il rig
        if self._circuit_breaker_is_open():
            print(f"[CIRCUIT] Rig bannato da circuit breaker, skip health check")
            self.remote_coder_ok = False
            self.remote_reasoning_ok = False
            return

        # Un solo endpoint reale ora (rig a ruolo unico): un solo health-check,
        # niente piu' 2 richieste ridondanti verso lo stesso URL.
        self.remote_coder_ok = self._health_check(self.remote_coder_url)
        self.remote_reasoning_ok = self.remote_coder_ok

        # Se il rig è online, registra successo e resetta circuit breaker
        if self.remote_coder_ok or self.remote_reasoning_ok:
            self._circuit_breaker_record_success()

        # Se il rig è offline e non abbiamo ancora provato WOL (con throttling)
        if not (self.remote_coder_ok or self.remote_reasoning_ok) and try_wake and self.wol_enabled:
            now = time.time()
            can_wake = (now - self._last_wol_time) > self.wol_throttle_seconds

            if can_wake and self.rig_mac != self.WOL_MAC_FALLBACK:
                print(f"[RIG] Rig offline — invio magic packet WOL a {self.rig_mac}")
                if self._send_wol(self.rig_mac, self.wol_port):
                    self._rig_was_awakened = True
                    self._last_wol_time = now
                    # Attesa SOLO se richiesta esplicitamente (dentro una chiamata
                    # che sta gia' ritentando). All'avvio wait_after_wake=False:
                    # il pacchetto e' partito, il rig si sveglia in background e
                    # verra' rilevato al primo uso reale — niente 90s di blocco.
                    if wait_after_wake and self._wait_for_rig():
                        self.remote_coder_ok = True
                        self.remote_reasoning_ok = True
                        self._circuit_breaker_record_success()

        if self.remote_coder_ok and self.remote_reasoning_ok:
            print(f"Rig esterno OK su {self.remote_host} -- uso modelli 32B")
        elif self.remote_coder_ok or self.remote_reasoning_ok:
            print(f"Rig parziale: coder={self.remote_coder_ok}, reasoning={self.remote_reasoning_ok}")
        else:
            print(f"Rig non disponibile -- fallback su modelli locali")

    def _health_check(self, url):
        """Ping rapido (2s) a /v1/models."""
        try:
            base = url.replace("/v1/chat/completions", "")
            r = requests.get(f"{base}/v1/models", timeout=2)
            return r.status_code == 200
        except Exception:
            return False

    def health(self):
        """Restituisce stato salute connessioni."""
        return {
            "remote_coder": self.remote_coder_ok,
            "remote_reasoning": self.remote_reasoning_ok,
            "remote_host": self.remote_host,
            "openai": self.use_openai,
            "rig_awakened": self._rig_was_awakened,
            "circuit_breaker": {
                "state": self._rig_health["state"],
                "failures": self._rig_health["failures"],
                "cooldown_remaining": max(0, self.CIRCUIT_BREAKER_COOLDOWN - (time.time() - self._rig_health["last_fail"]))
                if self._rig_health["state"] == "open" else 0
            }
        }

    def _get_endpoints(self, mode="reasoning"):
        """Seleziona URL e modello in base a disponibilita rig."""
        if mode == "reasoning":
            if self.remote_reasoning_ok:
                return self.remote_reasoning_url, self.remote_reasoning_model
            return self.local_reasoning_url, self.local_reasoning_model
        else:
            if self.remote_coder_ok:
                return self.remote_coder_url, self.remote_coder_model
            return self.local_coder_url, self.local_coder_model

    # ============================================================
    # TASK 12: RETRY CON BACKOFF ESPONENZIALE (COMPLETO)
    # ============================================================

    def _is_retryable_error(self, exception) -> bool:
        """Determina se un errore è retryable."""
        if isinstance(exception, requests.exceptions.Timeout):
            return True
        if isinstance(exception, requests.exceptions.ConnectionError):
            return True
        if isinstance(exception, requests.exceptions.HTTPError):
            # Retry su 502, 503, 504 (server error temporanei)
            if hasattr(exception, 'response') and exception.response is not None:
                return exception.response.status_code in (502, 503, 504)
            return False
        if isinstance(exception, requests.exceptions.ChunkedEncodingError):
            return True
        return False

    def _record_rig_failure(self, url: str):
        """Registra un fallimento del rig se l'URL è remoto."""
        if self.remote_host in url:
            self._circuit_breaker_record_failure()

    def local(self, messages, mode="reasoning", timeout=None):
        """
        Chiama endpoint locale/rig con retry e backoff esponenziale.
        Copre: Timeout, ConnectionError, HTTPError (502/503/504), ChunkedEncodingError.
        """
        url, model = self._get_endpoints(mode)

        if timeout is None:
            timeout = 60 if mode == "reasoning" else 90

        last_exception = None

        for attempt in range(self.MAX_RETRIES):
            try:
                print(f"[AIClient] POST {url} (mode={mode}, model={model}, timeout={timeout}s, attempt={attempt+1}/{self.MAX_RETRIES})")
                r = requests.post(
                    url,
                    json={
                        "model": model,
                        "messages": messages,
                        "temperature": 0.2
                    },
                    timeout=timeout
                )
                r.raise_for_status()
                data = r.json()
                content = data["choices"][0]["message"]["content"]

                # Successo: registra per circuit breaker
                if self.remote_host in url:
                    self._circuit_breaker_record_success()

                return content

            except Exception as e:
                last_exception = f"{type(e).__name__}: {e}"
                print(f"[AIClient] {last_exception} (mode={mode})")

                # Verifica se retryable
                if not self._is_retryable_error(e):
                    print(f"[AIClient] Errore non retryable, abort")
                    break

                # Registra fallimento rig per circuit breaker
                self._record_rig_failure(url)

                # Backoff esponenziale prima di retry
                if attempt < self.MAX_RETRIES - 1:
                    backoff = self.BASE_BACKOFF * (2 ** attempt)
                    print(f"[AIClient] Backoff {backoff}s prima di retry...")
                    time.sleep(backoff)
                    # Refresh connessioni: se il rig era tornato, usalo
                    # WOL solo all'ultimo tentativo (attempt == MAX_RETRIES - 2)
                    self.refresh(try_wake=(attempt == self.MAX_RETRIES - 2), wait_after_wake=True)

        # Tutti i retry esauriti
        print(f"[AIClient] Tutti i retry esauriti per {mode}")
        return None

    def cloud(self, messages, model="gpt-4o-mini"):
        if not self.openai:
            print("OpenAI non configurato (manca OPENAI_API_KEY)")
            return None

        try:
            response = self.openai.chat.completions.create(
                model=model,
                messages=messages,
                temperature=0.2
            )
            return response.choices[0].message.content

        except Exception as e:
            print(f"Errore chiamata OpenAI: {e}")
            return None

    def ask(self, messages, mode="reasoning"):
        """Entry point unico: rig -> locale -> cloud."""
        result = self.local(messages, mode=mode)

        if result is None and self.use_openai:
            print("Fallback su OpenAI...")
            result = self.cloud(messages)

        return result

    def complete(self, prompt, max_tokens=80, temperature=0.1, mode="coder"):
        """Per autocomplete e stream."""
        messages = [{"role": "user", "content": prompt}]

        if self.use_openai and self.openai:
            try:
                response = self.openai.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=messages,
                    temperature=temperature,
                    max_tokens=max_tokens
                )
                return response.choices[0].message.content
            except Exception as e:
                print(f"Errore OpenAI: {e}")
                return None

        url, model = self._get_endpoints(mode)

        try:
            r = requests.post(
                url,
                json={
                    "model": model,
                    "messages": messages,
                    "temperature": temperature,
                    "max_tokens": max_tokens
                },
                timeout=60
            )
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"]

        except Exception as e:
            print(f"Errore chiamata {mode}: {e}")
            self.refresh()
            return None

    def stream(self, messages, mode="reasoning"):
        """
        Streaming token-by-token -- con retry e backoff.
        """
        for attempt in range(self.MAX_RETRIES):
            url, model = self._get_endpoints(mode)

            try:
                with requests.post(
                    url,
                    json={
                        "model": model,
                        "messages": messages,
                        "temperature": 0.2,
                        "stream": True
                    },
                    timeout=120,
                    stream=True
                ) as r:
                    # 4xx = richiesta RIFIUTATA da un server RAGGIUNGIBILE: ritentare
                    # identico (o svegliare il rig) non serve. Causa tipica: contesto
                    # troppo lungo (es. 🌐 web search acceso su un modello locale con
                    # finestra piccola). Cattura il motivo vero e fermati con un messaggio
                    # utile, invece di bruciare 3 retry senza dire perché.
                    if 400 <= r.status_code < 500:
                        body = ""
                        try:
                            body = (r.text or "")[:400]
                        except Exception:
                            pass
                        print(f"[AIClient] {mode} HTTP {r.status_code} — richiesta rifiutata dal server: {body}")
                        looks_like_ctx = any(k in body.lower() for k in ("context", "exceed", "n_ctx", "too long", "token"))
                        hint = ("Contesto troppo lungo per il modello locale: spegni il 🌐 web search "
                                "o inizia una nuova conversazione." if (looks_like_ctx or r.status_code == 400)
                                else "")
                        yield f"\n[Richiesta rifiutata dal modello (HTTP {r.status_code}). {hint} Dettaglio server: {body[:200]}]"
                        return
                    r.raise_for_status()

                    for line in r.iter_lines():
                        if not line:
                            continue

                        line = line.decode('utf-8')
                        if not line.startswith('data: '):
                            continue

                        data = line[6:]
                        if data == '[DONE]':
                            return

                        try:
                            chunk = json.loads(data)
                            content = chunk.get('choices', [{}])[0].get('delta', {}).get('content')
                            if content:
                                yield content  # YIELD IMMEDIATO, nessun buffer
                        except json.JSONDecodeError:
                            continue

            except Exception as e:
                print(f"[AIClient] Stream error {mode} (attempt {attempt+1}/{self.MAX_RETRIES}): {e}")

                # Registra fallimento per circuit breaker
                self._record_rig_failure(url)

                if attempt < self.MAX_RETRIES - 1:
                    backoff = self.BASE_BACKOFF * (2 ** attempt)
                    print(f"[AIClient] Backoff {backoff}s prima di retry stream...")
                    time.sleep(backoff)
                    self.refresh(try_wake=(attempt == self.MAX_RETRIES - 2), wait_after_wake=True)
                else:
                    yield f"\n[Stream error after {self.MAX_RETRIES} attempts: {e}]"
                    return
