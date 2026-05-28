# fdi_office_automation

**Purpose**: Orchestrate overnight backlog processing, project rebuilds, and real-time QField synchronization for multi-team office coordination.

**Execution Model**:
- Schedule logic: All timing stored in `.env` variables (not hardcoded in scripts)
- comercial_maps, Phase 1, Phase 2: Trigger via Docker exec API calls (no embedded schedules)
- Office orchestrator: Calls Docker containers with CLI arguments
- All processing: Runs inside Docker for consistency and auditability

## Project Structure

```
fdi_office_automation/
├── README.md (this file)
├── .env.template (environment configuration template)
├── .env (actual config — NOT checked in)
├── modelos/
│   ├── __init__.py
│   ├── config/
│   │   ├── qfield_layer_discovery.json (P1-N11 — canonical layer patterns)
│   │   └── reference_codes.json (shared type codes)
│   ├── orchestrator/
│   │   ├── batch_processor.py (OFFICE-ORCHESTRATOR core)
│   │   ├── dependency_graph.py (phase ordering + validation)
│   │   └── project_discovery.py (SIG_[project] discovery)
│   ├── qfield/
│   │   ├── aggregator.py (OFFICE-QFIELD-WATCHER layer aggregation)
│   │   ├── conflict_detector.py (OFFICE-SYNC-CONFLICTS — Tier 6)
│   │   └── sync_monitor.py (QField sync state tracking)
│   └── helpers/
│       ├── docker_executor.py (Docker exec wrapper with logging)
│       ├── log_manager.py (centralized logging + metric tracking)
│       └── notification.py (email alerts for admin on failures)
├── scripts/
│   ├── overnight_predios_processor.py (OFFICE-N1)
│   ├── overnight_rebuild_trigger.py (OFFICE-N2)
│   ├── project_orchestrator.py (OFFICE-ORCHESTRATOR CLI)
│   ├── qfield_watcher.py (OFFICE-QFIELD-WATCHER daemon)
│   └── conflict_detector.py (OFFICE-SYNC-CONFLICTS — Tier 6)
├── tests/
│   ├── test_batch_processor.py
│   ├── test_qfield_watcher.py
│   └── test_docker_executor.py
└── logs/ (created at runtime)
    ├── overnight_predios_YYYY-MM-DD.log
    ├── overnight_rebuild_YYYY-MM-DD.log
    ├── qfield_watcher_YYYY-MM-DD.log
    └── orchestration_YYYY-MM-DD.log
```

## Configuration Files

### .env (environment variables)
```bash
# Overnight processing schedule
OVERNIGHT_PREDIOS_TIME=22:00
OVERNIGHT_REBUILD_TIME=06:00
QFIELD_SYNC_INTERVAL=300  # seconds between QField watcher checks

# Project discovery
SIG_BASE=/Users/andre/Sync/FdI/SIG
PREDIOS_FOLDER=/Users/andre/Sync/FdI/SIG/_Predios

# Docker container names
DOCKER_CONTAINER_COMERCIAL=qgis-comercial-processor
DOCKER_CONTAINER_PHASE1=qgis-py-phase1
DOCKER_CONTAINER_PHASE2=qgis-py-phase2

# Workspace paths
WORKSPACE_COMERCIAL=/workspace
WORKSPACE_PHASE1=/workspace
WORKSPACE_PHASE2=/workspace

# Logging + admin
LOG_DIR=/Users/andre/Sync/FdI/SIG/logs
ADMIN_EMAIL=admin@example.com
ALERT_ON_FAILURE=true
```

### qfield_layer_discovery.json (P1-N11)
Canonical layer naming patterns for automated discovery across all SIG_[project] folders.

## Implementation Phases

### Tier 4 (Weeks 3-5) — Office Automation Core
1. **OFFICE-N1** — overnight predios processor
2. **OFFICE-N2** — overnight rebuild trigger
3. **OFFICE-ORCHESTRATOR** — master batch processor
4. **OFFICE-QFIELD-WATCHER** — file system monitor (depends on P1-N11)

### Tier 5 (Weeks 4-7) — Project Template + Dashboard
1. **P1-N11** — QField layer discovery config (PREREQUISITE for Tier 4)
2. **P1-N12** — EPT admin dashboard with aggregation

### Tier 6 (Deferred/Wave 3) — Advanced Features
1. **OFFICE-SYNC-CONFLICTS** — conflict detector + remediation

## Quick Start

```bash
# Clone repo
git clone https://github.com/[org]/fdi_office_automation.git
cd fdi_office_automation

# Create environment
cp .env.template .env
# Edit .env with your paths and Docker container names

# Create directories
mkdir -p logs outputs_admin

# Install dependencies (Python 3.9+)
pip install watchdog pyyaml requests

# Run overnight predios processor (manual test)
python3 scripts/overnight_predios_processor.py --dry-run

# Run batch orchestrator (manual test)
python3 scripts/project_orchestrator.py --mode batch --projects SIG_Artosas --include-phases comercial_maps,phase1 --dry-run

# Start QField watcher daemon (continuous)
python3 scripts/qfield_watcher.py --watch-path /Users/andre/Sync/FdI/SIG --daemon
```

## Cron Configuration (Admin Setup)

```bash
# Add to /etc/crontab or crontab -e

# Overnight predios processing at 22:00
0 22 * * * /usr/bin/python3 /path/to/fdi_office_automation/scripts/overnight_predios_processor.py >> /path/to/logs/cron.log 2>&1

# Overnight rebuild at 06:00
0 6 * * * /usr/bin/python3 /path/to/fdi_office_automation/scripts/overnight_rebuild_trigger.py >> /path/to/logs/cron.log 2>&1

# QField watcher (runs continuously as systemd service or screen session)
# See: scripts/qfield_watcher.py --daemon
```

## Status

- **P1-N11 Config**: IN PROGRESS (creating qfield_layer_discovery.json)
- **OFFICE-N1**: PLANNED (Tier 4)
- **OFFICE-N2**: PLANNED (Tier 4)
- **OFFICE-ORCHESTRATOR**: PLANNED (Tier 4)
- **OFFICE-QFIELD-WATCHER**: PLANNED (Tier 4, depends on P1-N11)
- **P1-N12**: PLANNED (Tier 5)
- **OFFICE-SYNC-CONFLICTS**: PLANNED (Tier 6, deferred)

---

**Last Updated**: May 12, 2026  
**Implementation Target**: Start Tier 4 week of May 19, 2026
