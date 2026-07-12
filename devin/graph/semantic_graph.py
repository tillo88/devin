import ast
from pathlib import Path

class SemanticGraph:

    def __init__(self, path):
        self.path = Path(path)

    def build(self):

        graph = []

        for file in self.path.rglob("*.py"):

            try:
                tree = ast.parse(file.read_text(errors="ignore"))
            except:
                continue

            for node in ast.walk(tree):

                if isinstance(node, ast.FunctionDef):
                    graph.append({
                        "file": str(file),
                        "type": "function",
                        "name": node.name
                    })

                if isinstance(node, ast.ClassDef):
                    graph.append({
                        "file": str(file),
                        "type": "class",
                        "name": node.name
                    })

        return graph