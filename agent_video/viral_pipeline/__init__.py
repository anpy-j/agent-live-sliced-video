"""Independent viral-reference editing pipeline.

This package intentionally does not import the legacy ``agent_video.pipeline``
business stages.  It owns its database, artifacts and stage vocabulary so V2
can evolve without changing the production S1-S5 workflow.
"""

from .service import ViralPipelineService

__all__ = ["ViralPipelineService"]
