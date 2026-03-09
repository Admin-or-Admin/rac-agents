# rac-agents

**Recognise. Analyse. Classify.**

Three autonomous AI agents that form a cybersecurity pipeline. Raw logs come in one end, categorised threats with remediation plans come out the other. Each agent is a standalone Python process that communicates through Apache Kafka, which means you can run them on separate machines, scale them independently, or swap one out without touching the others.

This document covers what each agent does, how to set everything up from scratch, and how the pieces fit together.

---

## Table of Contents

- [How it works](#how-it-works)
- [Agents](#agents)
  - [Classifier](#classifier)
  - [Analyst](#analyst)
  - [Responder](#responder)
- [Kafka topics](#kafka-topics)
- [Analytics](#analytics)
- [Knowledge base](#knowledge-base)
- [Project structure](#project-structure)
- [Setup](#setup)
- [Configuration](#configuration)
- [Running the agents](#running-the-agents)
- [Pipeline JSON fallback](#pipeline-json-fallback)
- [Adding a new knowledge file](#adding-a-new-knowledge-file)

---

## How it works

Every log that enters the system goes through three stages in sequence.

The classifier reads raw logs from the `logs.unfiltered` Kafka topic, sends each one to Gemini for classification, and publishes the result to `logs.categories`. The analyst reads from `logs.categories`, filters out anything below the confidence threshold or not flagged as a security concern, and investigates the rest using Gemini with cross-domain context. It publishes investigation results to `logs.solver_plan`. The responder reads from `logs.solver_plan`, generates a concrete remediation plan with per-step risk levels and approval requirements, and publishes the final output to `logs.solution`.

All three agents also publish operational events to the `analytics` topic so you have full visibility into what each agent is doing, how long things take, and whether they are alive.

```
logs.unfiltered
      |
  classifier
      |
logs.categories
      |
   analyst
      |
logs.solver_plan
      |
  responder
      |
 logs.solution
      |
   analytics  <-- all three agents publish here in parallel
```

---

## Agents

### Classifier

**File:** `classifier.py`
**Reads from:** `logs.unfiltered`
**Writes to:** `logs.categories`, `analytics`

The classifier is the entry point of the pipeline. It takes a raw log message in whatever format it arrives and asks Gemini to make sense of it. The output is a structured JSON object that every downstream agent depends on.

For each log it produces:

- `category` — one of `security`, `infrastructure`, `application`, or `deployment`
- `severity` — one of `critical`, `high`, `medium`, `low`, or `info`
- `tags` — two to five lowercase descriptive tags
- `isCybersecurity` — whether this log has any security relevance
- `sendToInvestigationAgent` — whether the analyst should pick it up. This is true only when `isCybersecurity` is true and severity is not `info`
- `classificationConfidence` — integer from 0 to 100 representing how confident the model is in its classification
- `reasoning` — one sentence explaining why it was classified this way

The classifier runs in two modes controlled by the `CLASSIFIER_MODE` environment variable. In `manual` mode it reads from stdin so you can paste individual log lines for testing. In `kafka` mode it sits and listens on `logs.unfiltered` continuously.

If there are files in the `knowledge/` folder, their contents are injected into the system prompt before every classification call. This is how you teach the classifier about your company's specific log formats, GDPR requirements, or any custom categorisation rules. See the [Knowledge base](#knowledge-base) section for details.

---

### Analyst

**File:** `analyst.py`
**Reads from:** `logs.categories`
**Writes to:** `logs.solver_plan`, `analytics`

The analyst receives classified logs and decides which ones are worth investigating. It applies two filters before spending any tokens on analysis.

The first filter is confidence. Any log where `classificationConfidence` is below `MIN_CLASSIFICATION_CONFIDENCE` is dropped with a skip event published to `analytics`. The threshold is configurable in `.env`. Setting it to `0` disables the filter entirely and passes everything through. Raising it means fewer logs get investigated but with higher-quality input. Lowering it means more coverage but with noisier data.

The second filter is security relevance. Logs where `sendToInvestigationAgent` is false or `isCybersecurity` is false are skipped. These are infrastructure noise, deployment events, and application errors that the classifier already determined are not security concerns.

For logs that pass both filters, the analyst sends the log text and its classification to Gemini and asks for a threat investigation. The output includes:

- `aiSuggestion` — two to three sentences describing what is happening and why
- `attackVector` — the specific technique being used
- `complexity` — `simple` or `complex`, which determines how the responder handles it
- `autoFixable` — whether a script can safely remediate this without human risk
- `requiresHumanApproval` — whether a person needs to sign off before anything is executed
- `priority` — integer from 1 to 5, where 1 is most urgent
- `recurrenceRate` — probability from 0 to 100 that this happens again within 24 hours
- `notifyTeams` — which teams should be informed
- `proposedSteps` — a list of ordered remediation steps, each with a title, description, optional command, risk level, estimated time, and rollback instruction

---

### Responder

**File:** `responder.py`
**Reads from:** `logs.solver_plan`
**Writes to:** `logs.solution`, `analytics`

The responder takes the analyst's investigation and turns it into an executable resolution plan. It calls Gemini one more time with the full context — log, classification, and investigation findings — and produces a structured plan that the application layer can act on.

The output includes:

- `resolutionMode` — `autonomous` if the issue can be fixed without human intervention, `guided` if a person needs to be involved
- `executiveSummary` — two to three sentences describing the incident and the resolution approach
- `immediateActions` — a list of steps to take right now. Each step has an `approvalStatus` field that is either `auto` or `pending`. Steps marked `auto` are safe to execute without asking anyone. Steps marked `pending` need a human to approve them in the application before they run
- `followUpActions` — tasks for the next 24 to 48 hours to prevent the issue from recurring, each with an owner team and deadline
- `postIncidentSummary` — plain English explanation of what happened, what was or could have been impacted, the most likely root cause, and what should change going forward

The responder does not ask for human input on the command line. All approval decisions happen in the application layer by reading the `approvalStatus` field on each step. When a human approves or denies a step in the application, that decision gets published to the `actions` Kafka topic, which the responder consumes to execute or skip accordingly.

---

## Kafka topics

| Topic | Producer | Consumer | Content |
|---|---|---|---|
| `logs.unfiltered` | Your ingestor service | Classifier | Raw log documents as received from the source system |
| `logs.categories` | Classifier | Analyst | Logs enriched with category, severity, tags, confidence, and security flags |
| `logs.solver_plan` | Analyst | Responder | Security logs with full threat investigation and proposed remediation steps |
| `logs.solution` | Responder | Ledger, Gateway | Complete resolution plans with per-step approval status |
| `analytics` | All three agents | Ledger, Gateway | Heartbeats, per-event statistics, and cumulative counters |

All topics are created automatically by the agents using `ensure_topic()` if they do not already exist. You do not need to create them manually.

---

## Analytics

Every agent publishes to the `analytics` topic. Each message has an `agent` field so consumers can filter by source, and an `event` field so consumers know what kind of message it is.

**Classifier** publishes:

- `heartbeat` — alive signal with uptime, received count, classified count, forwarded to analyst count, and average processing time in milliseconds
- `classification_produced` — fires after every classification with the result details and cumulative breakdowns by category and severity

**Analyst** publishes:

- `heartbeat` — alive signal with uptime, received count, investigated count, skipped count, and average processing time
- `investigation_produced` — fires after every successful investigation with attack vector, complexity, priority, recurrence rate, steps breakdown by risk level, and cumulative stats
- `log_skipped` — fires when a log is dropped, with the reason (`low_confidence` or `not_security`) and the log's severity and category

**Responder** publishes:

- `heartbeat` — alive signal with uptime and quick stats
- `resolution_produced` — fires after every resolution plan is generated with the resolution mode, steps breakdown, and cumulative autonomous vs guided rates
- `human_decision` — fires when a human approves or denies a step in the application, with the step ID, outcome, optional reason, and running totals

The heartbeat interval for each agent is configurable independently. The counters in every event are cumulative for the lifetime of the process, meaning they reset when the agent restarts. For permanent storage of these metrics, point the ledger service at the `analytics` topic.

---

## Knowledge base

The `knowledge/` folder lets you inject domain-specific context into the classifier's prompt without touching any code. Any file you place in this folder is loaded once when the classifier starts and appended to the system prompt for every classification call.

Supported file types:

- `.pdf` — extracted using PyMuPDF
- `.docx` — extracted using python-docx
- `.txt` — read as plain text
- `.md` — read as plain text

Examples of what to put here:

- A PDF explaining your company's internal log format and what each field means
- GDPR or HIPAA compliance requirements that affect how logs should be categorised
- OWASP Top 10 reference material
- A guide describing which log patterns your team considers critical vs high severity
- A list of internal service names and what they do so the classifier can make better decisions

To add new knowledge, drop the file in the `knowledge/` folder and restart the classifier. Nothing else is required.

If you have very large documents (100 or more pages), be aware that the full text is injected into every single API call. This increases cost and latency. For the current setup this is acceptable. If it becomes a problem, the next step is proper RAG with a vector database like Chroma where only relevant chunks are retrieved per log instead of the entire knowledge base.

---

## Project structure

```
rac-agents/
├── knowledge/              Drop PDFs, DOCX, or TXT files here for classifier context
├── classifier.py           Agent 1 — classifies raw logs
├── analyst.py              Agent 2 — investigates security events
├── responder.py            Agent 3 — generates remediation plans
├── kafka_client.py         Shared Kafka producer and consumer wrappers
├── knowledge_loader.py     Reads and extracts text from files in knowledge/
├── pipeline.json           Shared state file used in manual mode
├── .env                    Environment variables (not committed)
└── .gitignore
```

---

## Setup

**Requirements:**

- Python 3.10 or higher. Python 3.9 works but Google's libraries will print deprecation warnings.
- A running Kafka broker accessible from your machine
- A Gemini API key from Google AI Studio

**Clone and create the virtual environment:**

```bash
cd rac-agents
python -m venv venv
source venv/bin/activate      # Mac / Linux
venv\Scripts\activate         # Windows
```

**Install dependencies:**

```bash
pip install langchain langchain-google-genai kafka-python python-dotenv pymupdf python-docx
```

**Create the `.env` file:**

```bash
cp .env.example .env
```

Then fill in your values. See the [Configuration](#configuration) section for all available variables.

**Create the knowledge folder:**

```bash
mkdir knowledge
```

**Create the pipeline file:**

```bash
echo "{}" > pipeline.json
```

---

## Configuration

All configuration lives in `.env`. None of these values are committed to the repository.

| Variable | Default | Description |
|---|---|---|
| `GEMINI_API_KEY` | required | Your Google Gemini API key from Google AI Studio |
| `KAFKA_BROKERS` | `localhost:29092` | Address of your Kafka broker. If Kafka runs on another machine use that machine's LAN IP, not localhost. Only define this once — if you have two entries the last one wins and the first is silently ignored |
| `KAFKA_GROUP_ID` | `classifier-group` | The Kafka consumer group ID for the classifier. Changing this makes Kafka treat the classifier as a brand new consumer, which causes it to reprocess all messages from the beginning of the topic |
| `BATCH_SIZE` | `20` | How many messages the classifier pulls from Kafka in a single poll before processing. This does not mean they are sent to Gemini in a batch — each message is still classified individually. Higher values reduce poll overhead but increase memory usage per cycle |
| `CLASSIFIER_MODE` | `manual` | Set to `kafka` to consume from Kafka automatically. Leave as `manual` to read from stdin for testing |
| `ANALYST_MODE` | `manual` | Set to `kafka` to consume from Kafka automatically |
| `RESPONDER_MODE` | `manual` | Set to `kafka` to consume from Kafka automatically |
| `MIN_CLASSIFICATION_CONFIDENCE` | `70` | Percentage from 0 to 100. Logs where the classifier confidence is below this value are skipped by the analyst. Set to `0` to disable the filter completely and pass all logs through regardless of confidence |
| `CLASSIFIER_HEARTBEAT_INTERVAL` | `30` | How often in seconds the classifier publishes a heartbeat to the analytics topic |
| `ANALYST_HEARTBEAT_INTERVAL` | `30` | How often in seconds the analyst publishes a heartbeat |
| `RESPONDER_HEARTBEAT_INTERVAL` | `30` | How often in seconds the responder publishes a heartbeat |
| `KNOWLEDGE_DIR` | `knowledge` | Path to the folder containing knowledge files for the classifier. Can be absolute or relative to where you run the script |

A complete `.env` for reference:

```properties
# Gemini
GEMINI_API_KEY=your_key_here

# Kafka — use the LAN IP of the machine running Docker, not localhost
KAFKA_BROKERS=192.168.1.6:29092
KAFKA_GROUP_ID=classifier-group
BATCH_SIZE=20

# Agent modes — manual for testing, kafka for production
CLASSIFIER_MODE=kafka
ANALYST_MODE=kafka
RESPONDER_MODE=kafka

# Confidence filter — set to 0 to disable and pass all logs through
MIN_CLASSIFICATION_CONFIDENCE=0

# Heartbeat intervals in seconds
CLASSIFIER_HEARTBEAT_INTERVAL=30
ANALYST_HEARTBEAT_INTERVAL=30
RESPONDER_HEARTBEAT_INTERVAL=30
```

---

## Running the agents

Open three separate terminal windows. Activate the virtual environment in each one.

```bash
source venv/bin/activate
```

Start the agents in this order. Each one depends on the previous publishing messages before it has anything to consume.

**Terminal 1 — Classifier:**

```bash
python classifier.py
```

**Terminal 2 — Analyst:**

```bash
python analyst.py
```

**Terminal 3 — Responder:**

```bash
python responder.py
```

Once all three are running, any message published to `logs.unfiltered` will flow through the full pipeline automatically.

To test manually without Kafka, set all three `_MODE` variables to `manual` in `.env`. Then run them one at a time in any single terminal. The classifier will prompt you to paste a log message, write the result to `pipeline.json`, and exit. Run the analyst next to read from `pipeline.json`, investigate, and write the result back. Run the responder last to read the investigation and produce the final plan.

---

## Pipeline JSON fallback

`pipeline.json` is a flat JSON file that acts as shared state between agents when running in manual mode. Each agent reads from it and writes back to it as it completes its stage. In Kafka mode this file is still written on every message as a local record of the last processed item, but it is not the primary communication channel.

Do not commit `pipeline.json` to the repository. It is already listed in `.gitignore`.

---

## Adding a new knowledge file

1. Obtain the file in PDF, DOCX, TXT, or MD format.
2. Copy it into the `knowledge/` folder.
3. Restart the classifier.

The file will be loaded on startup and you will see a confirmation line in the output:

```
[Knowledge] Loaded: gdpr-requirements.pdf (42381 chars)
```

If the file fails to load you will see an error message with the reason. The classifier will continue to run without that file rather than crashing.

To remove a knowledge file, delete it from the folder and restart the classifier.
