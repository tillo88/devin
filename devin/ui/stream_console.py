from devin.ai.client import AIClient

class StreamConsole:
    """
    Console interattiva con streaming reale dai modelli locali/rig.
    Usa AIClient per il routing automatico (rig -> locale -> cloud).
    """
    def __init__(self, mode="reasoning"):
        self.ai = AIClient()
        self.mode = mode

    def chat(self, prompt):
        """Stampa la risposta in streaming su stdout."""
        messages = [{"role": "user", "content": prompt}]
        print("🤖 ", end="", flush=True)
        for chunk in self.ai.stream(messages, mode=self.mode):
            print(chunk, end="", flush=True)
        print()

    def ask(self, prompt):
        """Ritorna la risposta completa come stringa."""
        messages = [{"role": "user", "content": prompt}]
        return "".join(self.ai.stream(messages, mode=self.mode))


if __name__ == "__main__":
    console = StreamConsole(mode="reasoning")
    print("💬 StreamConsole — scrivi 'exit' o premi Ctrl+C per uscire\n")
    while True:
        try:
            user_input = input("You: ")
            if user_input.lower() in ("exit", "quit", "q"):
                break
            console.chat(user_input)
        except (KeyboardInterrupt, EOFError):
            print("\n👋 Ciao!")
            break