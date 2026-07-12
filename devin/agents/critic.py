from dataclasses import dataclass
from devin.ai.client import AIClient
from devin.agents.prompts import CRITIC_SYSTEM_PROMPT


@dataclass
class Critique:
    feedback: str


class Critic:
    def __init__(self, ai_client: AIClient):
        self.ai = ai_client

    def analyze(self, error, patch, context, sandbox_files=None) -> Critique:
        sandbox_info = ""
        if sandbox_files:
            sandbox_info = "\n\nACTUAL FILES IN SANDBOX AFTER PATCH:\n"
            for path, content in sandbox_files.items():
                sandbox_info += f"\n# FILE: {path}\n{content[:800]}\n"

        messages = [
            {"role": "system", "content": CRITIC_SYSTEM_PROMPT},
            {
                "role": "user", 
                "content": f"""
ERROR:
{error}

PATCH APPLIED:
{patch}

CODE CONTEXT:
{context[:4000]}
{sandbox_info}

Analyze the root cause and propose a concrete fix.
"""
            }
        ]

        response = self.ai.local(messages, mode="reasoning", timeout=60)
        feedback_text = response if isinstance(response, str) else str(response)
        return Critique(feedback=feedback_text)