import subprocess
from pathlib import Path


def ensure_git_repo(project_path):
    """Inizializza un repo git nel progetto se non esiste già."""
    project_path = Path(project_path)
    git_dir = project_path / ".git"

    if not git_dir.exists():
        subprocess.run(["git", "init"], cwd=str(project_path), capture_output=True, text=True)

        # Config locale di sicurezza
        subprocess.run(["git", "config", "user.email", "devin@local"], cwd=str(project_path), capture_output=True, text=True)
        subprocess.run(["git", "config", "user.name", "Devin AI IDE"], cwd=str(project_path), capture_output=True, text=True)

        subprocess.run(["git", "add", "-A"], cwd=str(project_path), capture_output=True, text=True)
        subprocess.run(
            ["git", "commit", "-m", "Initial commit (auto, devin_ai_ide)"],
            cwd=str(project_path), capture_output=True, text=True
        )


def commit_changes(project_path, message):
    """Aggiunge e committa le modifiche correnti nel progetto."""
    project_path = Path(project_path)

    subprocess.run(["git", "add", "-A"], cwd=str(project_path), capture_output=True, text=True)
    result = subprocess.run(
        ["git", "commit", "-m", message],
        cwd=str(project_path), capture_output=True, text=True
    )
    return {
        "success": result.returncode == 0,
        "stdout": result.stdout,
        "stderr": result.stderr
    }


# === INTERFACCIA CLASSE PER ORCHESTRATOR ===
class GitOps:
    def __init__(self, project_path: str):
        self.project_path = project_path
        ensure_git_repo(project_path)

    def commit(self, patch: str, task: str):
        """Esegue il commit accettando i parametri passati dall'orchestratore."""
        message = f"Devin Auto-Commit: {task}\n\nPatch applied successfully."
        return commit_changes(self.project_path, message)