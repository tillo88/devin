from devin.ai.client import AIClient

def stream_chat(prompt, mode="reasoning"):
    """
    Generatore che streama la risposta token per token.
    
    Uso:
        for chunk in stream_chat("Ciao", mode="reasoning"):
            print(chunk, end="", flush=True)
    """
    ai = AIClient()
    yield from ai.stream([{"role": "user", "content": prompt}], mode=mode)