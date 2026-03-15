import os
import json
import time
from datetime import datetime, timezone
from typing import Dict, Any
from langchain_core.prompts import ChatPromptTemplate
from shared.base_agent import BaseAgent
from llm_router import invoke_with_rotation, get_current_provider

class ResponderAgent(BaseAgent):
    def __init__(self):
        super().__init__(
            name="responder",
            input_topic="logs.solver_plan",
            output_topic="logs.solution",
            knowledge_dir=os.getenv("RESPONDER_KNOWLEDGE_DIR", "rac-agents/responderKnowledge")
        )
        
        self.prompt = ChatPromptTemplate.from_messages([
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

    def handle_message(self, data: Dict[str, Any]):
        """Generates a final resolution plan based on classification and investigation."""
        log_text       = data.get("log", "")
        classification = data.get("classification", {})
        investigation  = data.get("investigation", {})

        if not log_text or not classification or not investigation:
            self.logger.warning("Message missing required fields - skipping")
            return

        severity   = classification.get("severity", "unknown")
        complexity = investigation.get("complexity", "unknown")

        self.logger.info(f"Generating resolution plan via {get_current_provider()}...")
        start_time = time.time()

        # RAG: Retrieve knowledge
        rag_query = f"{log_text} {investigation.get('attackVector', '')} {classification.get('category', '')}"
        knowledge_context = self.kb.retrieve(rag_query) if self.kb else ""

        # LLM: Invoke
        raw = invoke_with_rotation(self.prompt, {
            "log": log_text,
            "classification": json.dumps(classification, indent=2),
            "investigation": json.dumps(investigation, indent=2),
            "knowledge_context": knowledge_context,
        })
        raw = raw.replace("```json", "").replace("```", "").strip()
        result = json.loads(raw)

        processing_time_ms = int((time.time() - start_time) * 1000)

        # Mark each step with its approval status
        for step in result.get("immediateActions", []):
            step["approvalStatus"] = "pending" if step.get("requiresApproval") else "auto"

        # Prepare payload
        payload = {
            "log": log_text, 
            "classification": classification, 
            "investigation": investigation,
            "resolution": result,
            "resolved_at": datetime.now(timezone.utc).isoformat()
        }

        # Save for manual review/debug
        with open("pipeline.json", "w") as f:
            json.dump(payload, f, indent=2)

        # Publish
        self.producer.send_log(self.output_topic, payload, key=log_text[:50])
        self.producer.flush()

        # Analytics
        self.publish_analytics("resolution_produced", {
            "incident": {
                "severity": severity,
                "complexity": complexity,
                "resolution_mode": result.get("resolutionMode"),
                "processing_time_ms": processing_time_ms
            },
            "steps": {
                "total": len(result.get("immediateActions", [])),
                "auto_execute": sum(1 for s in result.get("immediateActions", []) if not s.get("requiresApproval"))
            }
        })

        self.logger.info(f"Resolution complete: {result.get('resolutionMode')} mode.")

    def run_manual(self):
        """Manual mode from pipeline.json."""
        self.logger.info("Manual mode: reading from pipeline.json")
        try:
            with open("pipeline.json", "r") as f:
                data = json.load(f)
            
            self.producer = self._init_producer_only()
            self.handle_message(data)
            self.logger.info("Resolution plan published from manual input.")
        except FileNotFoundError:
            self.logger.error("pipeline.json not found - run analyst first.")
        except Exception as e:
            self.logger.error(f"Manual processing error: {e}")

    def _init_producer_only(self):
        from shared.kafka_client import AuroraProducer
        return AuroraProducer(bootstrap_servers=[self.bootstrap_servers])

if __name__ == "__main__":
    agent = ResponderAgent()
    agent.start()
