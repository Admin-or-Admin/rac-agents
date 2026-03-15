import os
import json
import time
import hashlib
from typing import List
from dotenv import load_dotenv

load_dotenv()

KNOWLEDGE_DIR  = os.getenv("KNOWLEDGE_DIR", "classifierKnowledge")
CACHE_DIR      = os.path.join(KNOWLEDGE_DIR, ".cache")
CACHE_FILE     = os.path.join(CACHE_DIR, "embeddings.json")
MANIFEST_FILE  = os.path.join(CACHE_DIR, "manifest.json")

CHUNK_SIZE     = int(os.getenv("KNOWLEDGE_CHUNK_SIZE", "500"))
CHUNK_OVERLAP  = int(os.getenv("KNOWLEDGE_CHUNK_OVERLAP", "50"))
TOP_K_CHUNKS   = int(os.getenv("KNOWLEDGE_TOP_K", "5"))
EMBED_MODEL    = "text-embedding-3-small"

# ── File readers ──────────────────────────────────────────────────────────────

def _read_pdf(path: str) -> str:
    import fitz
    doc = fitz.open(path)
    return "\n".join(page.get_text() for page in doc)


def _read_docx(path: str) -> str:
    import docx
    doc = docx.Document(path)
    return "\n".join(p.text for p in doc.paragraphs if p.text.strip())


def _read_txt(path: str) -> str:
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        return f.read()


_READERS = {
    ".pdf":  _read_pdf,
    ".docx": _read_docx,
    ".txt":  _read_txt,
    ".md":   _read_txt,
}

# ── Manifest — used to detect changes in the knowledge folder ─────────────────

def _build_manifest() -> dict:
    """
    Walk the knowledge folder and record each file's name, size, and
    last-modified time. This is compared against the saved manifest to
    decide whether the cache is still valid.
    """
    manifest = {}

    if not os.path.isdir(KNOWLEDGE_DIR):
        return manifest

    for filename in sorted(os.listdir(KNOWLEDGE_DIR)):
        ext = os.path.splitext(filename)[1].lower()
        if ext not in _READERS:
            continue

        path = os.path.join(KNOWLEDGE_DIR, filename)
        stat = os.stat(path)
        manifest[filename] = {
            "size":  stat.st_size,
            "mtime": stat.st_mtime,
        }

    return manifest


def _cache_is_valid() -> bool:
    """Return True if the cache exists and the knowledge folder has not changed."""
    if not os.path.isfile(CACHE_FILE) or not os.path.isfile(MANIFEST_FILE):
        return False

    try:
        with open(MANIFEST_FILE, "r") as f:
            saved_manifest = json.load(f)
    except Exception:
        return False

    current_manifest = _build_manifest()
    return current_manifest == saved_manifest


def _save_cache(chunks: list, embeddings: list):
    os.makedirs(CACHE_DIR, exist_ok=True)

    with open(CACHE_FILE, "w") as f:
        json.dump({"chunks": chunks, "embeddings": embeddings}, f)

    with open(MANIFEST_FILE, "w") as f:
        json.dump(_build_manifest(), f)

    print(f"  [Knowledge] Cache saved to {CACHE_FILE}")


def _load_cache() -> tuple:
    with open(CACHE_FILE, "r") as f:
        data = json.load(f)
    return data["chunks"], data["embeddings"]


# ── Chunking ──────────────────────────────────────────────────────────────────

def _chunk_text(text: str, source: str) -> list:
    words = text.split()
    chunks = []
    start = 0

    while start < len(words):
        end = min(start + CHUNK_SIZE, len(words))
        chunk_text = " ".join(words[start:end])
        chunks.append({"text": chunk_text, "source": source})
        if end == len(words):
            break
        start += CHUNK_SIZE - CHUNK_OVERLAP

    return chunks


# ── Embeddings ────────────────────────────────────────────────────────────────

