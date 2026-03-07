import os
import json
from dotenv import load_dotenv
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain.prompts import ChatPromptTemplate
from langchain.schema.output_parser import StrOutputParser
from kafka_client import AuroraProducer, AuroraConsumer

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

KAFKA_BOOTSTRAP_SERVERS = [os.getenv("KAFKA_BROKERS", "localhost:29092")]
INPUT_TOPIC  = "logs.categories"
OUTPUT_TOPIC = "logs.solver_plan"

# Configurable — skip any log the classifier was less than this % confident about
MIN_CONFIDENCE = int(os.getenv("MIN_CLASSIFICATION_CONFIDENCE", "70"))


# ── Core investigation logic ──────────────────────────────────────────────────

def investigate_and_publish(log_text: str, classification: dict, producer: AuroraProducer):
    print(f"\n [ANALYST] Investigating threat...")

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
    print(f"   Analysis        : {result.get('aiSuggestion')}")

    payload = {
        "log": log_text,
        "classification": classification,
        "investigation": result,
    }

    # Write to pipeline.json for responder.py
    with open("pipeline.json", "w") as f:
        json.dump(payload, f, indent=2)
    print("    Written to pipeline.json")

    # Publish to Kafka logs.solver_plan
    try:
        producer.send_log(OUTPUT_TOPIC, payload, key=log_text[:50])
        producer.flush()
        print(f"    Published to Kafka → {OUTPUT_TOPIC}\n")
    except Exception as e:
        print(f"     Kafka publish failed (pipeline.json still written): {e}\n")

    return result


# ── Kafka consumer mode ───────────────────────────────────────────────────────

def start_kafka_consumer():
    print(f"📡 [KAFKA MODE] Connecting to {KAFKA_BOOTSTRAP_SERVERS}...")

    producer = AuroraProducer(bootstrap_servers=KAFKA_BOOTSTRAP_SERVERS)
    producer.ensure_topic(OUTPUT_TOPIC)

    consumer = AuroraConsumer(
        topics=[INPUT_TOPIC],
        group_id="analyst-group",
        bootstrap_servers=KAFKA_BOOTSTRAP_SERVERS,
        auto_offset="earliest",
    )

    # Force partition assignment so the group appears in Redpanda Console
    consumer.consumer.poll(timeout_ms=3000)

    print(f" Connected — listening on {INPUT_TOPIC}...")
    print(f"   Confidence threshold : {MIN_CONFIDENCE}% (set MIN_CLASSIFICATION_CONFIDENCE in .env to change)\n")

    try:
        for message in consumer:
            try:
                data = message.value

                # Extract log text and classification from the classifier payload
                log_text      = data.get("log", "")
                classification = data.get("classification", {})

                if not log_text or not classification:
                    print("⚠️  Skipping — message missing log or classification field")
                    continue

                log_id     = log_text[:40]
                category   = classification.get("category", "unknown")
                severity   = classification.get("severity", "unknown")
                confidence = classification.get("classificationConfidence", 0)
                is_security = classification.get("isCybersecurity", False)
                send_to_agent = classification.get("sendToInvestigationAgent", False)

                print(f"\n Received [{category.upper()}] severity={severity} confidence={confidence}%")

                # ── Filter 1: low confidence ──────────────────────────────────
                if confidence < MIN_CONFIDENCE:
                    print(f"   ⏭  Skipping — confidence {confidence}% is below threshold {MIN_CONFIDENCE}%")
                    continue

                # ── Filter 2: not a security concern ─────────────────────────
                if not send_to_agent or not is_security:
                    print(f"   ⏭  Skipping — not flagged as a security concern")
                    continue

                investigate_and_publish(log_text, classification, producer)

            except Exception as e:
                print(f" Error processing message: {e}")
                continue

    except KeyboardInterrupt:
        print("\nStopping analyst...")
    finally:
        consumer.close()
        producer.close()


# ── Manual mode (reads from pipeline.json) ────────────────────────────────────

def manual_mode():
    print("\n╔══════════════════════════════════════════╗")
    print("║        CyberControl — Analyst            ║")
    print("╚══════════════════════════════════════════╝\n")

    with open("pipeline.json", "r") as f:
        pipeline = json.load(f)

    log_text       = pipeline.get("log")
    classification = pipeline.get("classification")

    if not log_text or not classification:
        print(" pipeline.json is empty — run classifier.py first.")
        return

    if not classification.get("sendToInvestigationAgent"):
        print("  Not flagged as a security concern — no investigation needed.")
        return

    confidence = classification.get("classificationConfidence", 0)
    if confidence < MIN_CONFIDENCE:
        print(f"  Skipping — confidence {confidence}% is below threshold {MIN_CONFIDENCE}%")
        return

    print(f" Log      : {log_text}")
    print(f"  Category : {classification.get('category')} | {classification.get('severity')}")
    print(f"   Confidence threshold: {MIN_CONFIDENCE}%\n")

    producer = AuroraProducer(bootstrap_servers=KAFKA_BOOTSTRAP_SERVERS)
    producer.ensure_topic(OUTPUT_TOPIC)

    investigate_and_publish(log_text, classification, producer)

    producer.close()
    print("Written to pipeline.json — run responder.py next.\n")


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    mode = os.getenv("ANALYST_MODE", "manual").lower()

    if mode == "kafka":
        start_kafka_consumer()
    else:
        print("\n Tip: set ANALYST_MODE=kafka in .env to consume from Kafka automatically.")
        manual_mode()


if __name__ == "__main__":
    main()