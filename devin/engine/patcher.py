import re
import shutil
import subprocess
import tempfile
import hashlib
from pathlib import Path
from devin.engine.sandbox import create_sandbox

HEADER_PREFIXES = (
    "diff --git", "index ", "---", "+++",
    "new file mode", "deleted file mode", "old mode", "new mode",
    "similarity index", "rename from", "rename to"
)


def _clean_patch_text(patch_text):
    lines = patch_text.strip().splitlines()
    lines = [l for l in lines if not re.match(r"^```", l.strip())]

    cleaned = []
    in_hunk = False
    started = False

    for line in lines:
        if line.startswith(HEADER_PREFIXES):
            cleaned.append(line)
            started = True
            in_hunk = False
            continue

        if line.startswith("@@"):
            cleaned.append(line)
            started = True
            in_hunk = True
            continue

        if in_hunk and (line == "" or line.startswith((" ", "+", "-"))):
            cleaned.append(line)
            continue

        if started:
            break

    return "\n".join(cleaned) + "\n"


def _validate_diff_structure(patch_text):
    lines = patch_text.splitlines()
    in_hunk = False

    for i, line in enumerate(lines):
        if line.startswith("@@"):
            in_hunk = True
            continue

        if line.startswith(HEADER_PREFIXES):
            in_hunk = False
            continue

        if in_hunk and line != "" and not line.startswith((" ", "+", "-")):
            return f"Line {i+1} missing valid prefix: {line!r}"

    return None


def _file_hash(filepath):
    """MD5 del file per verificare se è cambiato."""
    h = hashlib.md5()
    h.update(Path(filepath).read_bytes())
    return h.hexdigest()


def _try_git_apply(tmp_path, target_dir):
    if not shutil.which("git"):
        return None

    for strip_level in range(0, 5):
        result = subprocess.run(
            ["git", "apply", "--check", f"-p{strip_level}", tmp_path],
            cwd=str(target_dir),
            text=True,
            capture_output=True,
            input=""
        )
        if result.returncode == 0:
            # Solo se il check passa, applica davvero
            result2 = subprocess.run(
                ["git", "apply", f"-p{strip_level}", tmp_path],
                cwd=str(target_dir),
                text=True,
                capture_output=True,
                input=""
            )
            if result2.returncode == 0:
                return {
                    "success": True, "tool": "git apply", "strip_level": strip_level,
                    "stdout": result2.stdout, "stderr": result2.stderr
                }
    return None


def _try_patch(tmp_path, target_dir):
    results = []

    for strip_level in range(0, 5):
        result = subprocess.run(
            ["patch", f"-p{strip_level}", "--fuzz=10", "-i", tmp_path],
            cwd=str(target_dir),
            text=True,
            capture_output=True,
            input=""
        )
        results.append((strip_level, result))

        if result.returncode == 0:
            return {
                "success": True, "tool": "patch", "strip_level": strip_level,
                "stdout": result.stdout, "stderr": result.stderr
            }

    best = None
    for strip_level, result in results:
        if "can\'t find file to patch" not in result.stdout:
            best = (strip_level, result)
            break

    if best is None:
        best = results[-1]

    strip_level, result = best
    return {
        "success": False, "tool": "patch", "strip_level_tried": strip_level,
        "stdout": result.stdout, "stderr": result.stderr,
        "all_attempts": [r.stdout for _, r in results]
    }


def _parse_hunks(patch_text):
    """Parser condiviso dei fallback Python (riscritto 2026-07-10).

    DUE bug storici corretti qui, root cause dei fallimenti sistematici visti
    su run_20260710_094808 (fuzzy che falliva perfino sui FILE NUOVI):

    1. Le righe header `--- a/file` e `+++ b/file` INIZIANO con '-' e '+':
       il vecchio parser le ingoiava come righe di contenuto, creando hunk
       fantasma corrotti prima di ogni hunk vero (es. un "hunk" che voleva
       rimuovere la riga '-- /dev/null'). Ora gli header vengono saltati.
    2. Il flag 'nuovo file' ('--- /dev/null') e' ora rilevato dagli header,
       per-file, invece che indovinato dall'esistenza del file su disco.

    Ritorna (hunks, new_files): hunks = [(filepath, [righe hunk])],
    new_files = set di filepath marcati nuovi nel diff.
    """
    hunks = []
    new_files = set()
    current_file = None
    current_hunk = []

    def _flush():
        nonlocal current_hunk
        if current_file and current_hunk:
            hunks.append((current_file, current_hunk))
        current_hunk = []

    for line in patch_text.splitlines():
        if line.startswith("diff --git"):
            _flush()
            m = re.match(r'diff --git a/(.+?) b/(.+)', line)
            current_file = m.group(2) if m else None
        elif line.startswith("--- "):
            if line.startswith("--- /dev/null") and current_file:
                new_files.add(current_file)
        elif line.startswith("+++ "):
            # header, non contenuto (e "+++ b/x" puo' anche definire il file
            # se manca la riga "diff --git" — capita nei diff dei modelli)
            if current_file is None:
                m = re.match(r'\+\+\+ b/(.+)', line)
                if m:
                    current_file = m.group(1)
        elif line.startswith(HEADER_PREFIXES):
            continue
        elif line.startswith("@@"):
            _flush()
        elif line.startswith((" ", "+", "-")) or line == "":
            current_hunk.append(line)

    _flush()
    return hunks, new_files


