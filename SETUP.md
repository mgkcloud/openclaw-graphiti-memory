# Graphiti Memory Plugin — Fresh Instance Setup Guide

This guide walks through installing the Graphiti memory plugin on a **brand new VPS or machine** from scratch. Estimated time: 15-20 minutes.

---

## What You're Installing

A three-layer memory system for [OpenClaw](https://openclaw.ai):

| Layer | Technology | Purpose |
|-------|------------|---------|
| Private memory | QMD (vector search) | Agent's private notes and session history |
| Shared files | Symlinked `_shared/` dir | Cross-agent reference docs (roster, profiles) |
| Temporal knowledge | Graphiti + Neo4j | Time-stamped facts and cross-agent discoveries |

---

## Prerequisites

- **OpenClaw** installed and running ([docs](https://docs.openclaw.ai))
- **Docker** + **Docker Compose** v2
- **Ubuntu 22.04+** or **macOS 13+**
- An **OpenAI API key** (for Graphiti entity extraction; searches are free)
- **1GB RAM** minimum (Neo4j needs ~512MB)

---

## Step 1 — Clone the Repo

SSH into your fresh instance and clone:

```bash
git clone https://github.com/mgkcloud/openclaw-graphiti-memory.git ~/graphiti-memory
cd ~/graphiti-memory
```

---

## Step 2 — Deploy Graphiti + Neo4j

```bash
# Create services directory
mkdir -p ~/services/graphiti

# Copy the Docker Compose file
cp ~/graphiti-memory/docker-compose.yml ~/services/graphiti/

# Set your OpenAI API key (required for embeddings)
export OPENAI_API_KEY="sk-your-key-here"

# Start the stack
cd ~/services/graphiti
docker compose up -d
```

Verify it's running:

```bash
docker ps | grep graphiti
curl http://localhost:8001/health  # should return 200
```

**Services:**
- Graphiti API: `http://localhost:8001`
- Neo4j Browser: `http://localhost:7474` (default password: `graphiti`)

---

## Step 3 — Install Scripts

```bash
# Copy all scripts to your OpenClaw scripts directory
cp ~/graphiti-memory/scripts/* ~/clawd/scripts/
chmod +x ~/clawd/scripts/*.sh ~/clawd/scripts/*.py

# Copy shared files (reference docs that all agents read)
mkdir -p ~/clawd/agents/_shared
cp ~/graphiti-memory/shared-files/* ~/clawd/agents/_shared/
```

**Scripts installed:**

| Script | What it does |
|--------|-------------|
| `graphiti-search.sh "query"` | Search the knowledge graph |
| `graphiti-log.sh <agent_id> <role> <name> "fact"` | Log a fact to your agent's group |
| `graphiti-context.sh "task description"` | Get full context for a task (hybrid QMD + Graphiti) |
| `memory-hybrid-search.sh "query"` | Search QMD private files + Graphiti together |
| `memory-status.sh` | Check health of all memory layers |
| `graphiti-sync-sessions.py` | Sync OpenClaw session transcripts to Graphiti |
| `graphiti-watch-files.py` | Watch files and auto-sync changes to Graphiti |
| `graphiti-import-files.py` | Bulk import existing docs into Graphiti |

---

## Step 4 — Configure OpenClaw

Add this to `~/.openclaw/openclaw.json` under `agents.defaults`:

```json
{
  "memorySearch": {
    "enabled": true,
    "sources": ["memory", "sessions"],
    "provider": "gemini",
    "model": "gemini-embedding-001",
    "sync": {
      "onSessionStart": true,
      "watch": true
    }
  }
}
```

Or if you prefer the memory backend directly in the config:

```json
{
  "memory": {
    "backend": "qmd",
    "qmd": {
      "command": "qmd",
      "includeDefaultMemory": true,
      "sessions": {
        "enabled": true,
        "retentionDays": 30
      }
    }
  }
}
```

Restart OpenClaw to pick up the config:

```bash
openclaw gateway restart
```

---

## Step 5 — Set Up Shared Memory Directory (Multi-Agent Only)

If you're running multiple OpenClaw agents that share context:

```bash
# Create the shared directory structure
mkdir -p ~/clawd/agents/_shared/bin

# Symlink shared files into each agent's workspace
for agent_dir in ~/clawd/agents/*/; do
  agent=$(basename "$agent_dir")
  [[ "$agent" == "_shared" || "$agent" == "_template" ]] && continue
  ln -sf ~/clawd/agents/_shared "$agent_dir/shared"
done
```

---

## Step 6 — Seed Initial Context

Log your first facts to get started:

```bash
# Set up your user profile
~/clawd/scripts/graphiti-log.sh user-main user "User" "User is based in [CITY], timezone [TZ]."

# Set up your agent roster
~/clawd/scripts/graphiti-log.sh system-shared system "System" "Agent team: [main-agent] (orchestrator), [agent-2] (specialty)..."

# Log your first discovery
~/clawd/scripts/graphiti-log.sh clawdbot-main agent "GG" "Graphiti memory plugin installed on [DATE]"
```

---

## Step 7 — Verify Everything Works

```bash
# Check all memory layers
~/clawd/scripts/memory-status.sh

# Search the knowledge graph
~/clawd/scripts/graphiti-search.sh "what agents are configured"

# Search across QMD + Graphiti together
~/clawd/scripts/memory-hybrid-search.sh "user preferences"
```

Expected output from `memory-status.sh`:
```
✓ Graphiti API:  http://localhost:8001/health → 200
✓ Neo4j:         docker ps | grep neo4j → running
✓ Scripts:      ~/clawd/scripts/*.sh → found
✓ Config:       OpenClaw memorySearch → enabled
```

---

## One-Command Installer

If you prefer the automated install (macOS/Linux with Docker already set up):

```bash
curl -fsSL https://raw.githubusercontent.com/mgkcloud/openclaw-graphiti-memory/main/install.sh | bash
```

The installer will:
1. Check Docker + QMD prerequisites
2. Create all necessary directories
3. Download scripts to `~/clawd/scripts/`
4. Deploy Graphiti via Docker
5. Create sample `MEMORY.md`
6. Print the OpenClaw config snippet

---

## Architecture Overview

```
Your OpenClaw Agent
│
├── Private Memory (QMD)
│   ~/clawd/memory/
│   ├── sessions/       ← session transcripts (auto-archived)
│   ├── projects/       ← project-specific notes
│   └── system/        ← agent's own system notes
│
├── Shared Files (_shared/)
│   ~/clawd/agents/_shared/
│   ├── user-profile.md     ← user context (read by all agents)
│   ├── agent-roster.md     ← who's on the team
│   └── infrastructure.md   ← servers, ports, credentials
│
└── Temporal Knowledge (Graphiti)
    http://localhost:8001
    └── Neo4j (graph database)
        Groups:
        - clawdbot-main      ← your main agent's discoveries
        - user-main          ← user profile facts
        - system-shared      ← infrastructure + roster facts
```

---

## API Reference

### graphiti-search.sh

```bash
# Global search (all groups)
~/clawd/scripts/graphiti-search.sh "user timezone"

# Search specific group only
~/clawd/scripts/graphiti-search.sh "deployment config" clawdbot-main

# Limit results
~/clawd/scripts/graphiti-search.sh "bitcoin" user-main 5
```

### graphiti-log.sh

```bash
# Log a fact to your agent's group
~/clawd/scripts/graphiti-log.sh clawdbot-main agent "YourAgent" "Fact goes here"

# Log to system-shared (orchestrator only)
~/clawd/scripts/graphiti-log.sh system-shared system "System" "Infrastructure fact"
```

### graphiti-context.sh

```bash
# Get full hybrid context for a task
~/clawd/scripts/graphiti-context.sh "deploy the new trading bot"
```

---

## Cost

| Component | Cost |
|-----------|------|
| QMD vector search | Free (uses Gemini embeddings) |
| Graphiti ingestion | OpenAI API calls during sync only |
| Graphiti search | Free (local Neo4j query) |
| Neo4j storage | Free (local) |
| **Typical monthly** | **< $1/month** for a busy 5-agent setup |

---

## Troubleshooting

### Graphiti API not responding

```bash
# Check containers
docker ps | grep -E 'graphiti|neo4j'

# Restart
cd ~/services/graphiti && docker compose restart

# Check logs
docker compose logs -f graphiti
```

### Neo4j connection refused

```bash
# Check Neo4j is running
docker ps | grep neo4j

# Check logs
docker compose logs neo4j

# Default credentials: neo4j / graphiti
# Connect browser to http://localhost:7474
```

### OpenClaw memory search not working

```bash
# Check QMD is installed
which qmd || npm install -g qmd

# Check config applied
cat ~/.openclaw/openclaw.json | grep -A5 memorySearch

# Restart gateway
openclaw gateway restart
```

### Permission denied on scripts

```bash
chmod +x ~/clawd/scripts/*.sh ~/clawd/scripts/*.py
```

---

## Updating

```bash
cd ~/graphiti-memory && git pull
cp scripts/* ~/clawd/scripts/
cd ~/services/graphiti && docker compose pull && docker compose up -d
```
