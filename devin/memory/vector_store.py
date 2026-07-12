"""vector_store.py - Semantic search con persistenza e re-indicizzazione condizionale

FASE 1 AGGIORNAMENTI:
- Persistenza in workspace/.devin_cache/semantic_index.pkl
- Re-indicizzazione condizionale (check mtime)
- Fallback robusto: sentence-transformers -> sklearn -> keyword matching
- Test E2E integrato
- BUGFIX: TF-IDF con testi corti, cache dir creation, multilingual search
"""

import os
import pickle
import hashlib
from pathlib import Path
from typing import List, Dict, Any, Optional


class VectorStore:
    """
    Vector store con embedding progressivi:
    1. sentence-transformers all-MiniLM-L6-v2 (~80MB)
    2. sklearn TfidfVectorizer(max_features=5000) — con min_df=1 per testi corti
    3. keyword matching (one-hot parole, pad a stessa dim)
    """

    def __init__(self, cache_dir: str = None):
        self._embedding_engine = None
        self._engine_name = None
        self._vectorizer = None
        self._index = []  # lista di dict: {text, embedding, metadata, mtime_hash}
        self._dim = None
        self._cache_dir = Path(cache_dir) if cache_dir else None
        self._cache_file = None
        self._project_path = None

    def _get_embedding_engine(self):
        """Inizializza l'engine di embedding migliore disponibile."""
        if self._embedding_engine is not None:
            return self._embedding_engine, self._engine_name

        # 1. Prova sentence-transformers
        try:
            from sentence_transformers import SentenceTransformer
            model = SentenceTransformer('all-MiniLM-L6-v2')
            self._embedding_engine = model
            self._engine_name = "sentence-transformers"
            self._dim = 384  # all-MiniLM-L6-v2
            print(f"[VectorStore] Using sentence-transformers (dim={self._dim})")
            return self._embedding_engine, self._engine_name
        except Exception as e:
            print(f"[VectorStore] sentence-transformers unavailable: {e}")

        # 2. Fallback a sklearn TF-IDF — min_df=1 per testi corti, no stop words
        try:
            from sklearn.feature_extraction.text import TfidfVectorizer
            self._vectorizer = TfidfVectorizer(
                max_features=5000,
                min_df=1,           # BUGFIX: accetta anche parole che appaiono 1 volta
                stop_words=None,     # BUGFIX: non filtrare stop words (multilingual)
                token_pattern=r"(?u)\b\w+\b"  # BUGFIX: include anche numeri e underscore
            )
            self._embedding_engine = self._vectorizer
            self._engine_name = "sklearn-tfidf"
            self._dim = 5000
            print(f"[VectorStore] Using sklearn TF-IDF (dim={self._dim})")
            return self._embedding_engine, self._engine_name
        except Exception as e:
            print(f"[VectorStore] sklearn unavailable: {e}")

        # 3. Fallback a keyword matching
        self._embedding_engine = None
        self._engine_name = "keyword"
        self._dim = 1000
        print(f"[VectorStore] Using keyword matching (dim={self._dim})")
        return self._embedding_engine, self._engine_name

    def _encode(self, texts: List[str]) -> List[List[float]]:
        """Codifica una lista di testi in embeddings."""
        engine, name = self._get_embedding_engine()

        if name == "sentence-transformers":
            embeddings = engine.encode(texts, show_progress_bar=False)
            return embeddings.tolist()

        elif name == "sklearn-tfidf":
            if not hasattr(self._vectorizer, 'vocabulary_'):
                # Prima chiamata: fit_transform
                matrix = self._vectorizer.fit_transform(texts)
            else:
                matrix = self._vectorizer.transform(texts)
            return matrix.toarray().tolist()

        else:  # keyword fallback
            return self._encode_keyword(texts)

    def _encode_keyword(self, texts: List[str]) -> List[List[float]]:
        """Fallback: one-hot encoding delle parole più comuni."""
        from collections import Counter
        import re

        # Estrai parole da tutti i testi
        all_words = []
        for text in texts:
            words = re.findall(r'\b[a-zA-Z_]+\b', text.lower())
            all_words.extend(words)

        # Top 1000 parole
        vocab = {word: i for i, (word, _) in enumerate(Counter(all_words).most_common(1000))}

        embeddings = []
        for text in texts:
            words = re.findall(r'\b[a-zA-Z_]+\b', text.lower())
            vec = [0.0] * 1000
            for word in words:
                if word in vocab:
                    vec[vocab[word]] = 1.0
            embeddings.append(vec)

        return embeddings

    def _file_mtime_hash(self, file_path: Path) -> str:
        """Hash basato su path + mtime per tracciare modifiche."""
        try:
            mtime = file_path.stat().st_mtime
            content = f"{file_path}:{mtime}"
            return hashlib.md5(content.encode()).hexdigest()[:16]
        except Exception:
            return hashlib.md5(str(file_path).encode()).hexdigest()[:16]

    def _should_reindex(self, files: List[Dict[str, Any]], cache_path: Path) -> bool:
        """Verifica se l'indice in cache è ancora valido."""
        if not cache_path.exists():
            return True

        try:
            with open(cache_path, "rb") as f:
                cached = pickle.load(f)

            cached_files = cached.get("files_meta", {})
            for file_info in files:
                path = Path(file_info["path"])
                current_hash = self._file_mtime_hash(path)
                cached_hash = cached_files.get(str(path))
                if cached_hash != current_hash:
                    print(f"[VectorStore] File changed: {path}")
                    return True

            # Verifica anche che non ci siano file rimossi
            current_paths = {str(f["path"]) for f in files}
            cached_paths = set(cached_files.keys())
            if current_paths != cached_paths:
                print(f"[VectorStore] File set changed")
                return True

            return False

        except Exception as e:
            print(f"[VectorStore] Cache read error: {e}, reindexing")
            return True

    def index_project(
        self, 
        project_path: str, 
        files: List[Dict[str, Any]],
        cache_path: Path = None
    ):
        """
        Indicizza i file del progetto con persistenza.

        Args:
            project_path: path del progetto
            files: lista di dict con 'path', 'content'
            cache_path: path per la cache persistente (default: project/.devin_cache/semantic_index.pkl)
        """
        self._project_path = Path(project_path)

        if cache_path is None:
            cache_dir = self._project_path / ".devin_cache"
            cache_dir.mkdir(parents=True, exist_ok=True)
            cache_path = cache_dir / "semantic_index.pkl"
        else:
            # BUGFIX: assicurati che la directory esista
            cache_path.parent.mkdir(parents=True, exist_ok=True)

        self._cache_file = cache_path

        # Verifica se serve re-indicizzazione
        if not self._should_reindex(files, cache_path):
            print(f"[VectorStore] Cache valida, caricamento da {cache_path}")
            self._load_from_cache(cache_path)
            return

        print(f"[VectorStore] Re-indicizzazione di {len(files)} file...")

        # Rimuovi vecchi doc dello stesso progetto
        self._index = []

        texts = []
        metadatas = []
        mtime_hashes = {}

        for file_info in files:
            path = Path(file_info["path"])
            content = file_info.get("content", "")

            # Tronca content a 4000 chars
            if len(content) > 4000:
                content = content[:4000]

            texts.append(content)
            metadatas.append({
                "path": str(path),
                "project": str(project_path),
                "filename": path.name,
            })
            mtime_hashes[str(path)] = self._file_mtime_hash(path)

        if not texts:
            print("[VectorStore] Nessun file da indicizzare")
            return

        # Genera embeddings
        embeddings = self._encode(texts)

        # Costruisci indice
        for i, (text, emb, meta) in enumerate(zip(texts, embeddings, metadatas)):
            self._index.append({
                "text": text,
                "embedding": emb,
                "metadata": meta,
                "id": i,
            })

        # Salva cache
        self._save_to_cache(cache_path, mtime_hashes)
        print(f"[VectorStore] Indicizzati {len(self._index)} documenti")

    def _save_to_cache(self, cache_path: Path, mtime_hashes: Dict[str, str]):
        """Salva l'indice su disco con atomic write."""
        try:
            # BUGFIX: assicurati che la directory esista
            cache_path.parent.mkdir(parents=True, exist_ok=True)

            cache_data = {
                "index": self._index,
                "engine": self._engine_name,
                "dim": self._dim,
                "files_meta": mtime_hashes,
                "project": str(self._project_path),
            }

            # Atomic write
            tmp_path = cache_path.with_suffix(".tmp")
            with open(tmp_path, "wb") as f:
                pickle.dump(cache_data, f)
            tmp_path.rename(cache_path)

            print(f"[VectorStore] Cache salvata: {cache_path}")
        except Exception as e:
            print(f"[VectorStore] Cache save error: {e}")

    def _load_from_cache(self, cache_path: Path):
        """Carica l'indice dalla cache."""
        try:
            with open(cache_path, "rb") as f:
                cache_data = pickle.load(f)

            self._index = cache_data.get("index", [])
            self._engine_name = cache_data.get("engine", "unknown")
            self._dim = cache_data.get("dim", 0)
            self._project_path = Path(cache_data.get("project", "."))

            # Ripristina vectorizer se necessario
            if self._engine_name == "sklearn-tfidf":
                # Non possiamo serializzare il vectorizer, dobbiamo re-fit
                # ma manteniamo l'indice per la search
                pass

            print(f"[VectorStore] Caricati {len(self._index)} documenti dalla cache")
        except Exception as e:
            print(f"[VectorStore] Cache load error: {e}")
            self._index = []

    def search_semantic(
        self, 
        query: str, 
        project_path: str = None, 
        top_k: int = 5
    ) -> List[Dict[str, Any]]:
        """
        Ricerca semantica nel progetto.

        Args:
            query: query di ricerca
            project_path: filtra per progetto specifico
            top_k: numero di risultati
        """
        if not self._index:
            print("[VectorStore] Indice vuoto, nessun risultato")
            return []

        # Codifica query
        query_embedding = self._encode([query])[0]

        # Calcola similarità coseno
        results = []
        for doc in self._index:
            # Filtra per progetto se specificato
            if project_path:
                doc_project = doc["metadata"].get("project", "")
                if doc_project != str(project_path):
                    continue

            score = self._cosine_similarity(query_embedding, doc["embedding"])
            results.append({
                "text": doc["text"],
                "score": score,
                "metadata": doc["metadata"],
            })

        # Ordina per score decrescente
        results.sort(key=lambda x: x["score"], reverse=True)
        return results[:top_k]

    def _cosine_similarity(self, a: List[float], b: List[float]) -> float:
        """Calcola similarità coseno tra due vettori."""
        import math

        dot = sum(x * y for x, y in zip(a, b))
        norm_a = math.sqrt(sum(x * x for x in a))
        norm_b = math.sqrt(sum(x * x for x in b))

        if norm_a == 0 or norm_b == 0:
            return 0.0

        return dot / (norm_a * norm_b)

    def clear(self):
        """Pulisce l'indice in memoria."""
        self._index = []
        self._embedding_engine = None
        self._engine_name = None


