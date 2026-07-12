"""
web_search.py - Provider astratto per ricerca web in chat.

Design: provider-agnostico così lo swap TinyFish -> SearXNG self-hosted (privacy-first)
richiede solo un cambio di config, zero refactoring lato chat.

TinyFish: pragmatico, gratuito, zero setup — ma cloud terzo (query + fetch passano
          dai loro server). Va bene come default rapido / fallback.
SearXNG:  self-hosted (docker), zero telemetria, nessuna query esce mai dalla tua rete
          verso un provider di ricerca terzo con la tua API key. Consigliato per privacy.
"""

import os
import requests
from typing import List, Dict, Any


class WebSearchProvider:
    def search(self, query: str, max_results: int = 5) -> List[Dict[str, Any]]:
        raise NotImplementedError


class SearXNGProvider(WebSearchProvider):
    """Privacy-first: metasearch self-hosted, nessun log, nessuna API key esterna."""

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")

    def search(self, query: str, max_results: int = 5) -> List[Dict[str, Any]]:
        r = requests.get(
            f"{self.base_url}/search",
            params={"q": query, "format": "json"},
            timeout=10
        )
        r.raise_for_status()
        results = r.json().get("results", [])[:max_results]
        return [
            {"title": x.get("title", ""), "url": x.get("url", ""), "snippet": x.get("content", "")}
            for x in results
        ]


class TinyFishProvider(WebSearchProvider):
    """Pragmatico: gratuito, zero setup, ma cloud terzo (query non restano in casa)."""

    def __init__(self, api_key: str):
        if not api_key:
            raise RuntimeError("TinyFish API key mancante")
        self.api_key = api_key

    def search(self, query: str, max_results: int = 5) -> List[Dict[str, Any]]:
        r = requests.get(
            "https://api.search.tinyfish.ai",
            params={"query": query},
            headers={"X-API-Key": self.api_key},
            timeout=10
        )
        r.raise_for_status()
        results = r.json().get("results", [])[:max_results]
        return [
            {
                "title": x.get("title", ""),
                "url": x.get("url", x.get("link", "")),
                "snippet": x.get("snippet", x.get("content", "")),
            }
            for x in results
        ]


def get_web_search_provider(config: Dict[str, Any]) -> WebSearchProvider:
    """
    Factory: legge config["web_search"]["provider"] ("searxng" | "tinyfish").
    Default: tinyfish (retrocompatibile con la key già in uso), ma settings.json
    di default punta a searxng non appena il container è su (vedi README).
    """
    ws_cfg = config.get("web_search", {})
    provider = ws_cfg.get("provider", "tinyfish")

    if provider == "searxng":
        base_url = ws_cfg.get("searxng_url")
        if not base_url:
            raise RuntimeError("web_search.searxng_url mancante in settings.json")
        return SearXNGProvider(base_url)

    api_key_env = ws_cfg.get("tinyfish_api_key_env", "TINYFISH_API_KEY")
    api_key = os.getenv(api_key_env)
    return TinyFishProvider(api_key)


def format_results_as_context(results: List[Dict[str, Any]]) -> str:
    """Formatta i risultati come blocco testuale da iniettare nel prompt della chat."""
    if not results:
        return "(nessun risultato)"
    lines = []
    for r in results:
        lines.append(f"- {r['title']}: {r['snippet']} ({r['url']})")
    return "\n".join(lines)


# =============================================================================
# Ricerca al servizio degli AGENTI (2026-07-10)
#
# DEVIN non e' una chat generica (quello e' Hermes): la ricerca serve al CODING.
# Questo helper viene chiamato dall'orchestratore quando il Critic incontra un
# errore "cercabile" (modulo mancante, API cambiata, versioni incompatibili):
# cerca l'errore, scarica la pagina migliore, e ritorna un blocco compatto da
# dare al Critic come riferimento REALE invece di farlo ragionare a memoria.
# =============================================================================

# Firme di errori per cui il web aiuta davvero (docs/issue/changelog), non
# errori di logica interna del progetto (per quelli cercare e' solo rumore).
SEARCHABLE_ERROR_PATTERNS = [
    "modulenotfounderror", "importerror", "no module named",
    "has no attribute", "unexpected keyword argument", "got an unexpected",
    "deprecat", "no matching distribution", "requires python",
    "incompatible", "not supported", "unknown option", "unrecognized arguments",
    "certificate verify failed", "ssl", "econnrefused",
]


def is_searchable_error(error: str) -> bool:
    err = (error or "").lower()
    return any(p in err for p in SEARCHABLE_ERROR_PATTERNS)


def search_coding_context(query: str, config: Dict[str, Any],
                           max_chars: int = 2000) -> str:
    """Ricerca compatta per gli agenti: snippet + contenuto della prima pagina
    utile. Fail-soft: '' su qualsiasi problema (rete giu', key mancante...)."""
    try:
        provider = get_web_search_provider(config)
        results = provider.search(query, max_results=4)
        if not results:
            return ""
        snippets = format_results_as_context(results)
        ws_cfg = config.get("web_search", {})
        page = fetch_top_results(results, max_pages=1,
                                  max_chars_per_page=max(600, max_chars - len(snippets)),
                                  engine=ws_cfg.get("fetch_engine", "requests"))
        block = snippets if not page else f"{snippets}\n\n{page}"
        return block[:max_chars]
    except Exception as e:
        print(f"[WebSearch] search_coding_context fallita: {e}")
        return ""