def _apply_diff_python(patch_text, target_dir, strict=True):
    """
    Fallback Python: applica una unified diff con match ESATTO sul blocco
    contesto+rimozioni (2026-07-10: prima usava solo le righe '-', quindi gli
    hunk di sole aggiunte venivano inseriti alla cieca in cima al file).
    """
    target_dir = Path(target_dir)
    hunks, new_files = _parse_hunks(patch_text)

    applied = 0
    failed = []
    failed_hunk_idx = []

    for hunk_idx, (filepath, hunk) in enumerate(hunks):
        if not filepath:
            continue
        target_file = target_dir / filepath

        # Nuovo file dichiarato dal diff, o comunque assente su disco
        if filepath in new_files or not target_file.exists():
            add_lines = [l[1:] for l in hunk if l.startswith("+")]
            if add_lines:
                target_file.parent.mkdir(parents=True, exist_ok=True)
                if target_file.exists():  # secondo hunk dello stesso file nuovo
                    existing = target_file.read_text(encoding="utf-8", errors="ignore")
                    target_file.write_text(existing + "\n".join(add_lines) + "\n", encoding="utf-8")
                else:
                    target_file.write_text("\n".join(add_lines) + "\n", encoding="utf-8")
                applied += 1
            continue

        # File esistente: match sul blocco contesto+rimozioni (semantica
        # unified diff corretta), sostituito con contesto+aggiunte.
        original = target_file.read_text(encoding="utf-8", errors="ignore")
        original_lines = original.splitlines()

        old_block = [l[1:] for l in hunk if l.startswith((" ", "-")) or l == ""]
        new_block = [l[1:] for l in hunk if l.startswith((" ", "+")) or l == ""]

        if not any(l.strip() for l in old_block):
            # Sole aggiunte senza alcun contesto su file esistente: inserire
            # alla cieca (il vecchio codice le metteva IN CIMA al file) fa piu'
            # danni che fallire — lascia lavorare fuzzy/Critic.
            failed.append(filepath)
            failed_hunk_idx.append(hunk_idx)
            continue

        modified = False
        for i in range(len(original_lines) - len(old_block) + 1):
            if original_lines[i:i + len(old_block)] == old_block:
                original_lines[i:i + len(old_block)] = new_block
                modified = True
                break

        if modified:
            target_file.write_text("\n".join(original_lines) + "\n", encoding="utf-8")
            applied += 1
        else:
            failed.append(filepath)
            failed_hunk_idx.append(hunk_idx)

    return {
        "success": len(failed) == 0 and applied > 0,
        "tool": "python fallback",
        "applied": applied,
        "failed": failed,
        "failed_hunks": failed_hunk_idx,
    }


