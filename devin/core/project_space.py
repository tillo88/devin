"""
project_space.py - "Modalita' Progetti" stile Claude Projects, per DEVIN AI IDE.

Aggiunto 2026-07-09. Ogni progetto (project_path) ha una cartella nascosta
`.devin/` che contiene:

    .devin/
    ├── instructions.md          # istruzioni/system prompt del progetto
    ├── knowledge/               # file di conoscenza allegati (raw)
    │   └── _extracted/          # testo estratto per l'indicizzazione
    ├── knowledge_index.pkl      # indice semantico (VectorStore, cache)
    ├── chats/                   # conversazioni multiple (una per file)
    │   └── <chat_id>.json
    └── export/                  # dataset JSONL per harness/fine-tuning

Design:
- Tutto dentro il progetto (viaggia con esso), coerente con chat_persistence.py
  e state_persistence.py. Nessun DB globale.
- La knowledge NON viene mai iniettata per intero nel contesto (ctx locale 8192):
  retrieve_context() usa il VectorStore esistente (devin/memory/vector_store.py)
  per recuperare solo i chunk rilevanti alla domanda, con un budget massimo di
  caratteri esplicito.
- I chunk sono indicizzati con uno pseudo-path che include l'hash del contenuto:
  se il testo cambia, il set di path cambia, e _should_reindex del VectorStore
  ("file set changed") forza la re-indicizzazione senza logica extra qui.
"""

import json
import hashlib
import re
from pathlib import Path
from datetime import datetime
from typing import List, Dict, Optional

from devin.ai.document_extract import extract_text as _extract_document_text

# Estensioni testo puro: si decodificano direttamente, senza parser dedicati.
TEXT_EXTENSIONS = {".txt", ".md", ".py", ".json", ".jsonl", ".csv", ".log", ".yaml",
                   ".yml", ".sh", ".bat", ".js", ".html", ".css", ".toml", ".ini", ".cfg"}
# Formati binari gestiti da document_extract.py
BINARY_EXTENSIONS = {".pdf", ".docx", ".xlsx", ".pptx"}

MAX_KNOWLEDGE_FILE_BYTES = 20 * 1024 * 1024   # 20MB: limite upload singolo file
CHUNK_CHARS = 1500                             # dimensione chunk per il retrieval
MAX_CHUNKS_PER_FILE = 60                       # protezione contro file enormi
DEFAULT_RETRIEVE_BUDGET = 3500                 # budget caratteri knowledge nel contesto
MAX_INSTRUCTIONS_CHARS = 4000                  # le istruzioni si iniettano SEMPRE intere


def _safe_name(name: str) -> str:
    """Sanitizza un filename/chat_id: niente path traversal, niente caratteri strani."""
    name = Path(name).name  # butta via eventuali directory
    return re.sub(r"[^\w.\- ]", "_", name).strip() or "unnamed"


