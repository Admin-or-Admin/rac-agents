import os
import json
import time
from datetime import datetime, timezone
from dotenv import load_dotenv
from langchain.prompts import ChatPromptTemplate
from kafka_client import AuroraProducer, AuroraConsumer
from llm_router import invoke_with_rotation, get_current_provider
from responderKnowledge_loader import load_knowledge_store

load_dotenv()

# Load knowledge base on startup — cached after first load
_knowledge = load_knowledge_store()

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
Investigation findings: {investigation}

{knowledge_context}"""),
])

KAFKA_BOOTSTRAP_SERVERS = [os.getenv("KAFKA_BROKERS", "localhost:29092")]
INPUT_TOPIC     = "logs.solver_plan"
OUTPUT_TOPIC    = "logs.solution"
ANALYTICS_TOPIC = "analytics"

# ── Analytics helpers ─────────────────────────────────────────────────────────

# In-memory counters for the lifetime of this process
_stats = {
    "processed":       0,
    "autonomous":      0,
    "guided":          0,
    "total_steps":     0,
    "auto_steps":      0,
    "approval_steps":  0,
    "approved":        0,
    "denied":          0,
    "started_at":      datetime.now(timezone.utc).isoformat(),
}


def publish_heartbeat(producer: AuroraProducer):
    heartbeat = {
        "agent":        "responder",
        "event":        "heartbeat",
        "timestamp":    datetime.now(timezone.utc).isoformat(),
        "status":       "alive",
        "uptime_since": _stats["started_at"],
        "active_llm":   get_current_provider(),
    }
    try:
        producer.send_log(ANALYTICS_TOPIC, heartbeat)
        producer.flush()
    except Exception as e:
        print(f"  [Analytics] Heartbeat failed: {e}")


def publish_resolution_analytics(producer: AuroraProducer, result: dict, classification: dict, investigation: dict, processing_time_ms: int):
    immediate = result.get("immediateActions", [])
    follow_up = result.get("followUpActions", [])

    auto_steps     = [s for s in immediate if s.get("autoExecute")]
    approval_steps = [s for s in immediate if s.get("requiresApproval")]

    # Update in-memory counters
    _stats["processed"]      += 1
    _stats["total_steps"]    += len(immediate)
    _stats["auto_steps"]     += len(auto_steps)
    _stats["approval_steps"] += len(approval_steps)

    mode = result.get("resolutionMode", "guided")
    if mode == "autonomous":
        _stats["autonomous"] += 1
    else:
        _stats["guided"] += 1

    analytics = {
        "agent":            "responder",
        "event":            "resolution_produced",
        "timestamp":        datetime.now(timezone.utc).isoformat(),

        # This incident
        "incident": {
            "severity":             classification.get("severity"),
            "category":             classification.get("category"),
            "tags":                 classification.get("tags", []),
            "complexity":           investigation.get("complexity"),
            "priority":             investigation.get("priority"),
            "attack_vector":        investigation.get("attackVector"),
            "auto_fixable":         investigation.get("autoFixable"),
            "requires_human":       investigation.get("requiresHumanApproval"),
            "resolution_mode":      mode,
            "processing_time_ms":   processing_time_ms,
        },

        # Steps breakdown for this incident
        "steps": {
            "total":            len(immediate),
            "auto_execute":     len(auto_steps),
            "requires_approval": len(approval_steps),
            "follow_up":        len(follow_up),
            "risk_breakdown": {
                "low":    sum(1 for s in immediate if s.get("risk") == "low"),
                "medium": sum(1 for s in immediate if s.get("risk") == "medium"),
                "high":   sum(1 for s in immediate if s.get("risk") == "high"),
            },
        },

        # Cumulative stats for the lifetime of this agent process
        "cumulative": {
            "total_incidents_processed": _stats["processed"],
            "autonomous":                _stats["autonomous"],
            "guided":                    _stats["guided"],
            "autonomous_rate_pct":       round(_stats["autonomous"] / _stats["processed"] * 100, 1),
            "total_steps_generated":     _stats["total_steps"],
            "total_auto_steps":          _stats["auto_steps"],
            "total_approval_steps":      _stats["approval_steps"],
            "total_human_approved":      _stats["approved"],
            "total_human_denied":        _stats["denied"],
            "agent_started_at":          _stats["started_at"],
        },
    }

    try:
        producer.send_log(ANALYTICS_TOPIC, analytics)
        producer.flush()
        print(f"  [Analytics] Published resolution analytics to {ANALYTICS_TOPIC}")
    except Exception as e:
        print(f"  [Analytics] Failed to publish analytics: {e}")


def publish_human_decision_analytics(producer: AuroraProducer, step_id: int, decision: str, reason: str):
    if decision == "approved":
        _stats["approved"] += 1
    else:
        _stats["denied"] += 1

    analytics = {
        "agent":     "responder",
        "event":     "human_decision",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "decision": {
            "step_id":  step_id,
            "outcome":  decision,
            "reason":   reason or None,
        },
        "cumulative_decisions": {
            "approved": _stats["approved"],
            "denied":   _stats["denied"],
        },
    }

    try:
        producer.send_log(ANALYTICS_TOPIC, analytics)
        producer.flush()
    except Exception as e:
        print(f"  [Analytics] Failed to publish decision analytics: {e}")


# ── Core resolution logic ─────────────────────────────────────────────────────

def resolve_and_publish(log_text: str, classification: dict, investigation: dict, producer: AuroraProducer):
    print(f"\n  Generating resolution plan via {get_current_provider()}...")
    start = time.time()

    # Retrieve relevant knowledge for this incident
    rag_query = f"{log_text} {investigation.get(\"attackVector\", \"\")}" \
                f" {classification.get(\"category\", \"\"")}"
    knowledge_context = _knowledge.retrieve(rag_query)
    if knowledge_context:
        knowledge_context = f"Relevant knowledge from internal knowledge base:\n{knowledge_context}"
        print(f"  [RESPONDER] RAG: injecting {len(knowledge_context.split())} words of context")
    else:
        knowledge_context = ""

    raw = invoke_with_rotation(prompt, {
        "log":               log_text,
        "classification":    json.dumps(classification, indent=2),
        "investigation":     json.dumps(investigation, indent=2),
        "knowledge_context": knowledge_context,
    })
    raw = raw.replace("```json", "").replace("```", "").strip()
    result = json.loads(raw)

    processing_time_ms = round((time.time() - start) * 1000)

    mode = result.get("resolutionMode", "guided").upper()
    print(f"  Mode    : {mode}")
    print(f"  Summary : {result.get('executiveSummary')}")

    print(f"\n{'─'*60}")
    print("  IMMEDIATE ACTIONS:")
    for step in result.get("immediateActions", []):
        approval = "AUTO-EXECUTE" if step.get("autoExecute") else "REQUIRES HUMAN APPROVAL"
        print(f"\n  Step {step['id']}: {step['title']}")
        print(f"  Risk: {step.get('risk').upper()}  |  {approval}  |  {step.get('estimatedTime')}")
        print(f"  What : {step.get('description')}")
        if step.get("command"):
            print(f"  Cmd  : {step.get('command')}")
        if step.get("rollback"):
            print(f"  Undo : {step.get('rollback')}")

    print(f"\n{'─'*60}")
    print("  FOLLOW-UP ACTIONS:")
    for action in result.get("followUpActions", []):
        print(f"\n  [{action.get('deadline').upper()}] {action['title']}")
        print(f"  Owner : {action.get('owner')}")
        print(f"  What  : {action.get('description')}")

    print(f"\n{'─'*60}")
    summary = result.get("postIncidentSummary", {})
    print("  POST-INCIDENT SUMMARY:")
    print(f"\n  What happened   : {summary.get('whatHappened')}")
    print(f"  Impact          : {summary.get('impactAssessment')}")
    print(f"  Root cause      : {summary.get('rootCause')}")
    print(f"  Lessons learned : {summary.get('lessonsLearned')}")

    # Mark each step with its approval status.
    # Decisions are handled by the application, not the command line.
    # approvalStatus values:
    #   "auto"    - low risk, executes automatically, no human needed
    #   "pending" - requires human approval via the application before execution
    for step in result.get("immediateActions", []):
        step["approvalStatus"] = "pending" if step.get("requiresApproval") else "auto"

    pending = [s for s in result.get("immediateActions", []) if s.get("approvalStatus") == "pending"]
    auto    = [s for s in result.get("immediateActions", []) if s.get("approvalStatus") == "auto"]
    print(f"\n  Steps: {len(auto)} auto-execute | {len(pending)} pending human approval in application")

    # Build full solution payload
    solution_payload = {
        "log":            log_text,
        "classification": classification,
        "investigation":  investigation,
        "resolution":     result,
        "resolved_at":    datetime.now(timezone.utc).isoformat(),
    }

    # Write to pipeline.json
    with open("pipeline.json", "w") as f:
        json.dump(solution_payload, f, indent=2)
    print("\n  Written to pipeline.json")

    # Publish to logs.solution
    try:
        producer.send_log(OUTPUT_TOPIC, solution_payload, key=log_text[:50])
        producer.flush()
        print(f"  Published to Kafka -> {OUTPUT_TOPIC}")
    except Exception as e:
        print(f"  Kafka publish failed (pipeline.json still written): {e}")

    # Publish analytics for this resolution
    publish_resolution_analytics(producer, result, classification, investigation, processing_time_ms)

    return result


# ── Kafka consumer mode ───────────────────────────────────────────────────────

def start_kafka_consumer():
    print(f"[KAFKA MODE] Connecting to {KAFKA_BOOTSTRAP_SERVERS}...")

    producer = AuroraProducer(bootstrap_servers=KAFKA_BOOTSTRAP_SERVERS)
    producer.ensure_topic(OUTPUT_TOPIC)
    producer.ensure_topic(ANALYTICS_TOPIC)

    consumer = AuroraConsumer(
        topics=[INPUT_TOPIC],
        group_id="responder-group",
        bootstrap_servers=KAFKA_BOOTSTRAP_SERVERS,
        auto_offset="earliest",
    )

    consumer.consumer.poll(timeout_ms=3000)

    print(f"Connected - listening on {INPUT_TOPIC}...\n")

    # Send initial heartbeat on startup
    publish_heartbeat(producer)

    last_heartbeat = time.time()
    HEARTBEAT_INTERVAL = int(os.getenv("RESPONDER_HEARTBEAT_INTERVAL", "30"))

    try:
        for message in consumer:
            # Send a heartbeat every N seconds between messages
            now = time.time()
            if now - last_heartbeat >= HEARTBEAT_INTERVAL:
                publish_heartbeat(producer)
                last_heartbeat = now

            try:
                data = message.value

                log_text       = data.get("log", "")
                classification = data.get("classification", {})
                investigation  = data.get("investigation", {})

                if not log_text or not classification or not investigation:
                    print("  Skipping - message missing required fields")
                    continue

                severity   = classification.get("severity", "unknown")
                complexity = investigation.get("complexity", "unknown")
                priority   = investigation.get("priority", "?")

                print(f"\n[RESPONDER] Received incident | severity={severity} complexity={complexity} priority={priority}/5")

                resolve_and_publish(log_text, classification, investigation, producer)

            except Exception as e:
                print(f"Error processing message: {e}")
                continue

    except KeyboardInterrupt:
        print("\nStopping responder...")
    finally:
        consumer.close()
        producer.close()


# ── Manual mode (reads from pipeline.json) ────────────────────────────────────

def manual_mode():
    print("\n╔══════════════════════════════════════════╗")
    print("║       CyberControl - Responder           ║")
    print("╚══════════════════════════════════════════╝\n")

    with open("pipeline.json", "r") as f:
        pipeline = json.load(f)

    log_text       = pipeline.get("log")
    classification = pipeline.get("classification")
    investigation  = pipeline.get("investigation")

    if not investigation:
        print("No investigation found in pipeline.json - run analyst.py first.")
        return

    print(f"Log      : {log_text}")
    print(f"Severity : {classification.get('severity')} | complexity: {investigation.get('complexity')}\n")

    producer = AuroraProducer(bootstrap_servers=KAFKA_BOOTSTRAP_SERVERS)
    producer.ensure_topic(OUTPUT_TOPIC)
    producer.ensure_topic(ANALYTICS_TOPIC)

    resolve_and_publish(log_text, classification, investigation, producer)

    producer.close()


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    mode = os.getenv("RESPONDER_MODE", "manual").lower()

    if mode == "kafka":
        start_kafka_consumer()
    else:
        print("\nTip: set RESPONDER_MODE=kafka in .env to consume from Kafka automatically.")
        manual_mode()


if __name__ == "__main__":
    main()