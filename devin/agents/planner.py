import json
import re
from dataclasses import dataclass, field
from typing import List, Dict, Any
from devin.ai.client import AIClient
from devin.agents.prompts import PLANNER_SYSTEM_PROMPT, SCAFFOLD_PLANNER_SYSTEM_PROMPT


@dataclass
class Plan:
    """Struttura dati per il piano richiesta dall'Orchestrator."""
    steps: List[str] = field(default_factory=list)
    raw_response: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "steps": self.steps,
            "raw_response": self.raw_response
        }


def _extract_file_plan(raw: str) -> List[Dict[str, str]]:
    """
    Estrae una lista [{"filename": ..., "spec": ...}, ...] dalla risposta LLM per lo
    Zero-Shot Scaffolding. Tollerante a markdown-fence e testo extra prima/dopo il JSON.
    Ritorna lista vuota se non trova nulla di valido (mai un'eccezione verso il caller).
    """
    if not raw:
        return []

    # 1. Cerca blocco ```json ... ```
    m = re.search(r'```(?:json)?\s*(\[.*?\])\s*```', raw, re.DOTALL)
    candidate = m.group(1) if m else raw

    # 2. Fallback: prima occorrenza di un array JSON nel testo
    m2 = re.search(r'(\[.*\])', candidate, re.DOTALL)
    if m2:
        candidate = m2.group(1)

    try:
        data = json.loads(candidate)
    except json.JSONDecodeError:
        return []

    out = []
    for item in data:
        if isinstance(item, dict) and "filename" in item:
            out.append({"filename": item["filename"], "spec": item.get("spec", "")})
    return out


class Planner:
    def __init__(self, ai_client: AIClient):
        """Inizializza il Planner usando il client gestito dall'orchestratore."""
        self.client = ai_client

    def plan(self, task: str, context: str) -> Plan:
        """
        Genera un piano di esecuzione step-by-step basato su task e contesto.
        Usato in Modalità 1 (Mantenimento): patching su codice esistente.
        """
        messages = [
            {"role": "system", "content": PLANNER_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": f"""
TASK RICHIESTO:
{task}

CONTESTO DEL PROGETTO (file esistenti, se presenti):
{context if context.strip() else "(nessun file presente — progetto vuoto, da costruire da zero)"}

Crea un piano di esecuzione step-by-step per soddisfare il task richiesto.
"""
            }
        ]

        # Utilizza l'istanza del client passata (configurata dinamicamente con i giusti URL)
        response = self.client.local(messages, mode="reasoning", timeout=60)

        steps = []
        raw_text = ""

        # Gestione della risposta per estrarre la lista degli step
        if isinstance(response, str):
            raw_text = response
            try:
                # Se l'LLM risponde in JSON strutturato
                data = json.loads(response)
                if isinstance(data, dict):
                    steps = data.get("steps", [raw_text])
                elif isinstance(data, list):
                    steps = data
            except json.JSONDecodeError:
                # Fallback: Se risponde in testo/Markdown, estrae le righe puntate o numerate
                steps = [
                    line.strip().lstrip("-*0123456789. ")
                    for line in response.split("\n")
                    if line.strip() and (line.strip().startswith("-") or line.strip().startswith("*") or line.strip()[0].isdigit())
                ]
                if not steps:
                    steps = [response]
        elif isinstance(response, dict):
            steps = response.get("steps", [])
            raw_text = json.dumps(response)
        else:
            raw_text = str(response)
            steps = [raw_text]

        return Plan(steps=steps, raw_response=raw_text)

    def plan_scaffold(self, task: str) -> List[Dict[str, str]]:
        """
        Modalità 2 (Zero-Shot Scaffolding): genera l'elenco dei file da creare da zero,
        in ordine di creazione, con una spec concreta per ciascuno.
        Ritorna lista vuota (mai eccezione) se il parsing fallisce — il caller
        (Orchestrator.run_scaffold) deve gestire questo caso come errore recuperabile.
        """
        messages = [
            {"role": "system", "content": SCAFFOLD_PLANNER_SYSTEM_PROMPT},
            {"role": "user", "content": f"TASK: {task}\n\nGenera l'elenco file in formato JSON."}
        ]
        raw = self.client.local(messages, mode="reasoning", timeout=60) or ""
        return _extract_file_plan(raw)
