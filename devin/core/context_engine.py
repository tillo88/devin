import os
import re
from pathlib import Path

# Stessa exclusion-list gia' usata in devin/engine/sandbox.py per create_sandbox().
# FIX: senza questa, collect_project_files() include ricorsivamente anche i residui
# di sandbox/run precedenti (es. workspace/test_project/workspace/sandbox/calc.py),
# facendo vedere al Coder DUE versioni diverse dello stesso file e confondendolo su
# quale patchare -> patch ripetutamente "sul file sbagliato" anche dopo Self-Healing.
EXCLUDED_DIR_NAMES = {
    "workspace", "venv", ".venv", "env", ".git",
    "__pycache__", ".pytest_cache", "node_modules",
    "dist", "build", ".devin_cache", ".devin_state", "logs",
    # Modalita' Progetti (2026-07-09): dati di progetto (chat/knowledge/istruzioni),
    # NON codice — un .py caricato come knowledge confonderebbe il Coder (stesso
    # identico bug dei residui sandbox che questa lista era nata per risolvere).
    ".devin", ".devin_chat",
}


class ContextEngine:

    def __init__(self, max_chars=100000):
        self.max_total_chars = max_chars
        self.max_file_chars = 12_000        
        self.max_files = 60                 
        self.project_path = None

    def collect_project_files(self):
        files = []
        for root, dirs, filenames in os.walk(self.project_path):
            # FIX: pota le directory escluse PRIMA di scendere (in-place su os.walk,
            # non solo un filtro a posteriori) — coerente con create_sandbox().
            dirs[:] = [d for d in dirs if d not in EXCLUDED_DIR_NAMES]

            for name in filenames:
                if not name.endswith(".py"):
                    continue
                path = os.path.join(root, name)
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        content = f.read()
                    files.append({
                        "path": path,
                        "content": content,
                        "rel_path": os.path.relpath(path, self.project_path)
                    })
                except Exception:
                    continue
        return files

    def _score_relevance(self, file_obj, query):
        if not query:
            return 50
        query_lower = query.lower()
        query_terms = set(re.findall(r"\w+", query_lower))
        score = 0
        name_lower = file_obj["rel_path"].lower()
        for term in query_terms:
            if term in name_lower:
                score += 25
        content_lower = file_obj["content"][:5000].lower()
        for term in query_terms:
            if term in content_lower:
                score += 15
        central_names = ["main.py", "core", "config", "settings", "init", "app.py", "orchestrator"]
        for central in central_names:
            if central in name_lower:
                score += 10
        return min(score, 100)

    def build(self, project_path, query=None):
        self.project_path = project_path
        files = self.collect_project_files()
        if not files:
            return ""
        scored = []
        for f in files:
            score = self._score_relevance(f, query)
            size_penalty = min(len(f["content"]) / 5000, 15)
            final_score = score - size_penalty
            scored.append((final_score, f))
        scored.sort(key=lambda x: x[0], reverse=True)
        context = []
        total_chars = 0
        files_included = 0
        for score, f in scored:
            if total_chars >= self.max_total_chars:
                break
            if files_included >= self.max_files:
                break
            content = f["content"][:self.max_file_chars]
            header_line = "# FILE: " + f["rel_path"] + "\n"
            block = header_line + content + "\n"
            if total_chars + len(block) > self.max_total_chars:
                remaining = self.max_total_chars - total_chars
                if remaining < 200:
                    break
                trim = remaining - len(header_line) - 20
                if trim > 100:
                    content = f["content"][:trim] + "\n# [...truncated...]\n"
                    block = header_line + content
                else:
                    break
            context.append(block)
            total_chars += len(block)
            files_included += 1
        final_context = "\n\n".join(context)
        header = "# PROJECT CONTEXT (" + str(files_included) + " files, " + str(total_chars) + " chars, query: " + (query or "general") + ")\n\n"
        return (header + final_context)[:self.max_total_chars]

    def prioritize(self, base_context, semantic_context, query):
        """Unisce contesto semantico a quello base, dando priorità ai file rilevanti."""
        if not semantic_context or not semantic_context.strip():
            return base_context
        return semantic_context + "\n\n" + base_context
