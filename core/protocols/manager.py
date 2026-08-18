"""
core/protocols/manager.py — Protocol Manager facade
================================================================================
The single entry point the rest of Gama (actions.protocol_engine, and
eventually a Protocol Manager UI) talks to. Wires together storage, registry,
parser, and executor without exposing their internals.
"""

from __future__ import annotations

from utils.logger import get_logger

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core.protocols.models import Protocol, ProtocolExecutionRecord
from core.protocols.storage import protocol_storage
from core.protocols.registry import protocol_registry
from core.protocols.parser import protocol_parser
from core.protocols.executor import protocol_executor

log = get_logger(__name__)
logger = log  # back-compat alias
class ProtocolManager:
    """High-level CRUD + execution control for Protocols."""

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------
    def create_protocol(
        self,
        identifier: str,
        steps_text: str,
        description: str = "",
        category: str = "General",
        confirmation_required: bool = False,
    ) -> Tuple[bool, str, Optional[Protocol]]:
        existing = protocol_registry.resolve(identifier)
        if existing is not None:
            return False, f"{existing.display_identifier} already exists. Use rename/edit or pick a different name.", None

        protocol = protocol_parser.build_protocol_from_prompt(
            identifier, steps_text, description, category, confirmation_required
        )
        protocol_storage.save_protocol(protocol)
        step_count = len(protocol.steps)
        return (
            True,
            f"{protocol.display_identifier} created with {step_count} step(s). "
            f"Say 'run {protocol.display_name}' whenever you're ready, Sir.",
            protocol,
        )

    def execute_protocol(self, identifier: str, parameters: Optional[Dict[str, Any]] = None, task_id: Optional[str] = None) -> Tuple[bool, str]:
        protocol = protocol_registry.resolve(identifier)
        if protocol is None:
            return False, f"I couldn't find a protocol matching '{identifier}', Sir."

        from core.protocols.models import PermissionLevel
        if protocol.permission_level != PermissionLevel.INSTANT.value:
            # Confirmation-required protocols still get kicked off here — the
            # calling layer (voice/text intent handler) is expected to have
            # already gotten explicit confirmation before invoking this, per
            # Gama's existing confirmation.py flow.
            logger.info(f"[protocols.manager] Executing confirmation-required protocol: {protocol.display_name}")

        ok, msg, _execution_id = protocol_executor.execute_protocol(identifier, parameters=parameters, task_id=task_id)
        return ok, msg

    def delete_protocol(self, identifier: str) -> Tuple[bool, str]:
        protocol = protocol_registry.resolve(identifier)
        if protocol is None:
            return False, f"I couldn't find a protocol matching '{identifier}', Sir."
        protocol_storage.delete_protocol(protocol.id)
        return True, f"{protocol.display_identifier} deleted."

    def list_protocols(self, category: Optional[str] = None) -> List[Protocol]:
        protocols = protocol_storage.get_all()
        if category:
            protocols = [p for p in protocols if p.category.lower() == category.lower()]
        return sorted(protocols, key=lambda p: (p.numeric_id is None, p.numeric_id or 0, p.display_name.lower()))

    def search_protocols(self, query: str) -> List[Protocol]:
        return protocol_registry.search(query)

    def rename_protocol(self, identifier: str, new_name: str) -> Tuple[bool, str]:
        protocol = protocol_registry.resolve(identifier)
        if protocol is None:
            return False, f"I couldn't find a protocol matching '{identifier}', Sir."
        old_name = protocol.display_name
        protocol.display_name = new_name.strip()
        protocol_storage.save_protocol(protocol)
        return True, f"Renamed '{old_name}' to '{protocol.display_name}'."

    def duplicate_protocol(self, identifier: str, new_identifier: str) -> Tuple[bool, str, Optional[Protocol]]:
        source = protocol_registry.resolve(identifier)
        if source is None:
            return False, f"I couldn't find a protocol matching '{identifier}', Sir.", None

        import copy
        clone = copy.deepcopy(source)
        clone.id = __import__("uuid").uuid4().hex
        clone.numeric_id = None
        clone.display_name = new_identifier if not new_identifier.isdigit() else f"{source.display_name} Copy"
        clone.run_count = 0
        clone.last_run_at = None
        protocol_storage.save_protocol(clone)
        return True, f"Duplicated '{source.display_name}' as '{clone.display_name}'.", clone

    # ------------------------------------------------------------------
    # Import / export
    # ------------------------------------------------------------------
    def export_protocols(self, filepath: Optional[str] = None) -> str:
        protocols = protocol_storage.get_all()
        payload = json.dumps([p.to_dict() for p in protocols], indent=2)
        if filepath:
            try:
                Path(filepath).expanduser().write_text(payload, encoding="utf-8")
                return f"Exported {len(protocols)} protocol(s) to {filepath}."
            except Exception as exc:
                return f"Failed to export protocols: {exc}"
        return payload

    def import_protocols(self, json_data_or_file: str) -> Tuple[bool, str]:
        try:
            text = json_data_or_file
            # Only treat the input as a filesystem path if it's short enough
            # to plausibly be one — long JSON payloads would otherwise blow
            # up Path.exists() with an OSError on some platforms.
            if len(json_data_or_file) < 260:
                try:
                    path = Path(json_data_or_file).expanduser()
                    if path.exists():
                        text = path.read_text(encoding="utf-8")
                except OSError:
                    pass
            raw = json.loads(text)
            if isinstance(raw, dict):
                raw = [raw]
            imported = 0
            for item in raw:
                protocol = Protocol.from_dict(item)
                protocol.numeric_id = None  # renumber to avoid collisions
                protocol.id = __import__("uuid").uuid4().hex
                protocol_storage.save_protocol(protocol)
                imported += 1
            return True, f"Imported {imported} protocol(s)."
        except Exception as exc:
            return False, f"Failed to import protocols: {exc}"

    # ------------------------------------------------------------------
    # Execution control
    # ------------------------------------------------------------------
    def pause_protocol(self, execution_id: Optional[str] = None) -> bool:
        return protocol_executor.pause_execution(execution_id)

    def resume_protocol(self, execution_id: Optional[str] = None) -> bool:
        return protocol_executor.resume_execution(execution_id)

    def cancel_protocol(self, execution_id: Optional[str] = None) -> bool:
        return protocol_executor.cancel_execution(execution_id)

    def skip_step(self, execution_id: Optional[str] = None) -> bool:
        return protocol_executor.skip_current_step(execution_id)

    def get_history(self, limit: int = 20) -> List[ProtocolExecutionRecord]:
        return protocol_executor.get_execution_history(limit=limit)

    def get_active_executions(self) -> List[ProtocolExecutionRecord]:
        return protocol_executor.get_active_executions()


protocol_manager = ProtocolManager()

__all__ = ["ProtocolManager", "protocol_manager"]
