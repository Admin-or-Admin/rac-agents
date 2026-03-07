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
    ("system", """You are a senior cybersecurity analyst AI in a Security Operations Center.
You think like an attacker to understand what is really happening.
Respond with ONLY a valid JSON object, no markdown, no code fences.

Return exactly this structure:
{{
  "aiSuggestion": "2-3 sentence analysis of what is happening and why",
  "attackVector": "brief description of the attack method or threat pattern",
  "recurrenceRate": 0 to 100,
  "confidence": 0 to 100,
  "complexity": "simple or complex",
  "autoFixable": true or false,
  "requiresHumanApproval": true or false,
  "priority": 1 to 5,
  "notifyTeams": ["team names to notify"],
  "proposedSteps": [
    {{
      "id": 1,
      "title": "short action title",
      "description": "what this step does and why",
      "command": "exact command or null",
      "risk": "low or medium or high",
      "estimatedTime": "~30s",
      "rollback": "how to undo or null",
      "requiresApproval": true or false,
      "autoExecute": true or false
    }}
  ]
}}

autoExecute must only be true if risk is low AND requiresApproval is false."""),
    ("human", """Investigate this security event:
Log: {log}
Classification: {classification}"""),
])

chain = prompt | llm | StrOutputParser()


def main():
    print("\n╔══════════════════════════════════════════╗")
    print("║        CyberControl — Analyst            ║")
    print("╚══════════════════════════════════════════╝\n")

    # Read from pipeline
    with open("pipeline.json", "r") as f:
        pipeline = json.load(f)

    log_text = pipeline.get("log")
    classification = pipeline.get("classification")

    if not log_text or not classification:
        print("pipeline.json is empty — run classifier.py first.")
        return

    if not classification.get("sendToInvestigationAgent"):
        print("i Not flagged as a security concern — no investigation needed.")
        return

    print(f"Log     : {log_text}")
    print(f"Category : {classification.get('category')} | {classification.get('severity')}\n")

    print("Investigating threat...")
    raw = chain.invoke({
        "log": log_text,
        "classification": json.dumps(classification, indent=2),
    })
    raw = raw.replace("```json", "").replace("```", "").strip()
    result = json.loads(raw)

    print(f"   Attack Vector   : {result.get('attackVector')}")
    print(f"   Complexity      : {result.get('complexity')}")
    print(f"   Auto-fixable    : {'YES' if result.get('autoFixable') else 'NO'}")
    print(f"   Recurrence Rate : {result.get('recurrenceRate')}%")
    print(f"   Priority        : {result.get('priority')}/5")
    print(f"   Notify Teams    : {', '.join(result.get('notifyTeams', []))}")
    print(f"\n   Analysis: {result.get('aiSuggestion')}")

    # Update pipeline
    pipeline["investigation"] = result
    with open("pipeline.json", "w") as f:
        json.dump(pipeline, f, indent=2)

    print("\nWritten to pipeline.json — run responder.py next.\n")


if __name__ == "__main__":
    main()
