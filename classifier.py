import os
import json
from dotenv import load_dotenv
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain.prompts import ChatPromptTemplate
from langchain.schema.output_parser import StrOutputParser

load_dotenv()

llm = ChatGoogleGenerativeAI(
    model="gemini-2.5-flash",
    google_api_key=os.getenv("GEMINI_API_KEY"),
    temperature=0.1,
)

prompt = ChatPromptTemplate.from_messages([
    ("system", """You are a log classification agent for a cybersecurity operations center.
Classify the log and respond with ONLY a valid JSON object, no markdown, no code fences.

Return exactly this structure:
{{
  "category": "security or infrastructure or application or deployment",
  "severity": "critical or high or medium or low or info",
  "tags": ["2 to 5 lowercase tags"],
  "isCybersecurity": true or false,
  "sendToInvestigationAgent": true or false,
  "classificationConfidence": 0 to 100,
  "reasoning": "one sentence explaining the classification"
}}

sendToInvestigationAgent must be true if isCybersecurity is true AND severity is not info."""),
    ("human", "{log}"),
])

chain = prompt | llm | StrOutputParser()


def main():
    print("\n╔══════════════════════════════════════════╗")
    print("║       CyberControl — Classifier          ║")
    print("║  Paste a log message and press Enter.    ║")
    print("║  Type 'exit' to quit.                    ║")
    print("╚══════════════════════════════════════════╝\n")

    while True:
        try:
            log_text = input("LOG > ").strip()

            if not log_text:
                continue
            if log_text.lower() in ("exit", "quit", "q"):
                print("Exiting.")
                break

            print("\nAnalysing log...")
            raw = chain.invoke({"log": log_text})
            raw = raw.replace("```json", "").replace("```", "").strip()
            result = json.loads(raw)

            print(f"   Category   : {result.get('category')}")
            print(f"   Severity   : {result.get('severity')}")
            print(f"   Tags       : {', '.join(result.get('tags', []))}")
            print(f"   Security   : {'YES' if result.get('isCybersecurity') else 'NO'}")
            print(f"   Confidence : {result.get('classificationConfidence')}%")
            print(f"   Reasoning  : {result.get('reasoning')}")

            # Write to pipeline
            with open("pipeline.json", "w") as f:
                json.dump({"log": log_text, "classification": result}, f, indent=2)

            print("\nWritten to pipeline.json — run analyst.py next.\n")

        except KeyboardInterrupt:
            print("\nExiting.")
            break
        except Exception as e:
            print(f"\nError: {e}")


if __name__ == "__main__":
    main()
