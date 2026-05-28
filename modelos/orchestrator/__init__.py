"""
orchestrator — Batch processing and pipeline orchestration

Core modules:
- batch_processor: OFFICE-ORCHESTRATOR main logic
- dependency_graph: Phase ordering and validation
- project_discovery: SIG_[project] discovery and metadata
"""

__all__ = [
    "batch_processor",
    "dependency_graph",
    "project_discovery",
]
