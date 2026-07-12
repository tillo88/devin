import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

from devin.agents.planner import Planner
from devin.agents.coder import Coder
from devin.core.context_engine import ContextEngine
from devin.ai.client import AIClient

project_path = "workspace/project/workspace"

print("=== 1. CONTEXT ENGINE ===")
ctx_engine = ContextEngine()
ctx = ctx_engine.build(project_path, query="trova e correggi il bug")
print(ctx[:800])

print("\n=== 2. PLANNER ===")
ai = AIClient()
planner = Planner(ai)
plan = planner.plan("trova e correggi il bug", ctx)
print(plan)

print("\n=== 3. CODER (deve restituire una unified diff) ===")
coder = Coder(ai)
patch = coder.generate(plan, ctx)
print(patch)