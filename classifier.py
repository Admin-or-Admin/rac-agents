import os
from dotenv import load_dotenv
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain.prompts import ChatPromptTemplate
from langchain.schema.output_parser import StrOutputParser

load_dotenv()

# ── Model setup ───────────────────────────────────────────────────────────────

llm = ChatGoogleGenerativeAI(
    model="gemini-2.5-flash",
    google_api_key=os.getenv("GEMINI_API_KEY"),
    temperature=0.2,
)

# ── Prompt ────────────────────────────────────────────────────────────────────

prompt = ChatPromptTemplate.from_messages([
    (
        "system",
        """You are a log classification agent for a cybersecurity operations center.
When given a log message, classify it and explain your reasoning clearly.

For every log you receive, tell the user:
1. Category   — one of: security / infrastructure / application / deployment
2. Severity   — one of: critical / high / medium / low / info
3. Tags       — 2 to 5 short lowercase tags
4. Is it a cybersecurity concern? — yes or no, and why
5. Recommended action — what should be done about it

Be concise but thorough. Speak like a senior security analyst."""
    ),
    (
        "human",
        "{input}"
    ),
])

# ── Chain ─────────────────────────────────────────────────────────────────────

chain = prompt | llm | StrOutputParser()

# ── Main loop ─────────────────────────────────────────────────────────────────

def main():
    print("\n╔══════════════════════════════════════════╗")
    print("║        CyberControl — Classifier         ║")
    print("║  Paste a log message and press Enter.    ║")
    print("║  Type 'exit' to quit.                    ║")
    print("╚══════════════════════════════════════════╝\n")

    while True:
        try:
            user_input = input("LOG > ").strip()

            if not user_input:
                continue

            if user_input.lower() in ("exit", "quit", "q"):
                print("Exiting.")
                break

            print("\nAnalysing...\n")
            response = chain.invoke({"input": user_input})
            print(f"ANALYSIS\n{'─' * 60}\n{response}\n{'─' * 60}\n")

        except KeyboardInterrupt:
            print("\nExiting.")
            break

if __name__ == "__main__":
    main()