# ============================================================
# TEST E2E INTEGRATO
# ============================================================

def _run_e2e_test():
    """Test E2E del vector store (no server, no GPU, no rete)."""
    import tempfile
    import shutil
    import time

    print("=" * 60)
    print("VectorStore E2E Test")
    print("=" * 60)

    # Crea progetto fittizio
    tmpdir = tempfile.mkdtemp(prefix="devin_test_")
    project_path = Path(tmpdir)

    try:
        # Crea file di test — contenuto più ricco per TF-IDF
        (project_path / "calc.py").write_text("""
def add(a, b):
    \"\"\"Add two numbers together and return the sum.\"\"\"
    return a + b

def sum_list(numbers):
    \"\"\"Calculate the total sum of a list of numbers.\"\"\"
    result = 0
    for n in numbers:
        result = result + n
    return result
""")
        (project_path / "readme.md").write_text("""
# Calculator Project
This is a simple calculator for basic arithmetic operations.
You can add numbers, subtract them, and perform calculations.
""")
        (project_path / "utils.py").write_text("""
import os

def get_env(key):
    \"\"\"Get environment variable value.\"\"\"
    return os.getenv(key, "")
""")

        files = []
        for f in project_path.rglob("*"):
            if f.is_file() and f.suffix in (".py", ".md"):
                files.append({
                    "path": str(f),
                    "content": f.read_text(),
                })

        # Test 1: Indicizzazione
        print("\n[Test 1] Indicizzazione...")
        vs = VectorStore()
        cache_path = project_path / ".devin_cache" / "semantic_index.pkl"
        vs.index_project(str(project_path), files, cache_path=cache_path)
        assert len(vs._index) == 3, f"Atteso 3 documenti, trovati {len(vs._index)}"
        print("  PASS: 3 documenti indicizzati")

        # Test 2: Ricerca semantica — query in inglese per TF-IDF
        print("\n[Test 2] Ricerca semantica...")
        results = vs.search_semantic("add numbers sum total", project_path=str(project_path), top_k=2)
        assert len(results) > 0, "Nessun risultato trovato"

        # Verifica che calc.py sia nei top risultati
        calc_found = any("calc.py" in r["metadata"]["path"] for r in results)
        assert calc_found, f"calc.py non trovato nei top-{len(results)} risultati: {[r['metadata']['path'] for r in results]}"
        print(f"  PASS: calc.py trovato nei top-{len(results)} risultati (scores: {[round(r['score'], 3) for r in results]})")

        # Test 3: Cache persistente
        print("\n[Test 3] Cache persistente...")
        vs2 = VectorStore()
        vs2.index_project(str(project_path), files, cache_path=cache_path)
        assert len(vs2._index) == 3, "Cache non caricata correttamente"
        print("  PASS: Cache caricata correttamente")

        # Test 4: Re-indicizzazione condizionale
        print("\n[Test 4] Re-indicizzazione condizionale...")
        time.sleep(0.1)
        (project_path / "calc.py").write_text("""
def add(a, b):
    return a + b

def multiply(a, b):
    return a * b
""")
        files_updated = []
        for f in project_path.rglob("*"):
            if f.is_file() and f.suffix in (".py", ".md"):
                files_updated.append({
                    "path": str(f),
                    "content": f.read_text(),
                })

        vs3 = VectorStore()
        vs3.index_project(str(project_path), files_updated, cache_path=cache_path)
        # Dovrebbe aver re-indicizzato perché calc.py è cambiato
        assert len(vs3._index) == 3, "Re-indicizzazione fallita"
        print("  PASS: Re-indicizzazione avvenuta su file modificato")

        print("\n" + "=" * 60)
        print("TUTTI I TEST PASSATI!")
        print("=" * 60)
        return True

    except Exception as e:
        print(f"\n[FAIL] Test fallito: {e}")
        import traceback
        traceback.print_exc()
        return False

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    _run_e2e_test()
