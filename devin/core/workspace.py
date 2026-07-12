from pathlib import Path

def get_workspace(project_path):

    path = Path(project_path)

    # struttura base
    (path / "workspace").mkdir(parents=True, exist_ok=True)
    (path / "patches").mkdir(parents=True, exist_ok=True)

    return path / "workspace"