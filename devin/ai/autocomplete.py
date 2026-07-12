from devin.ai.client import AIClient


class Autocomplete:

    def __init__(self, ai_client: AIClient = None):
        # FIX (bug 1.2 report): riusa il client passato invece di istanziarne uno
        # nuovo ad ogni chiamata (ogni AIClient() fa 2x health-check + eventuale WOL).
        self.ai = ai_client or AIClient()

    def suggest(self, code, language="python", cursor_position=None):
        """
        Genera una suggestion per il codice dato.
        Ritorna stringa sincrona (fallback).
        """
        prompt = self._build_prompt(code, language, cursor_position)
        try:
            return self.ai.complete(prompt, max_tokens=80, temperature=0.1, mode="coder")
        except Exception as e:
            print(f"[Autocomplete] Error: {e}")
            return ""

    def suggest_stream(self, code, language="python", cursor_position=None):
        """
        Genera suggestion in streaming (SSE).
        Yields token chunks.
        """
        prompt = self._build_prompt(code, language, cursor_position)
        messages = [{"role": "user", "content": prompt}]
        try:
            yield from self.ai.stream(messages, mode="coder")
        except Exception as e:
            print(f"[Autocomplete Stream] Error: {e}")
            yield ""

    def _build_prompt(self, code, language, cursor_position):
        """Costruisce prompt ottimizzato per code completion."""
        # Prendi le ultime 1500 chars prima del cursore per contesto
        if cursor_position is not None and cursor_position <= len(code):
            context = code[max(0, cursor_position - 1500):cursor_position]
        else:
            context = code[-1500:] if len(code) > 1500 else code

        # Se il contesto è vuoto o troppo corto, ritorna prompt base
        if len(context.strip()) < 3:
            # FIX (bug 3.1 report): era una stringa multi-riga non terminata
            # (newline letterale dentro f"..."), causava SyntaxError all'import
            # del modulo intero -> /api/autocomplete e /api/autocomplete/stream
            # crashavano silenziosamente (mascherati dal try/except in fast_app.py).
            return f"Complete this {language} code snippet:\n{context}"

        # Prompt ottimizzato: il modello deve continuare il codice, non spiegare
        return f"""You are an expert {language} programmer. Complete the following code.
Rules:
- Continue from where the code ends
- Output ONLY the completion, no explanations, no markdown
- Match the existing indentation and style
- Keep it concise (1-5 lines max)
- Do NOT repeat code already present

Code to complete:
```{language}
{context}
```

Completion:"""
