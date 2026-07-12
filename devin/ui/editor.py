import tkinter as tk

class Editor:

    def __init__(self, root):

        self.text = tk.Text(root, wrap="none")
        self.text.pack(fill="both", expand=True)

    def get_code(self):
        return self.text.get("1.0", "end")

    def set_code(self, code):
        self.text.delete("1.0", "end")
        self.text.insert("1.0", code)