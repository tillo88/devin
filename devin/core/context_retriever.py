from devin.memory.vector_store import VectorStore


class ContextRetriever:

    def __init__(self, enabled=True):
        self.store = VectorStore()
        self.enabled = enabled

    def build_context(self, query, project_path=None, top_k=5):
        """Costruisce un blocco di contesto dai file semanticamente piu rilevanti."""
        results = self.store.search_semantic(query, project_path=project_path, top_k=top_k)

        if not results:
            return ""

        lines = ["# === FILE RILEVANTI SEMANTICAMENTE AL TASK ==="]

        for r in results:
            path = r["metadata"]["path"]
            score = r["score"]
            text = r["text"]
            lines.append("")
            lines.append("# FILE: " + path + " (rilevanza: " + str(round(score, 2)) + ")")
            lines.append(text)

        return "\n".join(lines)

    def retrieve(self, query: str, project_path: str = None) -> str:
        """Metodo richiesto dall'Orchestrator per la ricerca semantica."""
        return self.build_context(query, project_path=project_path)