def _apply_diff_python_fuzzy(patch_text, target_dir, only_files=None):
    """
    Fallback Python con fuzzy matching sul blocco contesto+rimozioni
    (riscritto 2026-07-10 insieme a _parse_hunks — vedi nota li' per i bug
    storici: header ingoiati come contenuto, contesto buttato via).

    only_files: se valorizzato, processa SOLO quegli INDICI di hunk (nonostante
    il nome storico) — usato dopo lo strict per riprovare esclusivamente gli
    hunk falliti: riprocessare anche quelli gia' applicati su una sandbox gia'
    mutata li ricontava come falliti (visto su run_20260710_100655: hunk 2
    applicato dallo strict, poi 'fallito' dal fuzzy) o li duplicava.
    """
    target_dir = Path(target_dir)
    hunks, new_files = _parse_hunks(patch_text)

    applied = 0
    failed = []

    for hunk_idx, (filepath, hunk) in enumerate(hunks):
        if not filepath:
            continue
        if only_files is not None and hunk_idx not in only_files:
            continue
        target_file = target_dir / filepath

        if filepath in new_files or not target_file.exists():
            add_lines = [l[1:] for l in hunk if l.startswith("+")]
            if add_lines:
                target_file.parent.mkdir(parents=True, exist_ok=True)
                if target_file.exists():
                    existing = target_file.read_text(encoding="utf-8", errors="ignore")
                    target_file.write_text(existing + "\n".join(add_lines) + "\n", encoding="utf-8")
                else:
                    target_file.write_text("\n".join(add_lines) + "\n", encoding="utf-8")
                applied += 1
            continue

        original = target_file.read_text(encoding="utf-8", errors="ignore")
        original_lines = original.splitlines()

        old_block = [l[1:] for l in hunk if l.startswith((" ", "-")) or l == ""]
        new_block = [l[1:] for l in hunk if l.startswith((" ", "+")) or l == ""]

        # taglia righe vuote in testa/coda del blocco da cercare (allineamento)
        while old_block and not old_block[0].strip():
            old_block.pop(0)
            if new_block and not new_block[0].strip():
                new_block.pop(0)
        while old_block and not old_block[-1].strip():
            old_block.pop()
            if new_block and not new_block[-1].strip():
                new_block.pop()

        if not old_block:
            failed.append(filepath)
            continue

        modified = False
        if len(old_block) == 1 and len(new_block) == 1:
            # sostituzione di una singola riga: match per sottostringa
            old_line = old_block[0].strip()
            for i, line in enumerate(original_lines):
                if old_line and old_line in line:
                    original_lines[i] = new_block[0]
                    modified = True
                    break
        else:
            # blocco: anchor sulla prima riga, verifica approssimata sulle altre
            anchor = old_block[0].strip()
            for i in range(len(original_lines)):
                if anchor and anchor in original_lines[i]:
                    match = True
                    for j, ob in enumerate(old_block):
                        if not ob.strip():
                            continue  # le righe vuote non vincolano il match
                        if i + j >= len(original_lines) or ob.strip() not in original_lines[i + j]:
                            match = False
                            break
                    if match:
                        original_lines[i:i + len(old_block)] = new_block
                        modified = True
                        break

        if modified:
            target_file.write_text("\n".join(original_lines) + "\n", encoding="utf-8")
            applied += 1
        else:
            failed.append(filepath)

    return {
        "success": len(failed) == 0 and applied > 0,
        "tool": "python fuzzy fallback",
        "applied": applied,
        "failed": failed
    }


