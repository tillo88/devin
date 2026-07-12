"""
test_vector_store_e2e.py — Test E2E per VectorStore (Fase 1)
NO server, NO GPU, NO rete richiesti.
"""

import sys
import os
import tempfile
import shutil
from pathlib import Path

# Aggiungi devin al path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from devin.memory.vector_store import VectorStore, _run_e2e_test


def test_basic_indexing():
    """Test base: indicizzazione e ricerca."""
    tmpdir = tempfile.mkdtemp(prefix="devin_vs_test_")
    project_path = Path(tmpdir)

    try:
        # Setup file
        (project_path / "calc.py").write_text("""
def add(a, b):
    return a + b
""")
        (project_path / "main.py").write_text("""
from calc import add
print(add(2, 3))
""")

        files = [
            {"path": str(project_path / "calc.py"), "content": (project_path / "calc.py").read_text()},
            {"path": str(project_path / "main.py"), "content": (project_path / "main.py").read_text()},
        ]

        vs = VectorStore()
        vs.index_project(str(project_path), files)

        assert len(vs._index) == 2, f"Atteso 2 documenti, trovati {len(vs._index)}"

        # Ricerca
        results = vs.search_semantic("addition function", project_path=str(project_path), top_k=2)
        assert len(results) > 0
        assert any("calc.py" in r["metadata"]["path"] for r in results)

        print("✅ test_basic_indexing PASS")
        return True

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_cache_persistence():
    """Test persistenza cache con mtime."""
    tmpdir = tempfile.mkdtemp(prefix="devin_vs_cache_test_")
    project_path = Path(tmpdir)
    cache_path = project_path / ".devin_cache" / "test.pkl"

    try:
        (project_path / "file.py").write_text("x = 1")
        files = [{"path": str(project_path / "file.py"), "content": "x = 1"}]

        vs1 = VectorStore()
        vs1.index_project(str(project_path), files, cache_path=cache_path)

        # Seconda istanza: dovrebbe caricare da cache
        vs2 = VectorStore()
        vs2.index_project(str(project_path), files, cache_path=cache_path)

        assert len(vs2._index) == 1
        print("✅ test_cache_persistence PASS")
        return True

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_cache_invalidation():
    """Test invalidazione cache su file modificato."""
    tmpdir = tempfile.mkdtemp(prefix="devin_vs_inv_test_")
    project_path = Path(tmpdir)
    cache_path = project_path / ".devin_cache" / "test.pkl"

    try:
        (project_path / "file.py").write_text("x = 1")
        files = [{"path": str(project_path / "file.py"), "content": "x = 1"}]

        vs1 = VectorStore()
        vs1.index_project(str(project_path), files, cache_path=cache_path)

        # Modifica file
        import time
        time.sleep(0.05)
        (project_path / "file.py").write_text("x = 2")
        files_updated = [{"path": str(project_path / "file.py"), "content": "x = 2"}]

        vs2 = VectorStore()
        vs2.index_project(str(project_path), files_updated, cache_path=cache_path)

        # Verifica che l'indice sia stato aggiornato
        results = vs2.search_semantic("x equals two", project_path=str(project_path))
        assert len(results) > 0
        assert "x = 2" in results[0]["text"]

        print("✅ test_cache_invalidation PASS")
        return True

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_empty_project():
    """Test progetto vuoto."""
    tmpdir = tempfile.mkdtemp(prefix="devin_vs_empty_")
    project_path = Path(tmpdir)

    try:
        vs = VectorStore()
        vs.index_project(str(project_path), [])
        results = vs.search_semantic("anything", project_path=str(project_path))
        assert len(results) == 0
        print("✅ test_empty_project PASS")
        return True
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def run_all_tests():
    """Esegue tutti i test e restituisce report."""
    print("=" * 60)
    print("VectorStore E2E Test Suite — Fase 1")
    print("=" * 60)

    tests = [
        ("Basic Indexing", test_basic_indexing),
        ("Cache Persistence", test_cache_persistence),
        ("Cache Invalidation", test_cache_invalidation),
        ("Empty Project", test_empty_project),
        ("Integrated E2E", _run_e2e_test),
    ]

    passed = 0
    failed = 0

    for name, test_fn in tests:
        print(f"\n--- Running: {name} ---")
        try:
            if test_fn():
                passed += 1
            else:
                failed += 1
                print(f"❌ {name} FAIL")
        except Exception as e:
            failed += 1
            print(f"❌ {name} FAIL: {e}")
            import traceback
            traceback.print_exc()

    print("\n" + "=" * 60)
    print(f"RISULTATI: {passed} passati, {failed} falliti")
    print("=" * 60)

    return failed == 0


if __name__ == "__main__":
    success = run_all_tests()
    sys.exit(0 if success else 1)