class ProjectSpace:
    def __init__(self, project_path: str):
        self.project_path = Path(project_path).expanduser().resolve()
        self.base = self.project_path / ".devin"
        self.knowledge_dir = self.base / "knowledge"
        self.extracted_dir = self.knowledge_dir / "_extracted"
        self.chats_dir = self.base / "chats"
        self.export_dir = self.base / "export"
        self.instructions_file = self.base / "instructions.md"
        self.description_file = self.base / "description.md"   # "about" del progetto (header + contesto)
        self.pins_file = self.base / "pins.json"              # file del progetto sempre nel contesto (★)
        self.index_cache = self.base / "knowledge_index.pkl"
        self.files_index_cache = self.base / "files_index.pkl"
        self._vector_store = None        # lazy: sentence-transformers costa all'import
        self._files_vector_store = None  # indice separato per i FILE del progetto

    # ------------------------------------------------------------------
    # Istruzioni di progetto (system prompt per-progetto)
    # ------------------------------------------------------------------

    def get_instructions(self) -> str:
        if not self.instructions_file.exists():
            return ""
        try:
            return self.instructions_file.read_text(encoding="utf-8").strip()
        except Exception as e:
            print(f"[ProjectSpace] Errore lettura istruzioni: {e}")
            return ""

    def set_instructions(self, text: str) -> None:
        self.base.mkdir(parents=True, exist_ok=True)
        text = (text or "").strip()[:MAX_INSTRUCTIONS_CHARS]
        tmp = self.instructions_file.with_suffix(".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(self.instructions_file)

    # ------------------------------------------------------------------
    # Descrizione progetto ("about": scopo/stack) — header + contesto persistente
    # ------------------------------------------------------------------

    def get_description(self) -> str:
        if not self.description_file.exists():
            return ""
        try:
            return self.description_file.read_text(encoding="utf-8").strip()
        except Exception:
            return ""

    def set_description(self, text: str) -> None:
        self.base.mkdir(parents=True, exist_ok=True)
        text = (text or "").strip()[:2000]
        tmp = self.description_file.with_suffix(".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(self.description_file)

    # ------------------------------------------------------------------
    # Pin: file del progetto SEMPRE nel contesto (★) — pensato per il coding,
    # così il modello non "dimentica" com'è fatto un file chiave (modulo
    # principale, spec, contratto API). Salvati come path relativi in pins.json.
    # ------------------------------------------------------------------

    def _pin_is_safe(self, rel: str) -> Optional[Path]:
        """Risolve un pin relativo e lo ritorna solo se cade DENTRO il progetto."""
        rel = (rel or "").strip().lstrip("/")
        if not rel:
            return None
        target = (self.project_path / rel).resolve()
        if target != self.project_path and self.project_path not in target.parents:
            return None
        return target

    def list_pins(self) -> List[str]:
        if not self.pins_file.exists():
            return []
        try:
            data = json.loads(self.pins_file.read_text(encoding="utf-8"))
            return [p for p in data if isinstance(p, str)]
        except Exception:
            return []

    def _save_pins(self, pins: List[str]) -> None:
        self.base.mkdir(parents=True, exist_ok=True)
        tmp = self.pins_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(pins, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.pins_file)

    def add_pin(self, rel_path: str) -> bool:
        target = self._pin_is_safe(rel_path)
        if target is None or not target.is_file():
            return False
        rel = (rel_path or "").strip().lstrip("/")
        pins = self.list_pins()
        if rel not in pins:
            pins.append(rel)
            self._save_pins(pins)
        return True

    def remove_pin(self, rel_path: str) -> bool:
        rel = (rel_path or "").strip().lstrip("/")
        pins = self.list_pins()
        if rel in pins:
            pins.remove(rel)
            self._save_pins(pins)
            return True
        return False

    def read_pinned(self, max_chars_per_file: int = 4000) -> List[Dict]:
        """[{path, content}] dei file pinnati esistenti, per l'iniezione SEMPRE nel
        contesto. Salta (senza rimuovere) i pin il cui file è momentaneamente sparito."""
        out = []
        for rel in self.list_pins():
            target = self._pin_is_safe(rel)
            if target is None or not target.is_file():
                continue
            try:
                out.append({"path": rel,
                            "content": target.read_text(encoding="utf-8", errors="ignore")[:max_chars_per_file]})
            except Exception:
                continue
        return out

    # ------------------------------------------------------------------
    # Knowledge base (file allegati al progetto)
    # ------------------------------------------------------------------

    def add_knowledge(self, filename: str, raw: bytes) -> Dict:
        """Salva il file raw + il testo estratto. Ritorna metadati (o errore leggibile)."""
        filename = _safe_name(filename)
        if len(raw) > MAX_KNOWLEDGE_FILE_BYTES:
            return {"ok": False, "error": f"File troppo grande ({len(raw)} bytes, max {MAX_KNOWLEDGE_FILE_BYTES})"}

        ext = Path(filename).suffix.lower()
        if ext in TEXT_EXTENSIONS:
            text = raw.decode("utf-8", errors="replace")
        elif ext in BINARY_EXTENSIONS:
            text = _extract_document_text(filename, raw)
        else:
            supported = ", ".join(sorted(TEXT_EXTENSIONS | BINARY_EXTENSIONS))
            return {"ok": False, "error": f"Estensione '{ext}' non supportata. Supportate: {supported}"}

        self.knowledge_dir.mkdir(parents=True, exist_ok=True)
        self.extracted_dir.mkdir(parents=True, exist_ok=True)

        (self.knowledge_dir / filename).write_bytes(raw)
        (self.extracted_dir / (filename + ".txt")).write_text(text, encoding="utf-8")

        # Invalida l'indice: verra' ricostruito alla prossima retrieve
        self._vector_store = None
        if self.index_cache.exists():
            try:
                self.index_cache.unlink()
            except Exception:
                pass

        return {"ok": True, "filename": filename, "chars": len(text),
                "added_at": datetime.now().isoformat()}

    def list_knowledge(self) -> List[Dict]:
        if not self.knowledge_dir.exists():
            return []
        items = []
        for f in sorted(self.knowledge_dir.iterdir()):
            if not f.is_file():
                continue
            extracted = self.extracted_dir / (f.name + ".txt")
            items.append({
                "filename": f.name,
                "size": f.stat().st_size,
                "chars": extracted.stat().st_size if extracted.exists() else 0,
                "added_at": datetime.fromtimestamp(f.stat().st_mtime).isoformat(),
            })
        return items

    def delete_knowledge(self, filename: str) -> bool:
        filename = _safe_name(filename)
        raw = self.knowledge_dir / filename
        extracted = self.extracted_dir / (filename + ".txt")
        deleted = False
        for p in (raw, extracted):
            if p.exists():
                try:
                    p.unlink()
                    deleted = True
                except Exception as e:
                    print(f"[ProjectSpace] Errore delete {p}: {e}")
        if deleted:
            self._vector_store = None
            if self.index_cache.exists():
                try:
                    self.index_cache.unlink()
                except Exception:
                    pass
        return deleted

    # ------------------------------------------------------------------
    # Retrieval semantico (knowledge -> contesto, con budget)
    # ------------------------------------------------------------------

    def _chunks(self) -> List[Dict]:
        """Chunk di tutti i testi estratti, come pseudo-file per il VectorStore.
        Lo pseudo-path contiene l'hash del chunk: contenuto cambiato = path
        cambiato = re-indicizzazione automatica (vedi docstring del modulo)."""
        chunks = []
        if not self.extracted_dir.exists():
            return chunks
        for f in sorted(self.extracted_dir.glob("*.txt")):
            try:
                text = f.read_text(encoding="utf-8")
            except Exception:
                continue
            source = f.name[:-4]  # togli il ".txt" aggiunto all'estrazione
            # split su paragrafi, poi riaccorpa fino a CHUNK_CHARS
            paragraphs = re.split(r"\n\s*\n", text)
            current = ""
            file_chunks = []
            for p in paragraphs:
                if len(current) + len(p) + 2 > CHUNK_CHARS and current:
                    file_chunks.append(current)
                    current = p
                else:
                    current = (current + "\n\n" + p) if current else p
            if current.strip():
                file_chunks.append(current)
            for chunk in file_chunks[:MAX_CHUNKS_PER_FILE]:
                h = hashlib.md5(chunk.encode("utf-8", errors="replace")).hexdigest()[:8]
                chunks.append({
                    "path": f"{self.extracted_dir / source}#chunk-{h}",
                    "content": f"[Da: {source}]\n{chunk}",
                })
        return chunks

    def retrieve_context(self, query: str, top_k: int = 4,
                          max_chars: int = DEFAULT_RETRIEVE_BUDGET) -> str:
        """Recupera i chunk di knowledge piu' rilevanti alla query, entro il budget.
        Stringa vuota se non c'e' knowledge o niente di rilevante."""
        chunks = self._chunks()
        if not chunks or not query.strip():
            return ""

        def _index_and_search():
            if self._vector_store is None:
                from devin.memory.vector_store import VectorStore
                self._vector_store = VectorStore()
            self.base.mkdir(parents=True, exist_ok=True)
            # index_project SEMPRE, non solo alla prima chiamata: con contenuto
            # invariato e' un no-op (carica dalla cache via mtime-hash), con
            # contenuto nuovo re-indicizza — senza, un file aggiunto a server
            # acceso restava invisibile fino al riavvio.
            self._vector_store.index_project(
                str(self.project_path), chunks, cache_path=self.index_cache)
            return self._vector_store.search_semantic(
                query, project_path=str(self.project_path), top_k=top_k)

        try:
            results = _index_and_search()
        except Exception as e:
            # Caso noto: VectorStore in fallback TF-IDF non puo' ri-fit il
            # vectorizer da cache -> la search dopo un riavvio puo' fallire.
            # Butto la cache e re-indicizzo da zero, UNA volta. Se fallisce
            # ancora, si degrada a chat senza knowledge (mai un errore utente).
            print(f"[ProjectSpace] Retrieval fallito ({e}) — reindicizzo da zero e riprovo")
            self._vector_store = None
            try:
                if self.index_cache.exists():
                    self.index_cache.unlink()
                results = _index_and_search()
            except Exception as e2:
                print(f"[ProjectSpace] Retrieval fallito anche dopo reindex (procedo senza knowledge): {e2}")
                return ""

        parts, used = [], 0
        for r in results:
            # score 0 = nessuna sovrapposizione reale: non iniettare rumore
            if r.get("score", 0) <= 0:
                continue
            text = r["text"]
            if used + len(text) > max_chars:
                text = text[: max(0, max_chars - used)]
            if text.strip():
                parts.append(text)
                used += len(text)
            if used >= max_chars:
                break
        return "\n\n---\n\n".join(parts)

    # ------------------------------------------------------------------
    # Retrieval sui FILE del progetto (2026-07-10)
    # Caso d'uso: "guarda nel progetto X" quando X non ha knowledge curata ma
    # ha file di testo nella cartella. Indice separato da quello knowledge.
    # ------------------------------------------------------------------

    SCAN_EXCLUDED_DIRS = {".devin", ".devin_chat", ".devin_cache", ".devin_state",
                          ".git", "__pycache__", "venv", ".venv", "node_modules",
                          "workspace", "logs", "dist", "build", "memory_backups"}
    MAX_PROJECT_FILES_INDEXED = 40
    MAX_PROJECT_FILE_BYTES = 200_000

    def list_files(self, max_items: int = 30) -> List[str]:
        """Elenco (relativo) dei file di testo del progetto — per far SAPERE al
        modello cosa contiene un progetto anche quando il retrieval non trova
        match (altrimenti risponde 'non ho accesso ai file')."""
        items = []
        if not self.project_path.exists():
            return items
        try:
            for f in sorted(self.project_path.rglob("*")):
                if len(items) >= max_items:
                    break
                if not f.is_file():
                    continue
                # BUGFIX 2026-07-10: il check va sul path RELATIVO al progetto.
                # Con f.parts (assoluto) un progetto dentro .../workspace/... aveva
                # "workspace" tra i part -> TUTTI i file esclusi, sempre.
                rel_parts = f.relative_to(self.project_path).parts
                if any(part in self.SCAN_EXCLUDED_DIRS for part in rel_parts):
                    continue
                items.append(str(f.relative_to(self.project_path)))
        except Exception as e:
            print(f"[ProjectSpace] list_files errore: {e}")
        return items

    def _project_file_chunks(self) -> List[Dict]:
        """Chunk dei file di testo del progetto (stesso trucco hash-nel-path
        della knowledge: contenuto cambiato = reindex automatico)."""
        chunks = []
        if not self.project_path.exists():
            return chunks
        indexed = 0
        try:
            for f in sorted(self.project_path.rglob("*")):
                if indexed >= self.MAX_PROJECT_FILES_INDEXED:
                    break
                if not f.is_file() or f.suffix.lower() not in TEXT_EXTENSIONS:
                    continue
                # BUGFIX 2026-07-10: path RELATIVO, come in list_files (vedi nota li').
                if any(part in self.SCAN_EXCLUDED_DIRS
                       for part in f.relative_to(self.project_path).parts):
                    continue
                try:
                    if f.stat().st_size > self.MAX_PROJECT_FILE_BYTES:
                        continue
                    text = f.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    continue
                rel = str(f.relative_to(self.project_path))
                indexed += 1
                for i in range(0, min(len(text), CHUNK_CHARS * 20), CHUNK_CHARS):
                    chunk = text[i:i + CHUNK_CHARS]
                    if not chunk.strip():
                        continue
                    h = hashlib.md5(chunk.encode("utf-8", errors="replace")).hexdigest()[:8]
                    chunks.append({
                        "path": f"{f}#filechunk-{h}",
                        "content": f"[File del progetto: {rel}]\n{chunk}",
                    })
        except Exception as e:
            print(f"[ProjectSpace] scan file progetto fallita: {e}")
        return chunks

    def retrieve_from_files(self, query: str, top_k: int = 3,
                             max_chars: int = 1500) -> str:
        """Come retrieve_context ma sui file del progetto invece che sulla
        knowledge curata. Fail-soft: '' su qualsiasi problema."""
        chunks = self._project_file_chunks()
        if not chunks or not query.strip():
            return ""

        def _go():
            if self._files_vector_store is None:
                from devin.memory.vector_store import VectorStore
                self._files_vector_store = VectorStore()
            self.base.mkdir(parents=True, exist_ok=True)
            # index_project SEMPRE (no-op se nulla e' cambiato, vedi retrieve_context):
            # i file del progetto cambiano fuori dal nostro controllo, l'indice
            # deve seguirli senza riavvio del server.
            self._files_vector_store.index_project(
                str(self.project_path), chunks, cache_path=self.files_index_cache)
            return self._files_vector_store.search_semantic(
                query, project_path=str(self.project_path), top_k=top_k)

        try:
            results = _go()
        except Exception as e:
            print(f"[ProjectSpace] files-retrieval fallito ({e}) — reindex e riprovo")
            self._files_vector_store = None
            try:
                if self.files_index_cache.exists():
                    self.files_index_cache.unlink()
                results = _go()
            except Exception as e2:
                print(f"[ProjectSpace] files-retrieval fallito anche dopo reindex: {e2}")
                return ""

        parts, used = [], 0
        for r in results:
            if r.get("score", 0) <= 0:
                continue
            text = r["text"]
            if used + len(text) > max_chars:
                text = text[: max(0, max_chars - used)]
            if text.strip():
                parts.append(text)
                used += len(text)
            if used >= max_chars:
                break
        return "\n\n---\n\n".join(parts)

    # ------------------------------------------------------------------
    # Conversazioni multiple per progetto
    # ------------------------------------------------------------------

    def list_chats(self) -> List[Dict]:
        if not self.chats_dir.exists():
            return []
        chats = []
        for f in self.chats_dir.glob("*.json"):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                history = data.get("history", [])
                chats.append({
                    "chat_id": f.stem,
                    "title": data.get("title") or self._auto_title(history),
                    "updated_at": data.get("updated_at", ""),
                    "messages": len(history),
                })
            except Exception:
                # File corrotto: elencalo comunque, cosi' l'utente puo' cancellarlo
                chats.append({"chat_id": f.stem, "title": "(file corrotto)",
                              "updated_at": "", "messages": 0})
        chats.sort(key=lambda c: c.get("updated_at") or "", reverse=True)
        return chats

    @staticmethod
    def _auto_title(history: List[Dict]) -> str:
        for m in history:
            if m.get("role") == "user" and m.get("content", "").strip():
                return m["content"].strip().replace("\n", " ")[:60]
        return "Nuova chat"

    def new_chat(self, title: str = "") -> str:
        self.chats_dir.mkdir(parents=True, exist_ok=True)
        chat_id = datetime.now().strftime("chat_%Y%m%d_%H%M%S_%f")
        data = {"title": (title or "").strip()[:80], "history": [],
                "updated_at": datetime.now().isoformat()}
        (self.chats_dir / f"{chat_id}.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return chat_id

    def rename_chat(self, chat_id: str, title: str) -> bool:
        f = self.chats_dir / f"{_safe_name(chat_id)}.json"
        if not f.exists():
            return False
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            data["title"] = (title or "").strip()[:80]
            tmp = f.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(f)
            return True
        except Exception as e:
            print(f"[ProjectSpace] Errore rename chat: {e}")
            return False

    def delete_chat(self, chat_id: str) -> bool:
        f = self.chats_dir / f"{_safe_name(chat_id)}.json"
        if f.exists():
            try:
                f.unlink()
                return True
            except Exception as e:
                print(f"[ProjectSpace] Errore delete chat: {e}")
        return False

    # ------------------------------------------------------------------
    # Export dataset per harness / fine-tuning futuro
    # ------------------------------------------------------------------

    def export_dataset(self) -> Optional[Path]:
        """Esporta TUTTE le conversazioni del progetto in JSONL (una conversazione
        per riga, formato OpenAI chat: {"messages":[{role,content},...]}).
        Le istruzioni di progetto diventano il system message di ogni riga —
        cosi' il dataset e' direttamente usabile per un LoRA/harness senza
        conversioni. Ritorna il path del file, o None se non c'e' nulla."""
        chats = self.list_chats()
        instructions = self.get_instructions()
        lines = []
        for c in chats:
            f = self.chats_dir / f"{c['chat_id']}.json"
            try:
                history = json.loads(f.read_text(encoding="utf-8")).get("history", [])
            except Exception:
                continue
            msgs = [m for m in history if m.get("role") in ("user", "assistant")
                    and m.get("content", "").strip()]
            if not msgs:
                continue
            record = {"messages": ([{"role": "system", "content": instructions}] if instructions else []) + msgs,
                      "meta": {"project": self.project_path.name, "chat_id": c["chat_id"],
                               "exported_at": datetime.now().isoformat()}}
            lines.append(json.dumps(record, ensure_ascii=False))
        if not lines:
            return None
        self.export_dir.mkdir(parents=True, exist_ok=True)
        out = self.export_dir / f"dataset_{datetime.now():%Y%m%d_%H%M%S}.jsonl"
        out.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return out
