import ast
from pathlib import Path

class CodeGraph:

    def __init__(self, project_path):
        self.project_path = Path(project_path)
        self.graph = {}

    def build(self):

        for file in self.project_path.rglob("*.py"):

            with open(file, "r", encoding="utf-8") as f:
                tree = ast.parse(f.read())

            functions = []

            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef):
                    functions.append(node.name)

            self.graph[file.name] = functions

        return self.graph