# =============================================================================
# Fetch + estrazione contenuto pagine (2026-07-10)
#
# Problema osservato: la ricerca torna solo titolo+snippet — per domande tipo
# "che dice skysport sui mondiali" il modello non ha il CONTENUTO della pagina
# e improvvisa. Qui: fetch dei top risultati con User-Agent da browser (molti
# siti rispondono 403 allo UA di default di requests) + estrazione testo con
# la stdlib (niente dipendenze nuove).
#
# Limite onesto: pagine renderizzate via JavaScript (SPA) restituiscono poco o
# nulla — per quelle serve un browser headless (Playwright), previsto come
# passo futuro (meglio quando DEVIN girera' sul rig, vicino al modello).
# =============================================================================

from html.parser import HTMLParser

_BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
_SKIP_TAGS = {"script", "style", "noscript", "svg", "nav", "footer", "header", "aside", "form"}


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self.parts: List[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            self._skip_depth += 1

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data):
        if self._skip_depth == 0 and data.strip():
            self.parts.append(data.strip())


def fetch_page_text(url: str, max_chars: int = 2500, timeout: int = 10) -> str:
    """Testo leggibile di una pagina. Stringa vuota su QUALSIASI errore (fail-soft:
    un sito che blocca non deve rompere la risposta, al massimo manca una fonte)."""
    try:
        r = requests.get(url, timeout=timeout, headers={
            "User-Agent": _BROWSER_UA,
            "Accept-Language": "it-IT,it;q=0.9,en;q=0.7",
        })
        r.raise_for_status()
        ctype = r.headers.get("content-type", "")
        if "html" not in ctype and "text" not in ctype:
            return ""
        parser = _TextExtractor()
        parser.feed(r.text[:400_000])  # protezione: pagine enormi
        text = " ".join(parser.parts)
        # comprimi spazi multipli
        text = " ".join(text.split())
        return text[:max_chars]
    except Exception as e:
        print(f"[WebSearch] fetch fallito per {url}: {e}")
        return ""


def _fetch_pages_playwright(urls: List[str], max_chars: int = 2500,
                             per_page_timeout: int = 10) -> Dict[str, str]:
    """Fetch di PIU' pagine con UN SOLO browser headless (2026-07-10: prima si
    lanciava un Chromium intero per ogni pagina — 2-3s di avvio l'uno, era la
    causa principale della lentezza percepita).

    Prerequisiti (una volta sola, nel venv):
        pip install playwright && playwright install chromium

    Gira in un thread dedicato (la Sync API di Playwright rifiuta di partire
    dentro un event loop asyncio attivo). Fail-soft: {} o valori '' sugli
    errori, mai eccezioni."""
    import concurrent.futures

    def _run() -> Dict[str, str]:
        out: Dict[str, str] = {}
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            # --disable-http2: alcuni CDN anti-bot (visto su olympics.com)
            # chiudono l'HTTP/2 di Chromium headless con ERR_HTTP2_PROTOCOL_ERROR.
            browser = p.chromium.launch(headless=True, args=["--disable-http2"])
            try:
                ctx = browser.new_context(user_agent=_BROWSER_UA, locale="it-IT")
                for url in urls:
                    try:
                        page = ctx.new_page()
                        page.goto(url, timeout=per_page_timeout * 1000,
                                  wait_until="domcontentloaded")
                        page.wait_for_timeout(700)  # respiro per i render JS rapidi
                        text = page.inner_text("body")
                        out[url] = " ".join(text.split())[:max_chars]
                        page.close()
                    except Exception as e:
                        print(f"[WebSearch] playwright fetch fallito per {url}: {e}")
                        out[url] = ""
            finally:
                browser.close()
        return out

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            return ex.submit(_run).result(timeout=per_page_timeout * max(1, len(urls)) + 25)
    except Exception as e:
        print(f"[WebSearch] playwright non disponibile/batch fallito: {e}")
        return {}


def fetch_top_results(results: List[Dict[str, Any]], max_pages: int = 2,
                      max_chars_per_page: int = 2500, engine: str = "requests") -> str:
    """Scarica il contenuto dei primi max_pages risultati e lo formatta come
    blocco contesto. Salta in silenzio le pagine che falliscono/sono vuote.

    engine: "requests" (leggero) | "playwright" (browser headless, rende il JS).
    Latenza: con playwright UN solo avvio browser per l'intera chiamata; il
    fallback requests scatta solo se playwright e' proprio indisponibile —
    NON per ogni singola pagina bloccata (un sito che blocca Chromium blocca
    quasi sempre anche requests: sarebbero solo timeout doppi)."""
    urls = [r.get("url", "") for r in results if r.get("url")]
    if not urls:
        return ""

    pw_texts: Dict[str, str] = {}
    pw_available = False
    if engine == "playwright":
        pw_texts = _fetch_pages_playwright(urls[:max_pages + 2],
                                            max_chars=max_chars_per_page)
        pw_available = bool(pw_texts)  # {} = playwright non installato/rotto

    blocks = []
    for r in results:
        if len(blocks) >= max_pages:
            break
        url = r.get("url", "")
        if not url:
            continue
        if pw_available:
            text = pw_texts.get(url, "")
        else:
            text = fetch_page_text(url, max_chars=max_chars_per_page, timeout=8)
        if len(text) > 200:  # sotto questa soglia e' quasi certamente boilerplate/errore
            blocks.append(f"[Contenuto da {r.get('title', url)} — {url}]\n{text}")
    return "\n\n".join(blocks)
