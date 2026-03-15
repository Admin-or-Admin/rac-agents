import os
import json
import time
from typing import Dict, Any
from langchain_core.prompts import ChatPromptTemplate
from shared.base_agent import BaseAgent
from llm_router import invoke_with_rotation, get_current_provider

class AnalystAgent(BaseAgent):
    def __init__(self):
        super().__init__(
            name="analyst",
            input_topic="logs.categories",
            output_topic="logs.solver_plan",
            knowledge_dir=os.getenv("ANALYST_KNOWLEDGE_DIR", "rac-agents/analystKnowledge")
        )
        
        self.min_confidence = int(os.getenv("MIN_CLASSIFICATION_CONFIDENCE", "70"))
        
        self.prompt = ChatPromptTemplate.from_messages([
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

    def handle_message(self, data: Dict[str, Any]):
        """Analyzes a classified log and publishes an investigation plan."""
        log_text       = data.get("log", "")
        classification = data.get("classification", {})

        if not log_text or not classification:
            self.logger.warning("Message missing log or classification field - skipping")
            return

        category      = classification.get("category", "unknown")
        severity      = classification.get("severity", "unknown")
        confidence    = classification.get("classificationConfidence", 0)
        is_security   = classification.get("isCybersecurity", False)
        send_to_agent = classification.get("sendToInvestigationAgent", False)

        if confidence < self.min_confidence:
            self.logger.info(f"Skipping - confidence {confidence}% below threshold {self.min_confidence}%")
            self.publish_analytics("log_skipped", {"reason": "low_confidence", "confidence": confidence})
            return

        if not send_to_agent or not is_security:
            self.logger.info(f"Skipping - not flagged as a security concern")
            self.publish_analytics("log_skipped", {"reason": "not_security"})
            return

        self.logger.info(f"Investigating threat via {get_current_provider()}...")
        start_time = time.time()

        # RAG: Retrieve knowledge
        rag_query = f"{log_text} {category} {severity}"
        knowledge_context = self.kb.retrieve(rag_query) if self.kb else ""

        # LLM: Invoke
        raw = invoke_with_rotation(self.prompt, {
            "log": log_text,
            "classification": json.dumps(classification, indent=2),
            "knowledge_context": knowledge_context,
        })
        raw = raw.replace("```json", "").replace("```", "").strip()
        result = json.loads(raw)

        processing_time_ms = int((time.time() - start_time) * 1000)
        
        # Prepare payload
        payload = {
            "log": log_text, 
            "classification": classification, 
            "investigation": result
        }

        # Save for manual review/debug
        with open("pipeline.json", "w") as f:
            json.dump(payload, f, indent=2)

        # Publish
        self.producer.send_log(self.output_topic, payload, key=log_text[:50])
        self.producer.flush()

        # Analytics
        self.publish_analytics("investigation_produced", {
            "incident": {
                "category": category,
                "severity": severity,
                "priority": result.get("priority"),
                "auto_fixable": result.get("autoFixable"),
                "processing_time_ms": processing_time_ms
            }
        })

        self.logger.info(f"Investigation complete: {result.get('attackVector')} (Priority: {result.get('priority')})")

    def run_manual(self):
        """Manual mode from pipeline.json."""
        self.logger.info("Manual mode: reading from pipeline.json")
        try:
            with open("pipeline.json", "r") as f:
                data = json.load(f)
            
            self.producer = self._init_producer_only()
            self.handle_message(data)
            self.logger.info("Investigation plan published from manual input.")
        except FileNotFoundError:
            self.logger.error("pipeline.json not found - run classifier first.")
        except Exception as e:
            self.logger.error(f"Manual processing error: {e}")

    def _init_producer_only(self):
        from shared.kafka_client import AuroraProducer
        return AuroraProducer(bootstrap_servers=[self.bootstrap_servers])

if __name__ == "__main__":
    agent = AnalystAgent()
    agent.start()
