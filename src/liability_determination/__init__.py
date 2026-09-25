"""交通事故责任认定的基础组件。"""

from .liability import (
    LEVEL_LABELS,
    MATERIAL_CATEGORIES,
    PARTY_KINDS,
    TRIGGER_REASONS,
    liability_level,
    render_service_document,
)
from .service import LiabilityDeterminationService

__all__ = [
    "LEVEL_LABELS",
    "MATERIAL_CATEGORIES",
    "PARTY_KINDS",
    "TRIGGER_REASONS",
    "LiabilityDeterminationService",
    "liability_level",
    "render_service_document",
]

__version__ = "0.1.0"
