import os
import fitz  # pymupdf
import docx

KNOWLEDGE_DIR = os.getenv("KNOWLEDGE_DIR", "knowledge")


def load_pdf(path: str) -> str:
    doc = fitz.open(path)
    return "\n".join(page.get_text() for page in doc)


def load_docx(path: str) -> str:
    doc = docx.Document(path)
    return "\n".join(p.text for p in doc.paragraphs if p.text.strip())


def load_txt(path: str) -> str:
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        return f.read()


LOADERS = {
    ".pdf":  load_pdf,
    ".docx": load_docx,
    ".txt":  load_txt,
    ".md":   load_txt,
}


def load_knowledge() -> str:
    if not os.path.isdir(KNOWLEDGE_DIR):
        return ""

    files = sorted(os.listdir(KNOWLEDGE_DIR))
    chunks = []

    for filename in files:
        ext = os.path.splitext(filename)[1].lower()
        if ext not in LOADERS:
            continue

        path = os.path.join(KNOWLEDGE_DIR, filename)
        try:
            text = LOADERS[ext](path).strip()
            if text:
                chunks.append(
                    f"--- KNOWLEDGE FILE: {filename} ---\n{text}\n--- END: {filename} ---"
                )
                print(f"  [Knowledge] Loaded: {filename} ({len(text)} chars)")
        except Exception as e:
            print(f"  [Knowledge] Failed to load {filename}: {e}")

    if not chunks:
        print(f"  [Knowledge] No files found in '{KNOWLEDGE_DIR}' folder")
        return ""

    return "\n\n".join(chunks)
