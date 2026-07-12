#!/usr/bin/env python3
"""
Test end-to-end dell'orchestratore DEVIN con modelli mockati.
Non richiede llama-server, GPU né connessione di rete.

Uso:
    cd /home/tillo/devin_ai_ide
    source venv/bin/activate
    python test_orchestrator_e2e.py
"""
import sys
import json
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

from devin.core.orchestrator import Orchestrator, LauncherStatus


def test_sandbox_no_recursion():
    """Verifica che create_sandbox non entri in ricorsione infinita."""
    from devin.engine.sandbox import create_sandbox

    with tempfile.TemporaryDirectory() as tmpdir:
        project = Path(tmpdir) / "project"
        project.mkdir()
        (project / "main.py").write_text("print('hello')")
        (project / "workspace").mkdir()
        (project / "workspace" / "old_sandbox").mkdir()

        sandbox = create_sandbox(project)
        assert sandbox.exists(), "Sandbox non creata"
        assert (sandbox / "main.py").exists(), "File non copiato"
        assert not (sandbox / "workspace").exists(), "workspace/ non escluso!"

        # Seconda chiamata non deve crashare (pulizia precedente)
        sandbox2 = create_sandbox(project)
        assert sandbox2.exists()
        print("✓ Sandbox: nessuna ricorsione, workspace escluso")


def test_patcher_and_runner():
    """Test patcher + runner su un progetto finto."""
    from devin.engine.patcher import Patcher
    from devin.engine.runner import Runner

    with tempfile.TemporaryDirectory() as tmpdir:
        project = Path(tmpdir) / "project"
        project.mkdir()
        (project / "calc.py").write_text("def add(a, b):\n    return a - b\n")
        (project / "main.py").write_text("from calc import add\nprint(add(2, 3))\n")

        diff_patch = """diff --git a/calc.py b/calc.py
--- a/calc.py
+++ b/calc.py
@@ -1,2 +1,2 @@
 def add(a, b):
-    return a - b
+    return a + b
"""

        patcher = Patcher()
        sandbox = patcher.apply(diff_patch, str(project))

        calc_content = (sandbox / "calc.py").read_text()
        assert "return a + b" in calc_content, f"Patch non applicata: {calc_content}"

        runner = Runner()
        result = runner.run(str(sandbox), entrypoint="main.py")
        assert result.success, f"Runner fallito: {result.error}"

        print("✓ Patcher + Runner: patch applicata ed eseguita correttamente")


def test_orchestrator_e2e_mock():
    """Test end-to-end dell'orchestratore con LLM mockati."""

    with tempfile.TemporaryDirectory() as tmpdir:
        project = Path(tmpdir) / "project"
        project.mkdir()
        (project / "calc.py").write_text("def add(a, b):\n    return a - b\n")
        (project / "main.py").write_text("from calc import add\nassert add(2, 3) == 5\nprint('OK')\n")

        diff_patch = """diff --git a/calc.py b/calc.py
--- a/calc.py
+++ b/calc.py
@@ -1,2 +1,2 @@
 def add(a, b):
-    return a - b
+    return a + b
"""

        # Config temporaneo minimale
        config = {
            "context": {"max_chars": 100000, "semantic_search_enabled": False},
            "models": {
                "local_models_dir": str(project / "models"),
                "llama_server_path": "/bin/echo",
                "auto_start_local": False,
                "local_models": {}
            }
        }
        config_path = project / "config.json"
        config_path.write_text(json.dumps(config))

        # Mock AIClient
        mock_client = MagicMock()
        mock_client.refresh = MagicMock()
        mock_client.health.return_value = {}

        def mock_local(messages, mode="reasoning"):
            content = ""
            for m in reversed(messages):
                if m.get("role") == "user":
                    content = m.get("content", "")
                    break

            if "TASK" in content:
                return "RESULT: ACTION_NEEDED\n1. Fix calc.py"
            elif "CURRENT CODE" in content:
                return diff_patch
            elif "ERROR" in content:
                return "Change minus to plus"
            return "OK"

        mock_client.local = mock_local

        # Mock Launcher
        mock_launcher = MagicMock()
        mock_launcher.ensure_models.return_value = LauncherStatus(
            rig_available=False,
            rig_host="",
            rig_ports=[],
            local_running={
                "8000": {"name": "coder", "port": 8000, "status": "running"},
                "8001": {"name": "planner", "port": 8001, "status": "running"},
            },
            model_source="local",
            errors=[],
        )
        mock_launcher.get_status.return_value = mock_launcher.ensure_models.return_value
        mock_launcher.shutdown_all = MagicMock()

        with patch('devin.core.orchestrator.LocalModelLauncher') as MockLauncher:
            with patch('devin.core.orchestrator.AIClient') as MockAI:
                MockLauncher.from_config.return_value = mock_launcher
                MockAI.return_value = mock_client

                orch = Orchestrator(
                    config_path=str(config_path),
                    project_path=str(project)
                )

                result = orch.run("Fix the bug in calc.py", project_path=str(project))

                assert result["success"], f"Orchestrator fallito: {result.get('error')}"
                print("✓ Orchestrator E2E: task completato con successo")


if __name__ == "__main__":
    print("🧪 Test End-to-End DEVIN\n")
    test_sandbox_no_recursion()
    test_patcher_and_runner()
    test_orchestrator_e2e_mock()
    print("\n🎉 Tutti i test E2E passati!")