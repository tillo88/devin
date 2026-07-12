import re
from devin.ai.client import AIClient
from devin.agents.prompts import (
    CODER_SYSTEM_PROMPT, SCAFFOLD_CODER_SYSTEM_PROMPT, WHOLE_FILE_CODER_SYSTEM_PROMPT,
)


class Coder:
    def __init__(self, ai_client: AIClient):
        self.ai = ai_client

    def _extract_diff(self, raw: str) -> str:
        """Estrae una unified diff valida dalla risposta LLM."""
        raw = raw.strip()
        if not raw:
            return ""

        # 1. Cerca blocco markdown ```diff ... ```
        m = re.search(r'```(?:diff)?\s*\n?(.*?)```', raw, re.DOTALL)
        if m:
            return m.group(1).strip()

        # 2. Cerca inizio diff --git
        if "diff --git" in raw:
            idx = raw.index("diff --git")
            return raw[idx:].strip()

        # 3. Cerca pattern --- / +++
        m = re.search(r'--- .*?\n\+\+\+ .*', raw, re.DOTALL)
        if m:
            start = m.start()
            return raw[start:].strip()

        # Nessuna diff trovata
        return ""

    def generate(self, plan, context, feedback=None) -> str:
        """Modalità 1 (Mantenimento): genera una unified diff su codice esistente."""
        feedback_block = ""
        if feedback:
            feedback_block = f"""

FEEDBACK ON PREVIOUS ATTEMPT (the previous patch failed — fix these issues):
{feedback}
"""

        plan_str = getattr(plan, "raw_response", str(plan))

        messages = [
            {"role": "system", "content": CODER_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": f"""
PLAN:
{plan_str}

CURRENT CODE:
{context[:12000]}
{feedback_block}
"""
            }
        ]

        raw = self.ai.local(messages, mode="coder", timeout=90)
        if not raw:
            return ""

        diff = self._extract_diff(raw)

        # Log per debug
        if diff:
            print(f"[Coder] Extracted diff: {len(diff.splitlines())} lines")
        else:
            print(f"[Coder] WARNING: no valid diff found in response ({len(raw.splitlines())} lines)")

        return diff

    def _parse_full_files(self, raw: str) -> dict:
        """Estrae i blocchi '### FILE: <path>' + fenced code block dalla risposta
        whole-file. Ritorna {rel_path: full_content} nell'ordine di apparizione."""
        files = {}
        if not raw:
            return files
        pattern = re.compile(
            r'#+\s*FILE:\s*(?P<path>[^\n`]+?)\s*\n+```[a-zA-Z0-9_+.\-]*\n(?P<body>.*?)\n```',
            re.DOTALL,
        )
        for m in pattern.finditer(raw):
            path = m.group("path").strip().strip('`').strip().lstrip("/")
            body = m.group("body")
            if path:
                files[path] = body
        return files

    def generate_full_files(self, plan, context, feedback=None) -> dict:
        """Modalità WHOLE-FILE (file piccoli): il Coder restituisce il contenuto
        COMPLETO di ogni file da creare/modificare, niente diff e niente patcher.
        Elimina la classe di fallimenti da unified-diff su modelli piccoli (righe
        di contesto allucinate → patch non applicabile). Ritorna {rel_path: content}."""
        feedback_block = ""
        if feedback:
            feedback_block = f"""

FEEDBACK ON PREVIOUS ATTEMPT (the previous result failed — fix these issues):
{feedback}
"""
        plan_str = getattr(plan, "raw_response", str(plan))

        messages = [
            {"role": "system", "content": WHOLE_FILE_CODER_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": f"""
PLAN:
{plan_str}

CURRENT CODE (the exact current content of the project files):
{context[:12000]}
{feedback_block}
"""
            }
        ]

        raw = self.ai.local(messages, mode="coder", timeout=120) or ""
        files = self._parse_full_files(raw)

        if files:
            print(f"[Coder] Whole-file: {len(files)} file -> {list(files.keys())}")
        else:
            print(f"[Coder] WARNING: whole-file, nessun file estratto ({len(raw.splitlines())} righe grezze)")

        return files

    def generate_file(self, filename: str, spec: str, project_context: str = "") -> str:
        """
        Modalità 2 (Zero-Shot Scaffolding): genera il contenuto COMPLETO di un file
        da zero, scrittura diretta (no diff pipeline). Usato da Orchestrator.run_scaffold().
        """
        messages = [
            {"role": "system", "content": SCAFFOLD_CODER_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": f"""
FILE DA CREARE: {filename}

SPEC:
{spec}

CONTESTO PROGETTO (file già creati in questo scaffold, per coerenza import/nomi):
{project_context[:3000] if project_context.strip() else "(primo file del progetto, nessun contesto precedente)"}

Restituisci SOLO il contenuto del file {filename}.
"""
            }
        ]

        raw = self.ai.local(messages, mode="coder", timeout=60) or ""
        # Strip di un eventuale fence markdown residuo, anche se il prompt lo vieta esplicitamente
        content = re.sub(r'^```[a-zA-Z]*\n', '', raw.strip())
        content = re.sub(r'\n```$', '', content)
        return content
