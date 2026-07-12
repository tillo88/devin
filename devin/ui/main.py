import tkinter as tk
from tkinter import simpledialog, scrolledtext
import threading

from devin.ui.editor import Editor
from devin.core.orchestrator import Orchestrator


class App:

    def __init__(self):
        self.root = tk.Tk()
        self.root.title("DEVIN v14 - LOCAL AI IDE")

        self.editor = Editor(self.root)

        self.output = scrolledtext.ScrolledText(self.root, height=10, width=80)
        self.output.pack()

        self.btn = tk.Button(
            self.root,
            text="RUN AI AGENT",
            command=self.run_agent
        )
        self.btn.pack()

    def run_agent(self):
        def task():
            project_path = simpledialog.askstring("Project Path", "Enter project path:")
            if not project_path:
                return

            try:
                with Orchestrator(project_path=project_path) as orch:
                    result = orch.run("Analyze and fix project", project_path=project_path)
                self.output.insert("end", f"\n=== RESULT ===\n{result}\n")
            except Exception as e:
                self.output.insert("end", f"\n=== ERROR ===\n{str(e)}\n")

        threading.Thread(target=task).start()

    def start(self):
        self.root.mainloop()


if __name__ == "__main__":
    App().start()