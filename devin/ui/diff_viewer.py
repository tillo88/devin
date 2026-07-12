import difflib
import tkinter as tk

def show_diff(old_code, new_code):

    diff = difflib.unified_diff(
        old_code.splitlines(),
        new_code.splitlines(),
        lineterm=""
    )

    window = tk.Toplevel()
    window.title("AI Diff Viewer")

    text = tk.Text(window)
    text.pack(fill="both", expand=True)

    text.insert("1.0", "\n".join(diff))