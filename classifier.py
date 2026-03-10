import os
import json
import time
from datetime import datetime, timezone
from dotenv import load_dotenv
from langchain.prompts import ChatPromptTemplate
from kafka_client import AuroraProducer, AuroraConsumer
from llm_router import invoke_with_rotation, get_current_provider
from classifierKnowledge_loader import load_knowledge_store

load_dotenv()

# Load and embed knowledge files once on startup
knowledge_store = load_knowledge_store()

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

sendToInvestigationAgent must be true if isCybersecurity is true AND severity is not info.

{knowledge}"""),
    ("human", "{log}"),
])

KAFKA_BOOTSTRAP_SERVERS = [os.getenv("KAFKA_BROKERS", "localhost:29092")]
ANALYTICS_TOPIC = "analytics"

# ── In-memory stats ───────────────────────────────────────────────────────────

_stats = {
    "received":           0,
    "classified":         0,
    "security":           0,
    "non_security":       0,
    "sent_to_analyst":    0,
    "by_category": {
        "security":       0,
        "infrastructure": 0,
        "application":    0,
        "deployment":     0,
    },
    "by_severity": {
        "critical":       0,
        "high":           0,
        "medium":         0,
        "low":            0,
        "info":           0,
    },
    "processing_ms_total": 0,
    "started_at":          datetime.now(timezone.utc).isoformat(),
}

# ── Analytics helpers ─────────────────────────────────────────────────────────

def publish_heartbeat(producer: AuroraProducer):
    classified = _stats["classified"]
    avg_ms = round(_stats["processing_ms_total"] / classified) if classified > 0 else 0

    heartbeat = {
        "agent":        "classifier",
        "event":        "heartbeat",
        "timestamp":    datetime.now(timezone.utc).isoformat(),
        "status":       "alive",
        "uptime_since": _stats["started_at"],
        "active_llm":   get_current_provider(),
        "quick_stats": {
            "received":          _stats["received"],
            "classified":        classified,
            "sent_to_analyst":   _stats["sent_to_analyst"],
            "avg_processing_ms": avg_ms,
        },
    }

    try:
        producer.send_log(ANALYTICS_TOPIC, heartbeat)
        producer.flush()
        print(f"  [Analytics] Heartbeat published (active LLM: {get_current_provider()})")
    except Exception as e:
        print(f"  [Analytics] Heartbeat failed: {e}")


def publish_classification_analytics(producer: AuroraProducer, result: dict, processing_time_ms: int):
    category   = result.get("category", "unknown")
    severity   = result.get("severity", "unknown")
    is_sec     = result.get("isCybersecurity", False)
    send_to    = result.get("sendToInvestigationAgent", False)
    confidence = result.get("classificationConfidence", 0)

    _stats["classified"]          += 1
    _stats["processing_ms_total"] += processing_time_ms

    if is_sec:
        _stats["security"] += 1
    else:
        _stats["non_security"] += 1

    if send_to:
        _stats["sent_to_analyst"] += 1

    if category in _stats["by_category"]:
        _stats["by_category"][category] += 1

    if severity in _stats["by_severity"]:
        _stats["by_severity"][severity] += 1

    classified = _stats["classified"]

    analytics = {
        "agent":     "classifier",
        "event":     "classification_produced",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "llm_used":  get_current_provider(),

        "incident": {
            "category":            category,
            "severity":            severity,
            "tags":                result.get("tags", []),
            "confidence":          confidence,
            "is_cybersecurity":    is_sec,
            "sent_to_analyst":     send_to,
            "processing_time_ms":  processing_time_ms,
        },

        "cumulative": {
            "total_received":        _stats["received"],
            "total_classified":      classified,
            "total_security":        _stats["security"],
            "total_non_security":    _stats["non_security"],
            "total_sent_to_analyst": _stats["sent_to_analyst"],
            "security_rate_pct":     round(_stats["security"] / classified * 100, 1) if classified > 0 else 0,
            "analyst_rate_pct":      round(_stats["sent_to_analyst"] / classified * 100, 1) if classified > 0 else 0,
            "by_category":           _stats["by_category"].copy(),
            "by_severity":           _stats["by_severity"].copy(),
            "avg_processing_ms":     round(_stats["processing_ms_total"] / classified) if classified > 0 else 0,
            "agent_started_at":      _stats["started_at"],
        },
    }

    try:
        producer.send_log(ANALYTICS_TOPIC, analytics)
        producer.flush()
        print(f"  [Analytics] Published to {ANALYTICS_TOPIC}")
    except Exception as e:
        print(f"  [Analytics] Failed: {e}")


# ── Core classification logic ─────────────────────────────────────────────────

def classify_and_publish(log_text: str, producer: AuroraProducer, source: str = "manual"):
    print(f"\n  [CLASSIFIER] Analysing log from {source} via {get_current_provider()}...")
    start = time.time()

    # Retrieve only the most relevant knowledge chunks for this specific log
    knowledge_context = knowledge_store.retrieve(log_text)

    raw = invoke_with_rotation(prompt, {
        "log":       log_text,
        "knowledge": knowledge_context,
    })
    raw = raw.replace("```json", "").replace("```", "").strip()
    result = json.loads(raw)

    processing_time_ms = round((time.time() - start) * 1000)

    print(f"   Category   : {result.get('category')}")
    print(f"   Severity   : {result.get('severity')}")
    print(f"   Tags       : {', '.join(result.get('tags', []))}")
    print(f"   Security   : {'YES' if result.get('isCybersecurity') else 'NO'}")
    print(f"   Confidence : {result.get('classificationConfidence')}%")
    print(f"   Reasoning  : {result.get('reasoning')}")
    print(f"   Processing : {processing_time_ms}ms  |  LLM: {get_current_provider()}")

    payload = {"log": log_text, "classification": result, "source": source}

    with open("pipeline.json", "w") as f:
        json.dump(payload, f, indent=2)
    print("   Written to pipeline.json")

    try:
        producer.ensure_topic("logs.categories")
        producer.send_log("logs.categories", payload, key=log_text[:50])
        producer.flush()
        print("   Published to Kafka -> logs.categories")
    except Exception as e:
        print(f"   Kafka publish failed (pipeline.json still written): {e}")

    publish_classification_analytics(producer, result, processing_time_ms)

    return result


# ── Kafka consumer mode ───────────────────────────────────────────────────────

def start_kafka_consumer():
    print(f"[KAFKA MODE] Connecting to {KAFKA_BOOTSTRAP_SERVERS}...")

    producer = AuroraProducer(bootstrap_servers=KAFKA_BOOTSTRAP_SERVERS)
    # producer.ensure_topic("logs.unfiltered")
    # producer.ensure_topic("logs.categories")
    # producer.ensure_topic(ANALYTICS_TOPIC)

    consumer = AuroraConsumer(
        topics=["logs.unfiltered"],
        group_id="classifier-group",
        bootstrap_servers=KAFKA_BOOTSTRAP_SERVERS,
    )

    consumer.consumer.poll(timeout_ms=1000)

    print("Connected - listening on logs.unfiltered...\n")

    publish_heartbeat(producer)

    last_heartbeat = time.time()
    HEARTBEAT_INTERVAL = int(os.getenv("CLASSIFIER_HEARTBEAT_INTERVAL", "30"))

    try:
        for message in consumer:
            now = time.time()
            if now - last_heartbeat >= HEARTBEAT_INTERVAL:
                publish_heartbeat(producer)
                last_heartbeat = now

            try:
                data = message.value
                _stats["received"] += 1

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
                print(f"  Error processing message: {e}")
                continue

    except KeyboardInterrupt:
        print("\nStopping classifier...")
    finally:
        consumer.close()
        producer.close()


# ── Manual input mode ─────────────────────────────────────────────────────────

def manual_mode():
    print("\n╔══════════════════════════════════════════╗")
    print("║       CyberControl - Classifier          ║")
    print("║  Paste a log message and press Enter.    ║")
    print("║  Type 'exit' to quit.                    ║")
    print("╚══════════════════════════════════════════╝\n")

    producer = AuroraProducer(bootstrap_servers=KAFKA_BOOTSTRAP_SERVERS)
    producer.ensure_topic(ANALYTICS_TOPIC)

    while True:
        try:
            log_text = input("LOG > ").strip()

            if not log_text:
                continue
            if log_text.lower() in ("exit", "quit", "q"):
                print("Exiting.")
                break

            _stats["received"] += 1
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
        print("\nTip: set CLASSIFIER_MODE=kafka in .env to consume from Kafka automatically.")
        manual_mode()


if __name__ == "__main__":
    main()