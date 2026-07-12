import tkinter as tk
from devin.core.orchestrator import Orchestrator


class App:

    def __init__(self, root):
        self.root = root
        self.root.title("DEVIN AI IDE v19")

        self.path_entry = tk.Entry(root, width=60)
        self.path_entry.pack(pady=10)

        self.run_button = tk.Button(
            root,
            text="Run Agent",
            command=self.run_agent
        )
        self.run_button.pack(pady=5)

        self.output_text = tk.Text(root, height=20, width=100)
        self.output_text.pack(pady=10)

    def run_agent(self):
        project_path = self.path_entry.get().strip()

        self.output_text.insert("end", "\n🚀 RUN STARTED\n")
        self.output_text.insert("end", f"📂 Path: {project_path}\n")

        try:
            with Orchestrator(project_path=project_path) as orch:
                result = orch.run("Fix import error in main.py", project_path=project_path)

            self.output_text.insert("end", "\n📦 RESULT:\n")
            self.output_text.insert("end", str(result) + "\n")

        except Exception as e:
            self.output_text.insert("end", f"\n❌ ERROR: {str(e)}\n")


def start_ui():
    root = tk.Tk()
    app = App(root)
    root.mainloop()