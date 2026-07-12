#!/usr/bin/env python3
"""
Test per verificare che lo streaming sia correttamente integrato.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

from devin.ai.client import AIClient
from devin.ai.stream import stream_chat
from devin.ui.stream_console import StreamConsole

def test_stream_method_exists():
    client = AIClient()
    assert hasattr(client, 'stream'), "AIClient deve avere il metodo stream()"
    print("✓ AIClient.stream() esiste")

def test_stream_returns_generator():
    client = AIClient()
    gen = client.stream([{"role": "user", "content": "test"}], mode="reasoning")
    assert hasattr(gen, '__iter__'), "stream() deve ritornare un generatore"
    print("✓ stream() ritorna un generatore")

def test_stream_chat_generator():
    gen = stream_chat("test", mode="coder")
    assert hasattr(gen, '__iter__')
    print("✓ stream_chat() ritorna un generatore")

def test_stream_console_init():
    console = StreamConsole(mode="coder")
    assert console.mode == "coder"
    print("✓ StreamConsole init OK")

def test_syntax_all_files():
    import py_compile
    files = [
        "devin/ai/client.py",
        "devin/ai/stream.py",
        "devin/ui/stream_console.py",
    ]
    for f in files:
        py_compile.compile(f, doraise=True)
    print("✓ Sintassi OK per tutti i file streaming")

if __name__ == "__main__":
    print("🧪 Test Streaming Module\n")
    test_stream_method_exists()
    test_stream_returns_generator()
    test_stream_chat_generator()
    test_stream_console_init()
    test_syntax_all_files()
    print("\n🎉 Tutti i test streaming passati!")