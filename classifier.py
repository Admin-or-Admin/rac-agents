import os
import json
import time
from typing import Dict, Any
from langchain_core.prompts import ChatPromptTemplate
from shared.base_agent import BaseAgent
from llm_router import invoke_with_rotation, get_current_provider

class ClassifierAgent(BaseAgent):
    def __init__(self):
        super().__init__(
            name="classifier",
            input_topic="logs.unfiltered",
            output_topic="logs.categories",
            knowledge_dir=os.getenv("KNOWLEDGE_DIR", "rac-agents/classifierKnowledge")
        )
        
        self.prompt = ChatPromptTemplate.from_messages([
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

    def handle_message(self, data: Dict[str, Any]):
        """Processes a raw log and publishes its classification."""
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

        self.logger.info(f"Classifying log via {get_current_provider()}...")
        start_time = time.time()

        # RAG: Retrieve knowledge
        knowledge_context = self.kb.retrieve(log_text) if self.kb else ""

        # LLM: Invoke
        raw = invoke_with_rotation(self.prompt, {
            "log": log_text,
            "knowledge": knowledge_context,
        })
        raw = raw.replace("```json", "").replace("```", "").strip()
        result = json.loads(raw)

        processing_time_ms = int((time.time() - start_time) * 1000)
        
        # Prepare payload
        payload = {
            "log": log_text, 
            "classification": result, 
            "source": f"kafka:{self.input_topic}"
        }

        # Save for manual review/debug
        with open("pipeline.json", "w") as f:
            json.dump(payload, f, indent=2)

        # Publish
        self.producer.send_log(self.output_topic, payload, key=log_text[:50])
        self.producer.flush()

        # Analytics
        self.publish_analytics("classification_produced", {
            "incident": {
                "category": result.get("category"),
                "severity": result.get("severity"),
                "is_cybersecurity": result.get("isCybersecurity"),
                "confidence": result.get("classificationConfidence"),
                "processing_time_ms": processing_time_ms
            }
        })

        self.logger.info(f"Classification complete: {result.get('category')} ({result.get('severity')})")

    def run_manual(self):
        """Interactive manual mode."""
        print(f"\n--- {self.display_name} Manual Mode ---")
        self.producer = self._init_producer_only()
        
        while True:
            try:
                log_text = input("LOG > ").strip()
                if not log_text or log_text.lower() in ("exit", "quit", "q"):
                    break
                
                self.handle_message(log_text)
            except KeyboardInterrupt:
                break
            except Exception as e:
                self.logger.error(f"Manual processing error: {e}")

    def _init_producer_only(self):
        from shared.kafka_client import AuroraProducer
        return AuroraProducer(bootstrap_servers=[self.bootstrap_servers])

if __name__ == "__main__":
    agent = ClassifierAgent()
    agent.start()
