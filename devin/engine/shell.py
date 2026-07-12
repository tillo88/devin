import subprocess


def run_shell(command, cwd, timeout=60):
    """Esegue un comando shell arbitrario in modo controllato (timeout, cwd fissata).

    ⚠️ Nota di sicurezza: oggi questo tool viene chiamato solo da codice nostro
    (es. installazione dipendenze), NON da scelte autonome del modello.
    Quando in v25+ daremo all'agente la libertà di scegliere comandi, andrà
    aggiunta una whitelist/sandboxing più stretto — qui per ora teniamo
    solo timeout e cwd fissa come protezioni minime.
    """
    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout
        )
        return {
            "success": result.returncode == 0,
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr
        }
    except subprocess.TimeoutExpired:
        return {"success": False, "returncode": -1, "stdout": "", "stderr": f"Timeout dopo {timeout}s"}
    except Exception as e:
        return {"success": False, "returncode": -1, "stdout": "", "stderr": str(e)}
