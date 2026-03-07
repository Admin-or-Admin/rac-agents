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
    temperature=0.0,
)

prompt = ChatPromptTemplate.from_messages([
    ("system", """You are an autonomous incident response AI in a Security Operations Center.
You receive a security log, its classification, and an investigation report.
Your job is to produce a concrete, executable resolution plan.

Respond with ONLY a valid JSON object, no markdown, no code fences.

Return exactly this structure:
{{
  "resolutionMode": "autonomous or guided",
  "executiveSummary": "2-3 sentences describing the incident and the resolution approach",
  "immediateActions": [
    {{
      "id": 1,
      "title": "short action title",
      "description": "exactly what this does, why it is safe, and what it prevents",
      "command": "exact ready-to-run bash/kubectl/aws-cli command, or null",
      "risk": "low or medium or high",
      "estimatedTime": "~30s",
      "rollback": "exact command to undo this, or null",
      "autoExecute": true or false,
      "requiresApproval": true or false
    }}
  ],
  "followUpActions": [
    {{
      "id": 1,
      "title": "follow-up task title",
      "description": "what needs to be done in the next 24-48 hours to prevent recurrence",
      "owner": "which team owns this",
      "deadline": "immediate or 24h or 48h or 1 week"
    }}
  ],
  "postIncidentSummary": {{
    "whatHappened": "plain english explanation of the attack or issue",
    "impactAssessment": "what was or could have been impacted",
    "rootCause": "most likely root cause",
    "lessonsLearned": "what should be changed or monitored going forward"
  }}
}}

resolutionMode must be autonomous if autoFixable is true and complexity is simple.
resolutionMode must be guided if requiresHumanApproval is true or complexity is complex.
autoExecute must only be true if risk is low AND requiresApproval is false."""),
    ("human", """Produce a resolution plan for this incident:

Log: {log}
Classification: {classification}
Investigation findings: {investigation}"""),
])

chain = prompt | llm | StrOutputParser()


def main():
    print("\n╔══════════════════════════════════════════╗")
    print("║       CyberControl — Responder           ║")
    print("╚══════════════════════════════════════════╝\n")

    # Read from pipeline
    with open("pipeline.json", "r") as f:
        pipeline = json.load(f)

    log_text = pipeline.get("log")
    classification = pipeline.get("classification")
    investigation = pipeline.get("investigation")

    if not investigation:
        print("No investigation found in pipeline.json — run analyst.py first.")
        return

    print(f"Log     : {log_text}")
    print(f"Severity : {classification.get('severity')} | complexity: {investigation.get('complexity')}\n")

    print("⚡ Generating resolution plan...")
    raw = chain.invoke({
        "log": log_text,
        "classification": json.dumps(classification, indent=2),
        "investigation": json.dumps(investigation, indent=2),
    })
    raw = raw.replace("```json", "").replace("```", "").strip()
    result = json.loads(raw)

    mode = result.get("resolutionMode", "guided").upper()
    print(f"   Mode    : {mode}")
    print(f"   Summary : {result.get('executiveSummary')}")

    print(f"\n{'─'*60}")
    print("   IMMEDIATE ACTIONS:")
    for step in result.get("immediateActions", []):
        risk_icon = {"low": "LOW", "medium": "MEDIUM", "high": "HIGH"}.get(step.get("risk"), "RISK")
        approval = "AUTO-EXECUTE" if step.get("autoExecute") else "REQUIRES HUMAN APPROVAL"
        print(f"\n   Step {step['id']}: {step['title']}")
        print(f"   {risk_icon} Risk: {step.get('risk').upper()}  |  {approval}  |  {step.get('estimatedTime')}")
        print(f"   What : {step.get('description')}")
        if step.get("command"):
            print(f"   Cmd  : {step.get('command')}")
        if step.get("rollback"):
            print(f"   Undo : {step.get('rollback')}")

    print(f"\n{'─'*60}")
    print("   FOLLOW-UP ACTIONS:")
    for action in result.get("followUpActions", []):
        print(f"\n   [{action.get('deadline').upper()}] {action['title']}")
        print(f"   Owner : {action.get('owner')}")
        print(f"   What  : {action.get('description')}")

    print(f"\n{'─'*60}")
    summary = result.get("postIncidentSummary", {})
    print("   POST-INCIDENT SUMMARY:")
    print(f"\n   What happened   : {summary.get('whatHappened')}")
    print(f"   Impact          : {summary.get('impactAssessment')}")
    print(f"   Root cause      : {summary.get('rootCause')}")
    print(f"   Lessons learned : {summary.get('lessonsLearned')}")

    # Human approval loop
    approval_steps = [s for s in result.get("immediateActions", []) if s.get("requiresApproval")]
    if approval_steps:
        print(f"\n{'─'*60}")
        print("   The following steps require your approval:")
        for step in approval_steps:
            while True:
                answer = input(f"\n   Approve step {step['id']} — '{step['title']}'? (yes/no): ").strip().lower()
                if answer in ("yes", "y"):
                    print(f"   ✓ Step {step['id']} approved.")
                    break
                elif answer in ("no", "n"):
                    reason = input(f"   Reason for denial (optional): ").strip()
                    print(f"   ✗ Step {step['id']} denied.{f' Reason: {reason}' if reason else ''}")
                    break
                else:
                    print("   Please type yes or no.")
    else:
        print("\n    All immediate actions are low-risk and can be auto-executed.")

    # Write final state to pipeline
    pipeline["resolution"] = result
    with open("pipeline.json", "w") as f:
        json.dump(pipeline, f, indent=2)

    print("\n Resolution written to pipeline.json\n")


if __name__ == "__main__":
    main()
