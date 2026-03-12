# rac-agents

Three autonomous AI agents that form the cybersecurity processing pipeline for the CyberControl platform. Raw logs come in one end, categorised threats with full remediation plans come out the other.

Each agent is a standalone Python process that communicates through Apache Kafka. They can run on separate machines, scale independently, or be restarted without affecting the others. All three also support a manual mode for local testing without Kafka.

---

## Table of contents

- [How the pipeline works](#how-the-pipeline-works)
- [Agents](#agents)
  - [Classifier](#classifier)
  - [Analyst](#analyst)
  - [Responder](#responder)
- [LLM router](#llm-router)
- [Kafka topics](#kafka-topics)
- [Kafka client](#kafka-client)
- [Analytics](#analytics)
- [Knowledge bases (RAG)](#knowledge-bases-rag)
- [Project structure](#project-structure)
- [Setup](#setup)
- [Environment variables](#environment-variables)
- [Running the agents](#running-the-agents)
- [Manual mode](#manual-mode)
- [pipeline.json](#pipelinejson)

---

## How the pipeline works

Every log that enters the system flows through three stages in sequence. Each stage reads from one Kafka topic and writes to the next.

```
logs.unfiltered
      │
  [Classifier]  ──── classifierKnowledge/ (RAG)
      │
logs.categories
      │
   [Analyst]    ──── analystKnowledge/ (RAG)
      │
logs.solver_plan
      │
  [Responder]   ──── responderKnowledge/ (RAG)
      │
 logs.solution
      │
  [Ledger] ──► PostgreSQL ──► Gateway ──► Dashboard

  analytics  ◄── all three agents publish here in parallel
```

The classifier reads raw log strings, classifies them, and publishes the result. The analyst reads classified logs, filters out low-confidence or non-security events, and investigates the rest. The responder reads investigation results and produces executable remediation plans. All three publish heartbeats and event statistics to the `analytics` topic in parallel.

Each agent also loads a knowledge base from a local folder on startup. Relevant chunks are retrieved and injected into the prompt for every LLM call — this is the RAG layer that lets you teach the agents about your specific environment without modifying any code.

---

## Agents

### Classifier

**File:** `classifier.py`  
**Reads from:** `logs.unfiltered`  
**Writes to:** `logs.categories`, `analytics`  
**Knowledge folder:** `classifierKnowledge/`

The entry point of the pipeline. Takes a raw log string in any format and produces a structured classification.

**What it produces per log:**

| Field | Type | Description |
|---|---|---|
| `category` | string | `security`, `infrastructure`, `application`, or `deployment` |
| `severity` | string | `critical`, `high`, `medium`, `low`, or `info` |
| `tags` | string[] | 2–5 lowercase descriptive tags |
| `isCybersecurity` | bool | Whether this log has any security relevance |
| `sendToInvestigationAgent` | bool | True only if `isCybersecurity` is true AND severity is not `info` |
| `classificationConfidence` | int | 0–100, model confidence in this classification |
| `reasoning` | string | One sentence explaining the classification decision |

The classifier runs in `manual` mode (reads from stdin, good for testing individual logs) or `kafka` mode (listens on `logs.unfiltered` continuously). Controlled by `CLASSIFIER_MODE` in `.env`.

**RAG query:** the raw log text itself is used as the retrieval query. The most relevant chunks from `classifierKnowledge/` are injected into the system prompt before each classification call.

---

### Analyst

**File:** `analyst.py`  
**Reads from:** `logs.categories`  
**Writes to:** `logs.solver_plan`, `analytics`  
**Knowledge folder:** `analystKnowledge/`

The analyst receives classified logs and investigates the ones that matter. It applies two filters before spending any tokens:

**Filter 1 — confidence threshold.** If `classificationConfidence` is below `MIN_CLASSIFICATION_CONFIDENCE`, the log is dropped and a `log_skipped` event with reason `low_confidence` is published to `analytics`. Set `MIN_CLASSIFICATION_CONFIDENCE=0` in `.env` to disable this filter entirely.

**Filter 2 — security relevance.** If `sendToInvestigationAgent` is false or `isCybersecurity` is false, the log is dropped with reason `not_security`. These are infrastructure noise, deployment events, and application errors.

For logs that pass both filters, the analyst sends the log and its classification to the LLM and produces a threat investigation.

**What it produces per investigation:**

| Field | Type | Description |
|---|---|---|
| `aiSuggestion` | string | 2–3 sentences: what is happening and why |
| `attackVector` | string | The specific technique or threat pattern |
| `complexity` | string | `simple` or `complex` — drives the responder's resolution mode |
| `autoFixable` | bool | Whether a script can safely fix this |
| `requiresHumanApproval` | bool | Whether a human must sign off |
| `priority` | int | 1–5, where 5 is most urgent |
| `recurrenceRate` | int | 0–100 probability of recurrence within 24 hours |
| `confidence` | int | 0–100 model confidence in this investigation |
| `notifyTeams` | string[] | Teams to alert |
| `proposedSteps` | object[] | Ordered remediation steps (see below) |

**Each proposed step:**

| Field | Type | Description |
|---|---|---|
| `id` | int | Step number |
| `title` | string | Short action title |
| `description` | string | What this step does and why |
| `command` | string\|null | Exact shell/kubectl/aws-cli command, or null |
| `risk` | string | `low`, `medium`, or `high` |
| `estimatedTime` | string | e.g. `~30s`, `~5min` |
| `rollback` | string\|null | How to undo this step |
| `requiresApproval` | bool | Whether human approval is needed |
| `autoExecute` | bool | True only if risk is `low` AND `requiresApproval` is false |

**RAG query:** `log text + category + severity` — the three signals most useful for retrieving relevant threat intelligence or runbook sections.

---

### Responder

**File:** `responder.py`  
**Reads from:** `logs.solver_plan`  
**Writes to:** `logs.solution`, `analytics`  
**Knowledge folder:** `responderKnowledge/`

Takes the analyst's investigation and produces a final, executable resolution plan. This is what gets written to PostgreSQL by the Ledger and displayed in the dashboard.

**What it produces per incident:**

| Field | Type | Description |
|---|---|---|
| `resolutionMode` | string | `autonomous` if `autoFixable=true` and `complexity=simple`, otherwise `guided` |
| `executiveSummary` | string | 2–3 sentences: the incident and resolution approach |
| `immediateActions` | object[] | Steps to take right now (see below) |
| `followUpActions` | object[] | Tasks for the next 24–48 hours |
| `postIncidentSummary` | object | Plain English debrief |

**Each immediate action:**

| Field | Description |
|---|---|
| `id` | Step number |
| `title` | Short action title |
| `description` | What this does, why it is safe, what it prevents |
| `command` | Exact ready-to-run command, or null |
| `risk` | `low`, `medium`, or `high` |
| `estimatedTime` | Time estimate |
| `rollback` | Exact command to undo this, or null |
| `autoExecute` | True only if risk is `low` AND `requiresApproval` is false |
| `requiresApproval` | Whether a human must approve in the application |
| `approvalStatus` | Set by the responder: `auto` or `pending` |

**Each follow-up action:**

| Field | Description |
|---|---|
| `title` | Task title |
| `description` | What needs to be done to prevent recurrence |
| `owner` | Which team owns this |
| `deadline` | `immediate`, `24h`, `48h`, or `1 week` |

**Post-incident summary:**

| Field | Description |
|---|---|
| `whatHappened` | Plain English explanation of the attack or issue |
| `impactAssessment` | What was or could have been impacted |
| `rootCause` | Most likely root cause |
| `lessonsLearned` | What should be changed or monitored going forward |

**RAG query:** `log text + attackVector + category` — at this stage the responder has the investigation results, so it uses the attack vector as the primary retrieval signal to find the most relevant playbooks or remediation guides.

**Approval flow:** the responder does not handle approvals on the command line. Each step is tagged `approvalStatus: pending` or `approvalStatus: auto` before the payload is published. The application (dashboard) reads these tags and presents pending steps to the operator. When the operator approves or denies, the decision is written to PostgreSQL via the gateway's `PATCH /remediation/{step_id}` endpoint.

---

## LLM router

**File:** `llm_router.py`

All three agents call the LLM through `invoke_with_rotation()` instead of calling the model directly. This function handles key rotation automatically.

**Rotation order:** Gemini key 1 → Gemini key 2 → GPT-4.1

On a 429 rate limit error, the router rotates to the next key silently. If all keys are exhausted in a single rotation, it waits `RATE_LIMIT_WAIT_SECONDS` (default 60) and then resets to the first key. Non-rate-limit errors are re-raised immediately without rotation.

To check which provider is currently active, call `get_current_provider()` — all three agents print this on every LLM call.

To add or remove keys, edit the `_build_clients()` function in `llm_router.py`. Commented-out entries for Gemini are already there — just uncomment them and set the corresponding env vars.

---

## Kafka topics

| Topic | Producer | Consumer | Content |
|---|---|---|---|
| `logs.unfiltered` | Ingestor | Classifier | Raw log strings as received from the source system |
| `logs.categories` | Classifier | Analyst | Log + classification JSON |
| `logs.solver_plan` | Analyst | Responder | Log + classification + investigation JSON |
| `logs.solution` | Responder | Ledger | Full resolution plan JSON |
| `analytics` | All three agents | Ledger | Heartbeats, per-event stats, and cumulative counters |

All topics are auto-created by the agents via `ensure_topic()` if they do not already exist. You do not need to create them manually in Kafka.

---

## Kafka client

**File:** `kafka_client.py`

A thin wrapper around `kafka-python-ng` with three classes:

**`AuroraProducer`** — wraps `KafkaProducer`. Key methods:
- `send_log(topic, payload, key=None)` — serialises payload to JSON and sends it. Uses `trace.id` or `id` as the partition key by default
- `flush()` — blocks until all buffered messages are delivered
- `ensure_topic(name)` — creates the topic if it does not exist

**`AuroraConsumer`** — wraps `KafkaConsumer`. Implements `__iter__` so you can use it directly in a `for message in consumer` loop. Messages are automatically deserialised from JSON.

**`AuroraKafkaClient`** — base class for both. The `_wait_for_connection` method retries the connection every 2 seconds indefinitely until Kafka is reachable. This means agents will keep retrying on startup if Kafka is not ready yet — no manual intervention needed.

---

## Analytics

Every agent publishes structured events to the `analytics` Kafka topic. All events have `agent`, `event`, and `timestamp` fields. Counters are cumulative for the lifetime of the process and reset on restart.

### Classifier events

**`heartbeat`** — published every `CLASSIFIER_HEARTBEAT_INTERVAL` seconds:
```json
{
  "agent": "classifier",
  "event": "heartbeat",
  "status": "alive",
  "active_llm": "GPT-4.1",
  "quick_stats": {
    "received": 142,
    "classified": 141,
    "sent_to_analyst": 38,
    "avg_processing_ms": 1240
  }
}
```

**`classification_produced`** — published after every successful classification. Includes the individual result (category, severity, tags, confidence, is_cybersecurity, sent_to_analyst) and cumulative breakdowns by category and severity.

### Analyst events

**`heartbeat`** — published every `ANALYST_HEARTBEAT_INTERVAL` seconds with received, investigated, skipped counts and average processing time.

**`investigation_produced`** — published after every investigation. Includes attack vector, complexity, priority, recurrence rate, steps breakdown by risk level, and all cumulative stats.

**`log_skipped`** — published when a log is dropped, with the reason (`low_confidence` or `not_security`), the log's severity and category, and running skip totals.

### Responder events

**`heartbeat`** — published every `RESPONDER_HEARTBEAT_INTERVAL` seconds.

**`resolution_produced`** — published after every resolution plan. Includes resolution mode, steps breakdown (total, auto-execute, requires-approval, follow-up, risk breakdown), and cumulative autonomous vs guided rates.

**`human_decision`** — published when a human approves or denies a remediation step in the application:
```json
{
  "agent": "responder",
  "event": "human_decision",
  "decision": {
    "step_id": 3,
    "outcome": "approved",
    "reason": null
  },
  "cumulative_decisions": {
    "approved": 12,
    "denied": 3
  }
}
```

---

## Knowledge bases (RAG)

Each agent has its own knowledge folder. Drop files into the appropriate folder and the agent will load, chunk, embed, and cache them automatically on startup. Relevant chunks are retrieved per event and injected into the LLM prompt.

| Agent | Folder | Best content to put here |
|---|---|---|
| Classifier | `classifierKnowledge/` | Internal log format docs, field definitions, service catalogue, custom categorisation rules, GDPR/HIPAA tagging requirements |
| Analyst | `analystKnowledge/` | Threat intelligence reports, CVE databases, MITRE ATT&CK descriptions, known attack patterns in your environment, past incident notes |
| Responder | `responderKnowledge/` | Remediation playbooks, runbooks, approved command templates, rollback procedures, escalation policies, team ownership docs |

**Supported file formats:** `.pdf`, `.docx`, `.txt`, `.md`

**How it works:**

1. On startup the agent reads all files in the knowledge folder, splits them into overlapping text chunks, and calls the OpenAI embeddings API (`text-embedding-3-small`) to embed each chunk.
2. The result is written to a `.cache/` subfolder inside the knowledge folder as `embeddings.json` and `manifest.json`.
3. On subsequent startups, the manifest is compared against the current files. If nothing has changed, the cache is loaded directly — no embedding API calls are made.
4. If any file has been added, removed, or modified, the entire cache is rebuilt.
5. At inference time, the agent builds a retrieval query from the current log/event, embeds it, and computes cosine similarity against all cached chunk embeddings. The top-K most relevant chunks are assembled into a context string and injected into the prompt.

**Configuration per agent:**

```properties
# Classifier
KNOWLEDGE_DIR=classifierKnowledge
KNOWLEDGE_CHUNK_SIZE=500
KNOWLEDGE_CHUNK_OVERLAP=50
KNOWLEDGE_TOP_K=5

# Analyst
ANALYST_KNOWLEDGE_DIR=analystKnowledge
ANALYST_KNOWLEDGE_CHUNK_SIZE=500
ANALYST_KNOWLEDGE_CHUNK_OVERLAP=50
ANALYST_KNOWLEDGE_TOP_K=5

# Responder
RESPONDER_KNOWLEDGE_DIR=responderKnowledge
RESPONDER_KNOWLEDGE_CHUNK_SIZE=500
RESPONDER_KNOWLEDGE_CHUNK_OVERLAP=50
RESPONDER_KNOWLEDGE_TOP_K=5
```

**To add new knowledge:** copy the file into the relevant folder and restart the agent. You will see confirmation lines like:
```
[AnalystKnowledge] Read: mitre-attack.pdf (87 chunks)
[AnalystKnowledge] Embedding 87 chunks with text-embedding-3-small...
[AnalystKnowledge] Ready — 87 chunks indexed
```

**If the folder does not exist:** the agent logs a warning and continues without RAG — it does not crash.

---

## Project structure

```
rac-agents/
  classifier.py                  — Agent 1: classifies raw logs
  analyst.py                     — Agent 2: investigates security events
  responder.py                   — Agent 3: generates remediation plans
  llm_router.py                  — LLM key rotation (Gemini → GPT-4.1)
  kafka_client.py                — Shared Kafka producer/consumer wrappers
  classifierKnowledge_loader.py  — RAG loader for the classifier
  analystKnowledge_loader.py     — RAG loader for the analyst
  responderKnowledge_loader.py   — RAG loader for the responder
  pipeline.json                  — Shared state file for manual mode
  requirements.txt               — All Python dependencies
  .env                           — Environment variables (never committed)
  .gitignore
  classifierKnowledge/           — Drop files here for classifier RAG
  analystKnowledge/              — Drop files here for analyst RAG
  responderKnowledge/            — Drop files here for responder RAG
```

---

## Setup

**Prerequisites:**
- Python 3.10+
- Apache Kafka running (via Docker Compose in the [.github repo](https://github.com/Admin-or-Admin/.github))
- At least one API key — OpenAI and/or Gemini

**1. Clone and create a virtual environment**
```bash
git clone https://github.com/Admin-or-Admin/rac-agents.git
cd rac-agents
python -m venv venv
source venv/bin/activate        # Mac/Linux
venv\Scripts\activate           # Windows
```

**2. Install dependencies**
```bash
pip install -r requirements.txt
```

**3. Create your `.env` file**
```bash
cp .env .env.backup   # keep the example for reference
```

Edit `.env` with your actual keys and Kafka address. See the [Environment variables](#environment-variables) section for all options.

**4. Create knowledge folders**
```bash
mkdir -p classifierKnowledge analystKnowledge responderKnowledge
```

These can be empty to start — the agents will skip RAG if no files are present.

**5. Verify Kafka is reachable**
```bash
curl http://localhost:8080   # Redpanda console should respond
```

---

## Environment variables

All configuration lives in `.env`.

### LLM keys

| Variable | Description |
|---|---|
| `OPENAI_API_KEY` | OpenAI key — used for GPT-4.1 (LLM) and `text-embedding-3-small` (RAG embeddings) |
| `GEMINI_API_KEY_1` | First Gemini key — used if uncommented in `llm_router.py` |
| `GEMINI_API_KEY_2` | Second Gemini key — used as fallback after key 1 |

### Kafka

| Variable | Default | Description |
|---|---|---|
| `KAFKA_BROKERS` | `localhost:29092` | Kafka broker address. If Kafka runs in Docker on the same machine, use `localhost:29092`. If it runs on another machine, use that machine's LAN IP |
| `KAFKA_GROUP_ID` | `classifier-group` | Consumer group for the classifier. Changing this causes Kafka to treat the classifier as a new consumer and reprocess all messages from the beginning |
| `BATCH_SIZE` | `20` | Messages pulled per Kafka poll. Each is still classified individually |

### Agent modes

| Variable | Default | Options |
|---|---|---|
| `CLASSIFIER_MODE` | `manual` | `manual` (stdin) or `kafka` (continuous) |
| `ANALYST_MODE` | `manual` | `manual` (reads `pipeline.json`) or `kafka` |
| `RESPONDER_MODE` | `manual` | `manual` (reads `pipeline.json`) or `kafka` |

### Filtering

| Variable | Default | Description |
|---|---|---|
| `MIN_CLASSIFICATION_CONFIDENCE` | `70` | Analyst skips logs below this confidence %. Set to `0` to disable |

### Heartbeats

| Variable | Default | Description |
|---|---|---|
| `CLASSIFIER_HEARTBEAT_INTERVAL` | `30` | Seconds between classifier heartbeats |
| `ANALYST_HEARTBEAT_INTERVAL` | `30` | Seconds between analyst heartbeats |
| `RESPONDER_HEARTBEAT_INTERVAL` | `30` | Seconds between responder heartbeats |
| `RATE_LIMIT_WAIT_SECONDS` | `60` | Seconds to wait when all LLM keys are rate limited |

### Knowledge base (RAG)

| Variable | Default | Description |
|---|---|---|
| `KNOWLEDGE_DIR` | `classifierKnowledge` | Knowledge folder for the classifier |
| `KNOWLEDGE_CHUNK_SIZE` | `500` | Words per chunk |
| `KNOWLEDGE_CHUNK_OVERLAP` | `50` | Overlap in words between adjacent chunks |
| `KNOWLEDGE_TOP_K` | `5` | Number of chunks to retrieve per classification |
| `ANALYST_KNOWLEDGE_DIR` | `analystKnowledge` | Knowledge folder for the analyst |
| `ANALYST_KNOWLEDGE_CHUNK_SIZE` | `500` | |
| `ANALYST_KNOWLEDGE_CHUNK_OVERLAP` | `50` | |
| `ANALYST_KNOWLEDGE_TOP_K` | `5` | |
| `RESPONDER_KNOWLEDGE_DIR` | `responderKnowledge` | Knowledge folder for the responder |
| `RESPONDER_KNOWLEDGE_CHUNK_SIZE` | `500` | |
| `RESPONDER_KNOWLEDGE_CHUNK_OVERLAP` | `50` | |
| `RESPONDER_KNOWLEDGE_TOP_K` | `5` | |

---

## Running the agents

Open three terminal windows. Activate the venv in each one (`source venv/bin/activate`). Start them in order — each depends on the previous one publishing messages before it has anything to consume.

**Terminal 1 — Classifier**
```bash
python classifier.py
```

**Terminal 2 — Analyst**
```bash
python analyst.py
```

**Terminal 3 — Responder**
```bash
python responder.py
```

Once all three are running, any message published to `logs.unfiltered` will flow through the full pipeline automatically. You will see output in each terminal as messages arrive and are processed.

**Expected startup output (classifier as example):**
```
[KAFKA MODE] Connecting to ['localhost:29092']...
  [AnalystKnowledge] Cache valid — loading from analystKnowledge/.cache/embeddings.json
  [AnalystKnowledge] Loaded 142 chunks from cache
Connected - listening on logs.unfiltered...
  [Analytics] Heartbeat published (active LLM: GPT-4.1)
```

---

## Manual mode

Set all three `_MODE` variables to `manual` in `.env`, then run each agent one at a time in a single terminal.

**Step 1 — Classifier (interactive)**
```bash
python classifier.py
```
```
LOG > 2024-01-15 14:23:01 Failed login attempt for user admin from 192.168.1.105
```
The classifier analyses the log, prints the classification, writes it to `pipeline.json`, and waits for the next input. Type `exit` to quit.

**Step 2 — Analyst (reads pipeline.json)**
```bash
python analyst.py
```
Reads the classification from `pipeline.json`, investigates if it passes the filters, and writes the investigation back to `pipeline.json`.

**Step 3 — Responder (reads pipeline.json)**
```bash
python responder.py
```
Reads the investigation from `pipeline.json` and prints the full resolution plan.

This is the fastest way to test a specific log end-to-end without needing Kafka running.

---

## pipeline.json

`pipeline.json` is the shared state file used in manual mode. In Kafka mode it is still written on every message as a local record of the last processed event, but it is not the primary communication channel.

It accumulates data through the pipeline stages — after the classifier it contains `log` and `classification`. After the analyst it also has `investigation`. After the responder it has all four fields plus `resolution` and `resolved_at`.

It is listed in `.gitignore` and should never be committed. The file in the repository is a sample showing what a complete pipeline run looks like.

---

## Related repos

| Repo | Description |
|---|---|
| [gateway](https://github.com/Admin-or-Admin/gateway) | FastAPI API that reads the processed data from PostgreSQL |
| [ledger](https://github.com/Admin-or-Admin/ledger) | Kafka consumer that writes agent output to PostgreSQL |
| [ingestor](https://github.com/Admin-or-Admin/ingestor) | Pushes logs to `logs.unfiltered` (mock, Elasticsearch, GNS3) |
| [dashboard](https://github.com/Admin-or-Admin/dashboard) | React frontend |
| [.github](https://github.com/Admin-or-Admin/.github) | Docker Compose for local development |
