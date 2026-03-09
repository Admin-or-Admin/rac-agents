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

KAFKA_BOOTSTRAP_SERVERS = [os.getenv("KAFKA_BROKERS", "localhost:29092")]

# ── Core classification logic ─────────────────────────────────────────────────

def classify_and_publish(log_text: str, producer: AuroraProducer, source: str = "manual"):
    print(f"\n [CLASSIFIER] Analysing log from {source}...")
    raw = chain.invoke({"log": log_text})
    raw = raw.replace("```json", "").replace("```", "").strip()
    result = json.loads(raw)

    print(f"   Category   : {result.get('category')}")
    print(f"   Severity   : {result.get('severity')}")
    print(f"   Tags       : {', '.join(result.get('tags', []))}")
    print(f"   Security   : {'YES' if result.get('isCybersecurity') else 'NO'}")
    print(f"   Confidence : {result.get('classificationConfidence')}%")
    print(f"   Reasoning  : {result.get('reasoning')}")

    payload = {"log": log_text, "classification": result, "source": source}

    # Always write to pipeline.json for analyst.py and responder.py
    with open("pipeline.json", "w") as f:
        json.dump(payload, f, indent=2)
    print("    Written to pipeline.json")

    # Publish to Kafka logs.categories using AuroraProducer
    try:
        producer.ensure_topic("logs.categories")
        producer.send_log("logs.categories", payload, key=log_text[:50])
        producer.flush()
        print("    Published to Kafka → logs.categories\n")
    except Exception as e:
        print(f"     Kafka publish failed (pipeline.json still written): {e}\n")

    return result


# ── Kafka consumer mode ───────────────────────────────────────────────────────

def start_kafka_consumer():
    print(f" [KAFKA MODE] Connecting to {KAFKA_BOOTSTRAP_SERVERS}...")

    producer = AuroraProducer(bootstrap_servers=KAFKA_BOOTSTRAP_SERVERS)
    producer.ensure_topic("logs.unfiltered")
    producer.ensure_topic("logs.categories")

    consumer = AuroraConsumer(
        topics=["logs.unfiltered"],
        group_id="classifier-group",
        bootstrap_servers=KAFKA_BOOTSTRAP_SERVERS
    )

    consumer.consumer.poll(timeout_ms=1000)

    print(" Connected — listening on logs.unfiltered...\n")

    try:
        for message in consumer:
            print(f"mphka1 = {message}")
            try:
                data = message.value

                if isinstance(data, str):
                    log_text = data
                elif isinstance(data, dict):
                    log_text = (
                        data.get("log", {}).get("message")
                        or data.get("message")
                        or json.dumps(data)
                    )
                else:
                    log_text = str(data)

                classify_and_publish(log_text, producer, source="kafka:logs.unfiltered")

            except Exception as e:
                print(f" Error processing message: {e}")
                continue

    except KeyboardInterrupt:
        print("\nStopping classifier...")
    finally:
        consumer.close()
        producer.close()


# ── Manual input mode ─────────────────────────────────────────────────────────

def manual_mode():
    print("\n╔══════════════════════════════════════════╗")
    print("║       CyberControl — Classifier          ║")
    print("║  Paste a log message and press Enter.    ║")
    print("║  Type 'exit' to quit.                    ║")
    print("╚══════════════════════════════════════════╝\n")

    producer = AuroraProducer(bootstrap_servers=KAFKA_BOOTSTRAP_SERVERS)

    while True:
        try:
            log_text = input("LOG > ").strip()

            if not log_text:
                continue
            if log_text.lower() in ("exit", "quit", "q"):
                print("Exiting.")
                break

            classify_and_publish(log_text, producer, source="manual")

        except KeyboardInterrupt:
            print("\nExiting.")
            break
        except Exception as e:
            print(f"\nError: {e}")

    producer.close()


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    mode = os.getenv("CLASSIFIER_MODE", "manual").lower()

    if mode == "kafka":
        start_kafka_consumer()
    else:
        print("\n Tip: set CLASSIFIER_MODE=kafka in .env to consume from Kafka automatically.")
        manual_mode()


if __name__ == "__main__":
    main()