def _embed_texts(texts: List[str]) -> List[List[float]]:
    from openai import OpenAI

    client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    batch_size = 100
    all_embeddings = []

    for i in range(0, len(texts), batch_size):
        batch = texts[i:i + batch_size]
        response = client.embeddings.create(model=EMBED_MODEL, input=batch)
        all_embeddings.extend([item.embedding for item in response.data])
        if i + batch_size < len(texts):
            time.sleep(0.5)

    return all_embeddings


def _cosine_similarity(a: List[float], b: List[float]) -> float:
    dot   = sum(x * y for x, y in zip(a, b))
    mag_a = sum(x ** 2 for x in a) ** 0.5
    mag_b = sum(x ** 2 for x in b) ** 0.5
    if mag_a == 0 or mag_b == 0:
        return 0.0
    return dot / (mag_a * mag_b)


# ── Knowledge store ───────────────────────────────────────────────────────────

class KnowledgeStore:
    def __init__(self):
        self.chunks: list = []
        self.embeddings: list = []
        self.loaded = False

    def load(self):
        if not os.path.exists(KNOWLEDGE_DIR) or not os.path.isdir(KNOWLEDGE_DIR):
            print(f"  [Knowledge] Folder '{KNOWLEDGE_DIR}' not found — skipping")
            self.loaded = True
            return

        # Try cache first
        if _cache_is_valid():
            print(f"  [Knowledge] Cache is valid — loading from {CACHE_FILE}")
            try:
                self.chunks, self.embeddings = _load_cache()
                self.loaded = True
                print(f"  [Knowledge] Loaded {len(self.chunks)} chunks from cache (no embedding API calls made)")
                return
            except Exception as e:
                print(f"  [Knowledge] Cache read failed ({e}) — rebuilding...")

        # Cache miss or invalid — read files and embed
        print(f"  [Knowledge] Building embedding cache...")
        files = sorted(os.listdir(KNOWLEDGE_DIR))
        all_chunks = []

        for filename in files:
            ext = os.path.splitext(filename)[1].lower()
            if ext not in _READERS:
                continue

            path = os.path.join(KNOWLEDGE_DIR, filename)
            try:
                text = _READERS[ext](path).strip()
                if not text:
                    continue
                chunks = _chunk_text(text, source=filename)
                all_chunks.extend(chunks)
                print(f"  [Knowledge] Read: {filename} ({len(chunks)} chunks)")
            except Exception as e:
                print(f"  [Knowledge] Failed to load {filename}: {e}")

        if not all_chunks:
            print(f"  [Knowledge] No files found in '{KNOWLEDGE_DIR}'")
            self.loaded = True
            return

        print(f"  [Knowledge] Embedding {len(all_chunks)} chunks with {EMBED_MODEL}...")
        texts = [c["text"] for c in all_chunks]
        embeddings = _embed_texts(texts)

        self.chunks = all_chunks
        self.embeddings = embeddings
        self.loaded = True

        _save_cache(all_chunks, embeddings)
        print(f"  [Knowledge] Ready — {len(self.chunks)} chunks indexed")

    def retrieve(self, query: str, top_k: int = TOP_K_CHUNKS) -> str:
        if not self.chunks:
            return "No additional knowledge loaded."

        query_embedding = _embed_texts([query])[0]

        scored = [
            (i, _cosine_similarity(query_embedding, self.embeddings[i]))
            for i in range(len(self.embeddings))
        ]
        scored.sort(key=lambda x: x[1], reverse=True)

        parts = []
        for rank, (idx, score) in enumerate(scored[:top_k], 1):
            chunk = self.chunks[idx]
            parts.append(
                f"[Knowledge {rank} | source: {chunk['source']} | relevance: {score:.2f}]\n"
                f"{chunk['text']}"
            )

        return "\n\n".join(parts)


# Singleton — loaded once when the module is imported
_store = KnowledgeStore()


def load_knowledge_store() -> KnowledgeStore:
    if not _store.loaded:
        _store.load()
    return _store