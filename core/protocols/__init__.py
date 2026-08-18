"""
core.protocols — Gama's first-class JARVIS-style Protocol System.

Public surface:
    Protocol, ProtocolStep, ProtocolExecutionRecord,
    ActionType, OnFailureStrategy, PermissionLevel  (core.protocols.models)
    protocol_storage                                (core.protocols.storage)
    protocol_registry                               (core.protocols.registry)
    protocol_parser                                 (core.protocols.parser)
    protocol_executor                               (core.protocols.executor)
    protocol_manager, ProtocolManager               (core.protocols.manager)

Everything else in the app should go through `protocol_manager` — see
actions/protocol_engine.py for the natural-language tool entry point.
"""

from core.protocols.models import (
    Protocol,
    ProtocolStep,
    ProtocolExecutionRecord,
    ActionType,
    OnFailureStrategy,
    PermissionLevel,
)
from core.protocols.storage import protocol_storage
from core.protocols.registry import protocol_registry
from core.protocols.parser import protocol_parser
from core.protocols.executor import protocol_executor
from core.protocols.manager import ProtocolManager, protocol_manager

__all__ = [
    "Protocol",
    "ProtocolStep",
    "ProtocolExecutionRecord",
    "ActionType",
    "OnFailureStrategy",
    "PermissionLevel",
    "protocol_storage",
    "protocol_registry",
    "protocol_parser",
    "protocol_executor",
    "ProtocolManager",
    "protocol_manager",
]
