import shutil
from pathlib import Path

def create_sandbox(project_path, sandbox_root="workspace/sandbox"):
    """Copia il progetto in una sandbox isolata, escludendo directory ricorsive."""
    project_path = Path(project_path).resolve()
    sandbox_path = project_path / sandbox_root
    
    # CRITICAL: se sandbox esiste, rimuovila prima di ricrearla
    if sandbox_path.exists():
        shutil.rmtree(sandbox_path)
    
    # Crea la directory sandbox
    sandbox_path.mkdir(parents=True, exist_ok=True)
    
    # Pattern da ignorare per evitare ricorsione e copiare spazzatura
    ignore = shutil.ignore_patterns(
        "workspace", "venv", ".venv", "env", ".git",
        "__pycache__", "*.pyc", ".pytest_cache",
        "node_modules", "dist", "build", "*.gguf",
        "logs", "*.log", "backup_*", "project_dump.txt",
        ".devin", ".devin_chat"  # Modalita' Progetti: chat/knowledge non vanno in sandbox
    )

    # Copia SOLO i contenuti di primo livello del progetto, escludendo workspace
    for item in project_path.iterdir():
        if item.name in ("workspace", "venv", ".git", "__pycache__", "logs", ".devin", ".devin_chat"):
            continue
        dest = sandbox_path / item.name
        if item.is_dir():
            shutil.copytree(item, dest, ignore=ignore, dirs_exist_ok=True)
        else:
            shutil.copy2(item, dest)
    
    return sandbox_path


def read_code(sandbox_path):
    code = ""
    path = Path(sandbox_path)

    for f in path.rglob("*.py"):
        code += f"\n# FILE: {f.name}\n"
        code += f.read_text(errors="ignore") + "\n"

    return code