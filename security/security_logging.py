"""
security/security_logging.py — Centralized security audit logging

Logs all verification attempts, failures, and sensitive actions
to a local-only JSON lines file for audit trail.
"""

from __future__ import annotations

from utils.logger import get_logger

import json
import logging
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, Any

log = get_logger(__name__)
logger = log  # back-compat alias
class SecurityAuditor:
    """Centralized security audit logging."""
    
    def __init__(self, log_dir: Optional[Path] = None):
        self.log_dir = log_dir or (Path.home() / ".gama" / "security")
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.log_file = self.log_dir / "audit.jsonl"
    
    def log_verification_attempt(
        self,
        verification_type: str,  # "speaker", "face", "confirm"
        username: str,
        result: bool,  # True = verified, False = failed
        similarity: float,
        confidence: float,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Log a verification attempt."""
        entry = {
            "timestamp": datetime.now().isoformat(),
            "event_type": "verification_attempt",
            "verification_type": verification_type,
            "username": username,
            "result": "verified" if result else "failed",
            "similarity": round(similarity, 4),
            "confidence": round(confidence, 2),
            "details": details or {},
        }
        self._write_log_entry(entry)
    
    def log_command_execution(
        self,
        tool_name: str,
        action: str,
        trust_level: str,
        username: str,
        verified: bool,
        reason: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Log a command execution decision."""
        entry = {
            "timestamp": datetime.now().isoformat(),
            "event_type": "command_execution",
            "tool_name": tool_name,
            "action": action,
            "trust_level": trust_level,
            "username": username,
            "executed": verified,
            "reason": reason,
            "details": details or {},
        }
        self._write_log_entry(entry)
    
    def log_failed_verification_chain(
        self,
        username: str,
        attempt_number: int,
        failures: Dict[str, Any],
    ) -> None:
        """Log a failed verification chain attempt."""
        entry = {
            "timestamp": datetime.now().isoformat(),
            "event_type": "verification_chain_failed",
            "username": username,
            "attempt": attempt_number,
            "failures": failures,
        }
        self._write_log_entry(entry)
    
    def log_security_incident(
        self,
        incident_type: str,  # "unauthorized_access_attempt", "intrusion_attempt", etc.
        username: Optional[str],
        details: Dict[str, Any],
    ) -> None:
        """Log a security incident."""
        entry = {
            "timestamp": datetime.now().isoformat(),
            "event_type": "security_incident",
            "incident_type": incident_type,
            "username": username or "unknown",
            "severity": details.get("severity", "medium"),
            "details": details,
        }
        self._write_log_entry(entry)
    
    def log_system_event(
        self,
        event_type: str,
        description: str,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Log a general system event."""
        entry = {
            "timestamp": datetime.now().isoformat(),
            "event_type": "system_event",
            "system_event_type": event_type,
            "description": description,
            "details": details or {},
        }
        self._write_log_entry(entry)
    
    def _write_log_entry(self, entry: Dict[str, Any]) -> None:
        """Write entry to log file."""
        try:
            # Append to JSON lines file
            with open(self.log_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception as e:
            logger.error(f"Failed to write security log: {e}")
    
    def get_recent_logs(self, limit: int = 100) -> list[Dict[str, Any]]:
        """Get recent log entries."""
        try:
            logs = []
            if not self.log_file.exists():
                return logs
            
            with open(self.log_file, "r", encoding="utf-8") as f:
                lines = f.readlines()
                # Get last 'limit' entries
                for line in lines[-limit:]:
                    try:
                        logs.append(json.loads(line.strip()))
                    except json.JSONDecodeError:
                        continue
            
            return logs
        except Exception as e:
            logger.error(f"Failed to read security logs: {e}")
            return []
    
    def get_failed_attempts(self, username: Optional[str] = None) -> list[Dict[str, Any]]:
        """Get recent failed verification attempts."""
        try:
            logs = self.get_recent_logs(limit=1000)
            failed = [
                log for log in logs
                if log.get("event_type") in ["verification_attempt", "verification_chain_failed"]
                and log.get("result") == "failed"
            ]
            
            if username:
                failed = [log for log in failed if log.get("username") == username]
            
            return failed
        except Exception as e:
            logger.error(f"Failed to get failed attempts: {e}")
            return []
    
    def get_security_incidents(self) -> list[Dict[str, Any]]:
        """Get recent security incidents."""
        try:
            logs = self.get_recent_logs(limit=1000)
            incidents = [
                log for log in logs
                if log.get("event_type") == "security_incident"
            ]
            return incidents
        except Exception as e:
            logger.error(f"Failed to get incidents: {e}")
            return []


# Global instance
_auditor: Optional[SecurityAuditor] = None


def get_auditor() -> SecurityAuditor:
    """Get or create global security auditor."""
    global _auditor
    if _auditor is None:
        _auditor = SecurityAuditor()
    return _auditor