def apply_patch(patch_text, target_dir):
    target_dir = Path(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)

    if not patch_text or not patch_text.strip():
        return {"success": False, "error": "Empty or invalid patch"}

    cleaned = _clean_patch_text(patch_text)
    print(f"[apply_patch] Cleaned diff: {len(cleaned.splitlines())} lines")

    validation_error = _validate_diff_structure(cleaned)
    if validation_error:
        return {"success": False, "error": f"Malformed diff: {validation_error}"}

    # Calcola hash pre-patch per verifica
    pre_hashes = {}
    for f in target_dir.rglob("*"):
        if f.is_file():
            pre_hashes[str(f.relative_to(target_dir))] = _file_hash(f)

    with tempfile.NamedTemporaryFile(mode="w", suffix=".patch", delete=False) as tmp:
        tmp.write(cleaned)
        tmp_path = tmp.name

    try:
        # 1. Prova git apply (con --check prima)
        git_result = _try_git_apply(tmp_path, target_dir)
        if git_result and git_result.get("success"):
            # Verifica che qualcosa sia effettivamente cambiato
            changed = False
            for f in target_dir.rglob("*"):
                if f.is_file():
                    rel = str(f.relative_to(target_dir))
                    if rel in pre_hashes and _file_hash(f) != pre_hashes[rel]:
                        changed = True
                        break
            if changed:
                print(f"[apply_patch] Applied via git apply (p{git_result['strip_level']}) — file changed confirmed")
                return git_result
            else:
                print(f"[apply_patch] git apply returned 0 but NO file changed! Trying fallback...")

        # 2. Prova patch
        patch_result = _try_patch(tmp_path, target_dir)
        if patch_result and patch_result.get("success"):
            changed = False
            for f in target_dir.rglob("*"):
                if f.is_file():
                    rel = str(f.relative_to(target_dir))
                    if rel in pre_hashes and _file_hash(f) != pre_hashes[rel]:
                        changed = True
                        break
            if changed:
                print(f"[apply_patch] Applied via patch (p{patch_result['strip_level']}) — file changed confirmed")
                return patch_result
            else:
                print(f"[apply_patch] patch returned 0 but NO file changed! Trying fallback...")

        # 3. Fallback Python strict
        print(f"[apply_patch] Trying Python strict fallback...")
        py_result = _apply_diff_python(cleaned, target_dir, strict=True)
        if py_result.get("success"):
            print(f"[apply_patch] Applied via Python strict ({py_result['applied']} files)")
            return py_result

        # 4. Fallback Python fuzzy — SOLO sui file falliti dallo strict
        # (2026-07-10: prima rifaceva tutto il diff su una sandbox gia' mutata
        # dallo strict parziale — hunk gia' applicati risultavano "falliti" o
        # venivano applicati due volte).
        strict_failed_hunks = set(py_result.get("failed_hunks") or [])
        print(f"[apply_patch] Python strict failed on hunks {sorted(strict_failed_hunks)} "
              f"(files: {sorted(set(py_result.get('failed') or []))}), trying fuzzy on those...")
        fuzzy_result = _apply_diff_python_fuzzy(cleaned, target_dir,
                                                 only_files=strict_failed_hunks or None)
        total_applied = (py_result.get("applied") or 0) + (fuzzy_result.get("applied") or 0)
        if not fuzzy_result.get("failed") and total_applied > 0:
            print(f"[apply_patch] Applied via Python strict+fuzzy ({total_applied} hunks)")
            return {"success": True, "tool": "python strict+fuzzy", "applied": total_applied,
                    "failed": []}

        return {
            "success": False,
            "error": f"All patch methods failed. Fuzzy failed on: {fuzzy_result.get('failed')}",
        }

    except Exception as e:
        return {"success": False, "error": str(e)}

    finally:
        Path(tmp_path).unlink(missing_ok=True)


class Patcher:
    def __init__(self):
        pass

    def apply(self, patch_text: str, project_path: str) -> Path:
        sandbox_path = create_sandbox(project_path)
        
        print(f"[Patcher] Applying diff ({len(patch_text.splitlines())} lines) to {sandbox_path}")
        
        res = apply_patch(patch_text, sandbox_path)
        if not res.get("success"):
            raise RuntimeError(res.get("error") or "Patch application failed in sandbox")
        
        # Verifica contenuto file modificati
        for f in sandbox_path.rglob("*.py"):
            rel = f.relative_to(sandbox_path)
            content = f.read_text(encoding="utf-8", errors="ignore")
            print(f"[Patcher] {rel}: {len(content)} chars")
            for i, line in enumerate(content.splitlines()[:5], 1):
                print(f"  L{i}: {line}")

        return sandbox_path

    def apply_full_files(self, files: dict, project_path: str) -> Path:
        """Modalità WHOLE-FILE: scrive il contenuto COMPLETO di ciascun file nel
        sandbox, sovrascrivendo/creando, senza passare dall'unified diff. Usato
        dall'Orchestrator per i progetti piccoli (il Coder riscrive i file interi).

        files = {rel_path: full_content}. Guardia anti path-traversal: nessuna
        scrittura fuori dalla root del sandbox (chiude AUDIT #8 per questo path)."""
        if not files:
            raise RuntimeError("Whole-file: il Coder non ha prodotto alcun file")

        sandbox_path = create_sandbox(project_path)
        sandbox_root = sandbox_path.resolve()
        written = []
        for rel_path, content in files.items():
            target = (sandbox_path / rel_path).resolve()
            # deve restare dentro il sandbox
            if sandbox_root != target and sandbox_root not in target.parents:
                raise RuntimeError(f"Whole-file: percorso non sicuro fuori dal sandbox: {rel_path!r}")
            target.parent.mkdir(parents=True, exist_ok=True)
            if content and not content.endswith("\n"):
                content += "\n"
            target.write_text(content, encoding="utf-8")
            written.append(rel_path)
            print(f"[Patcher] whole-file scritto {rel_path}: {len(content)} chars")

        return sandbox_path