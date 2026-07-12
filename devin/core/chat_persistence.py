"""
chat_persistence.py - Persistenza server-side dello storico chat, per-progetto.

Prerequisito per:
- bottone "Continua in chat" da un run fallito (Mantenimento/Scaffolding)
- notifica Telegram dopo N ore di silenzio sulla stessa sessione
- bottone "genera patch da questa conversazione e riprova"

Design: un file per progetto (.devin_chat/session.json dentro la cartella del
progetto stesso), non un DB globale — la conversazione ha senso "focalizzata"
su un progetto specifico, viaggia con esso, è ispezionabile a mano, e segue
lo stesso pattern di state_persistence.py (write .tmp -> rename atomico).
"""

import json
from pathlib import Path
from datetime import datetime
from typing import List, Dict, Optional


class ChatPersistence:
    def __init__(self, project_path: str, chat_id: str = None):
        """chat_id (opzionale, modalita' Progetti 2026-07-09): se presente, la
        conversazione vive in .devin/chats/<chat_id>.json — piu' chat per lo
        stesso progetto (vedi devin/core/project_space.py). Senza chat_id resta
        il comportamento storico (.devin_chat/session.json, una sola chat):
        retrocompatibile con bot Telegram, generate_patch e sessioni esistenti."""
        self.project_path = Path(project_path).resolve()
        self.chat_id = self._sanitize_chat_id(chat_id) if chat_id else None
        if self.chat_id:
            self.chat_dir = self.project_path / ".devin" / "chats"
            self.session_file = self.chat_dir / f"{self.chat_id}.json"
        else:
            self.chat_dir = self.project_path / ".devin_chat"
            self.session_file = self.chat_dir / "session.json"

    @staticmethod
    def _sanitize_chat_id(chat_id: str) -> str:
        """Solo caratteri sicuri: il chat_id arriva dal client e finisce in un
        filename — niente path traversal."""
        import re
        cleaned = re.sub(r"[^\w\-]", "_", Path(chat_id).name)
        return cleaned or "chat_invalid"

    def load(self) -> List[Dict[str, str]]:
        """Carica lo storico [{role, content}, ...]. Lista vuota se non esiste o corrotto."""
        if not self.session_file.exists():
            return []
        try:
            data = json.loads(self.session_file.read_text(encoding="utf-8"))
            history = data.get("history", [])
            if isinstance(history, list):
                return history
            return []
        except Exception as e:
            print(f"[ChatPersistence] Errore lettura ({self.session_file}): {e}")
            return []

    def save(self, history: List[Dict[str, str]], max_messages: int = 100) -> None:
        """Salva lo storico in modo atomico, troncato agli ultimi max_messages.
        In modalita' chat_id preserva il campo 'title' scritto da ProjectSpace
        (new_chat/rename_chat) — save() non deve cancellarlo."""
        self.chat_dir.mkdir(parents=True, exist_ok=True)
        trimmed = history[-max_messages:] if max_messages else history

        title = None
        if self.chat_id and self.session_file.exists():
            try:
                title = json.loads(self.session_file.read_text(encoding="utf-8")).get("title")
            except Exception:
                pass

        data = {
            "project_path": str(self.project_path),
            "history": trimmed,
            "updated_at": datetime.now().isoformat(),
        }
        if self.chat_id:
            data["chat_id"] = self.chat_id
            if title:
                data["title"] = title

        tmp = self.session_file.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.session_file)  # atomic: mai un file a meta' scritto
        except Exception as e:
            print(f"[ChatPersistence] Errore salvataggio ({self.session_file}): {e}")

    def append(self, role: str, content: str, max_messages: int = 100) -> List[Dict[str, str]]:
        """Helper: carica, appende un turno, salva, ritorna lo storico aggiornato."""
        history = self.load()
        history.append({"role": role, "content": content})
        self.save(history, max_messages=max_messages)
        return history

    def clear(self) -> None:
        """Elimina la sessione (bottone 'reset conversazione')."""
        if self.session_file.exists():
            self.session_file.unlink()

    def last_updated(self) -> Optional[str]:
        """ISO timestamp dell'ultimo aggiornamento, o None se non esiste — utile per
        il check periodico 'nessuna risposta da N ore' (notifica Telegram futura)."""
        if not self.session_file.exists():
            return None
        try:
            data = json.loads(self.session_file.read_text(encoding="utf-8"))
            return data.get("updated_at")
        except Exception:
            return None
