import os
import json
import time
from datetime import datetime, timezone
from dotenv import load_dotenv
from langchain.prompts import ChatPromptTemplate
from kafka_client import AuroraProducer, AuroraConsumer
from llm_router import invoke_with_rotation, get_current_provider
from analystKnowledge_loader import load_knowledge_store

load_dotenv()

# Load knowledge base on startup — cached after first load
_knowledge = load_knowledge_store()

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
Classification: {classification}

{knowledge_context}"""),
])

KAFKA_BOOTSTRAP_SERVERS = [os.getenv("KAFKA_BROKERS", "localhost:29092")]
INPUT_TOPIC     = "logs.categories"
OUTPUT_TOPIC    = "logs.solver_plan"
ANALYTICS_TOPIC = "analytics"

MIN_CONFIDENCE = int(os.getenv("MIN_CLASSIFICATION_CONFIDENCE", "70"))

# ── In-memory stats ───────────────────────────────────────────────────────────

_stats = {
    "received":               0,
    "investigated":           0,
    "skipped_confidence":     0,
    "skipped_not_security":   0,
    "simple":                 0,
    "complex":                0,
    "auto_fixable":           0,
    "requires_human":         0,
    "total_steps":            0,
    "processing_ms_total":    0,
    "started_at":             datetime.now(timezone.utc).isoformat(),
}

# ── Analytics helpers ─────────────────────────────────────────────────────────

def publish_heartbeat(producer: AuroraProducer):
    investigated = _stats["investigated"]
    avg_ms = round(_stats["processing_ms_total"] / investigated) if investigated > 0 else 0

    heartbeat = {
        "agent":            "analyst",
        "event":            "heartbeat",
        "timestamp":        datetime.now(timezone.utc).isoformat(),
        "status":           "alive",
        "uptime_since":     _stats["started_at"],
        "active_llm":       get_current_provider(),
        "confidence_threshold": MIN_CONFIDENCE,
        "quick_stats": {
            "received":          _stats["received"],
            "investigated":      investigated,
            "skipped":           _stats["skipped_confidence"] + _stats["skipped_not_security"],
            "avg_processing_ms": avg_ms,
        },
    }

    try:
        producer.send_log(ANALYTICS_TOPIC, heartbeat)
        producer.flush()
        print(f"  [Analytics] Heartbeat published (active LLM: {get_current_provider()})")
    except Exception as e:
        print(f"  [Analytics] Heartbeat failed: {e}")


def publish_investigation_analytics(producer: AuroraProducer, result: dict, classification: dict, processing_time_ms: int):
    _stats["investigated"]        += 1
    _stats["processing_ms_total"] += processing_time_ms

    complexity = result.get("complexity", "simple")
    if complexity == "simple":
        _stats["simple"] += 1
    else:
        _stats["complex"] += 1

    if result.get("autoFixable"):
        _stats["auto_fixable"] += 1
    if result.get("requiresHumanApproval"):
        _stats["requires_human"] += 1

    steps = result.get("proposedSteps", [])
    _stats["total_steps"] += len(steps)

    investigated = _stats["investigated"]

    analytics = {
        "agent":     "analyst",
        "event":     "investigation_produced",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "llm_used":  get_current_provider(),

        "incident": {
            "severity":                  classification.get("severity"),
            "category":                  classification.get("category"),
            "tags":                      classification.get("tags", []),
            "classification_confidence": classification.get("classificationConfidence"),
            "attack_vector":             result.get("attackVector"),
            "complexity":                complexity,
            "priority":                  result.get("priority"),
            "recurrence_rate":           result.get("recurrenceRate"),
            "auto_fixable":              result.get("autoFixable"),
            "requires_human":            result.get("requiresHumanApproval"),
            "notify_teams":              result.get("notifyTeams", []),
            "steps_proposed":            len(steps),
            "processing_time_ms":        processing_time_ms,
            "steps_breakdown": {
                "auto_execute":       sum(1 for s in steps if s.get("autoExecute")),
                "requires_approval":  sum(1 for s in steps if s.get("requiresApproval")),
                "risk_low":           sum(1 for s in steps if s.get("risk") == "low"),
                "risk_medium":        sum(1 for s in steps if s.get("risk") == "medium"),
                "risk_high":          sum(1 for s in steps if s.get("risk") == "high"),
            },
        },

        "cumulative": {
            "total_received":              _stats["received"],
            "total_investigated":          investigated,
            "total_skipped_confidence":    _stats["skipped_confidence"],
            "total_skipped_not_security":  _stats["skipped_not_security"],
            "total_skipped":               _stats["skipped_confidence"] + _stats["skipped_not_security"],
            "investigation_rate_pct":      round(investigated / _stats["received"] * 100, 1) if _stats["received"] > 0 else 0,
            "simple":                      _stats["simple"],
            "complex":                     _stats["complex"],
            "auto_fixable":                _stats["auto_fixable"],
            "requires_human":              _stats["requires_human"],
            "total_steps_proposed":        _stats["total_steps"],
            "avg_processing_ms":           round(_stats["processing_ms_total"] / investigated) if investigated > 0 else 0,
            "agent_started_at":            _stats["started_at"],
        },
    }

    try:
        producer.send_log(ANALYTICS_TOPIC, analytics)
        producer.flush()
        print(f"  [Analytics] Published to {ANALYTICS_TOPIC}")
    except Exception as e:
        print(f"  [Analytics] Failed: {e}")


def publish_skip_analytics(producer: AuroraProducer, reason: str, classification: dict):
    analytics = {
        "agent":     "analyst",
        "event":     "log_skipped",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "reason":    reason,
        "log_info": {
            "severity":   classification.get("severity"),
            "category":   classification.get("category"),
            "confidence": classification.get("classificationConfidence"),
        },
        "cumulative": {
            "total_received":              _stats["received"],
            "total_skipped_confidence":    _stats["skipped_confidence"],
            "total_skipped_not_security":  _stats["skipped_not_security"],
        },
    }

    try:
        producer.send_log(ANALYTICS_TOPIC, analytics)
        producer.flush()
    except Exception as e:
        print(f"  [Analytics] Failed to publish skip analytics: {e}")


# ── Core investigation logic ──────────────────────────────────────────────────

def investigate_and_publish(log_text: str, classification: dict, producer: AuroraProducer):
    print(f"\n  [ANALYST] Investigating threat via {get_current_provider()}...")
    start = time.time()

    # Retrieve relevant knowledge chunks for this specific threat
    rag_query = f"{log_text} {classification.get('category', '')} {classification.get('severity', '')}"
    knowledge_context = _knowledge.retrieve(rag_query)
    if knowledge_context:
        knowledge_context = f"Relevant knowledge from internal knowledge base:\n{knowledge_context}"
        print(f"  [ANALYST] RAG: injecting {len(knowledge_context.split())} words of context")
    else:
        knowledge_context = ""

    raw = invoke_with_rotation(prompt, {
        "log":               log_text,
        "classification":    json.dumps(classification, indent=2),
        "knowledge_context": knowledge_context,
    })
    raw = raw.replace("```json", "").replace("```", "").strip()
    result = json.loads(raw)

    processing_time_ms = round((time.time() - start) * 1000)

    print(f"   Attack Vector   : {result.get('attackVector')}")
    print(f"   Complexity      : {result.get('complexity')}")
    print(f"   Auto-fixable    : {'YES' if result.get('autoFixable') else 'NO'}")
    print(f"   Recurrence Rate : {result.get('recurrenceRate')}%")
    print(f"   Priority        : {result.get('priority')}/5")
    print(f"   Notify Teams    : {', '.join(result.get('notifyTeams', []))}")
    print(f"   Analysis        : {result.get('aiSuggestion')}")
    print(f"   Processing time : {processing_time_ms}ms  |  LLM: {get_current_provider()}")

    payload = {
        "log":            log_text,
        "classification": classification,
        "investigation":  result,
    }

    with open("pipeline.json", "w") as f:
        json.dump(payload, f, indent=2)
    print("   Written to pipeline.json")

    try:
        producer.send_log(OUTPUT_TOPIC, payload, key=log_text[:50])
        producer.flush()
        print(f"   Published to Kafka -> {OUTPUT_TOPIC}")
    except Exception as e:
        print(f"   Kafka publish failed (pipeline.json still written): {e}")

    publish_investigation_analytics(producer, result, classification, processing_time_ms)

    return result


# ── Kafka consumer mode ───────────────────────────────────────────────────────

def start_kafka_consumer():
    print(f"[KAFKA MODE] Connecting to {KAFKA_BOOTSTRAP_SERVERS}...")

    producer = AuroraProducer(bootstrap_servers=KAFKA_BOOTSTRAP_SERVERS)
    producer.ensure_topic(OUTPUT_TOPIC)
    producer.ensure_topic(ANALYTICS_TOPIC)

    consumer = AuroraConsumer(
        topics=[INPUT_TOPIC],
        group_id="analyst-group",
        bootstrap_servers=KAFKA_BOOTSTRAP_SERVERS,
        auto_offset="earliest",
    )

    consumer.consumer.poll(timeout_ms=3000)

    print(f"Connected - listening on {INPUT_TOPIC}...")
    print(f"Confidence threshold: {MIN_CONFIDENCE}% (set MIN_CLASSIFICATION_CONFIDENCE in .env to change)\n")

    publish_heartbeat(producer)

    last_heartbeat = time.time()
    HEARTBEAT_INTERVAL = int(os.getenv("ANALYST_HEARTBEAT_INTERVAL", "30"))

    try:
        for message in consumer:
            now = time.time()
            if now - last_heartbeat >= HEARTBEAT_INTERVAL:
                publish_heartbeat(producer)
                last_heartbeat = now

            try:
                data = message.value

                log_text       = data.get("log", "")
                classification = data.get("classification", {})

                if not log_text or not classification:
                    print("  Skipping - message missing log or classification field")
                    continue

                category      = classification.get("category", "unknown")
                severity      = classification.get("severity", "unknown")
                confidence    = classification.get("classificationConfidence", 0)
                is_security   = classification.get("isCybersecurity", False)
                send_to_agent = classification.get("sendToInvestigationAgent", False)

                _stats["received"] += 1

                print(f"\n[ANALYST] Received [{category.upper()}] severity={severity} confidence={confidence}%")

                if confidence < MIN_CONFIDENCE:
                    _stats["skipped_confidence"] += 1
                    print(f"  Skipping - confidence {confidence}% below threshold {MIN_CONFIDENCE}%")
                    publish_skip_analytics(producer, "low_confidence", classification)
                    continue

                if not send_to_agent or not is_security:
                    _stats["skipped_not_security"] += 1
                    print(f"  Skipping - not flagged as a security concern")
                    publish_skip_analytics(producer, "not_security", classification)
                    continue

                investigate_and_publish(log_text, classification, producer)

            except Exception as e:
                print(f"  Error processing message: {e}")
                continue

    except KeyboardInterrupt:
        print("\nStopping analyst...")
    finally:
        consumer.close()
        producer.close()


# ── Manual mode ───────────────────────────────────────────────────────────────

def manual_mode():
    print("\n╔══════════════════════════════════════════╗")
    print("║        CyberControl - Analyst            ║")
    print("╚══════════════════════════════════════════╝\n")

    with open("pipeline.json", "r") as f:
        pipeline = json.load(f)

    log_text       = pipeline.get("log")
    classification = pipeline.get("classification")

    if not log_text or not classification:
        print("  pipeline.json is empty - run classifier.py first.")
        return

    if not classification.get("sendToInvestigationAgent"):
        print("  Not flagged as a security concern - no investigation needed.")
        return

    confidence = classification.get("classificationConfidence", 0)
    if confidence < MIN_CONFIDENCE:
        print(f"  Skipping - confidence {confidence}% is below threshold {MIN_CONFIDENCE}%")
        return

    print(f"  Log      : {log_text}")
    print(f"  Category : {classification.get('category')} | {classification.get('severity')}")
    print(f"  Confidence threshold: {MIN_CONFIDENCE}%\n")

    producer = AuroraProducer(bootstrap_servers=KAFKA_BOOTSTRAP_SERVERS)
    producer.ensure_topic(OUTPUT_TOPIC)
    producer.ensure_topic(ANALYTICS_TOPIC)

    _stats["received"] += 1
    investigate_and_publish(log_text, classification, producer)

    producer.close()
    print("Written to pipeline.json - run responder.py next.\n")


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    mode = os.getenv("ANALYST_MODE", "manual").lower()

    if mode == "kafka":
        start_kafka_consumer()
    else:
        print("\nTip: set ANALYST_MODE=kafka in .env to consume from Kafka automatically.")
        manual_mode()


if __name__ == "__main__":
    main()