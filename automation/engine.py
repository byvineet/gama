"""
automation/engine.py — AutomationEngine: the "understand goal -> generate
plan -> execute -> verify" orchestrator described in the spec.

Design
------
Goal parsing here is deliberately rule-based/regex, NOT an LLM call —
per the Performance section, simple automation must plan in well under
100ms. Gama's existing intent layer (core/fast_intent.py) or an LLM
tool-call can still be the thing that decides "this is an automation
goal" and hands the free-text goal to `automation_engine.run(text)`;
this module only owns turning that text into capability calls.

Patterns are tried first (covers the multi-step example commands from
the spec verbatim). Anything that doesn't match a pattern falls back
to CapabilityRegistry.find_by_keywords() for a single best-guess step,
so the engine degrades gracefully instead of refusing unknown goals.
"""

from __future__ import annotations

import re
import threading
import time
from pathlib import Path
from typing import Optional

from utils.logger import get_logger
from utils.windows_paths import resolve_user_path
from automation import providers  # noqa: F401 -- registers all capabilities
from automation.models import AutomationPlan, PlanStepSpec
from automation.registry import registry
from automation.executor import execute_plan, ExecutionReport

log = get_logger(__name__)

try:
    from state_engine.event_bus import event_bus
except Exception:  # pragma: no cover
    event_bus = None

_HOME = Path.home()


def _resolve_dir(name: str) -> str:
    """Same Windows Known-Folder resolution actions/file_controller.py
    uses (desktop/downloads/documents/etc. all resolve correctly even if
    the user relocated them) — see utils/windows_paths.py."""
    return str(resolve_user_path(name.strip()))


class AutomationEngine:
    """Process-wide singleton. Stateless between calls except for the
    registry/provider caches each capability owns internally."""

    def __init__(self) -> None:
        self._patterns = self._build_patterns()

    # ── goal -> AutomationPlan ───────────────────────────────────────────
    def _build_patterns(self):
        """List of (regex, plan_builder) tried in order, first match wins."""
        P = []

        def organize(goal, m):
            target = m.group(1) if m.groups() else "desktop"
            path = _resolve_dir(target)
            return [PlanStepSpec(f"Organize {target}", "file.organize_folder", {"path": path})]
        P.append((re.compile(r"organi[sz]e (?:my |the )?(\w+)", re.I), organize))

        def compress_images(goal, m):
            target = m.group(1) if m.groups() else "desktop"
            path = _resolve_dir(target)
            return [PlanStepSpec(f"Compress every image in {target}", "file.compress_images", {"path": path})]
        P.append((re.compile(r"compress (?:every|all) image", re.I), compress_images))

        def extract_zips(goal, m):
            # "extract every zip [in <folder>]"
            folder_m = re.search(r"in (?:my |the )?(\w+)", goal, re.I)
            folder = _resolve_dir(folder_m.group(1)) if folder_m else _resolve_dir("downloads")
            p = Path(folder)
            steps = []
            if p.is_dir():
                for zf in p.glob("*.zip"):
                    steps.append(PlanStepSpec(f"Extract {zf.name}", "file.extract", {"src": str(zf)}))
            if not steps:
                steps.append(PlanStepSpec("No zip files found", "file.extract",
                                           {"src": str(p / "__none__.zip")}, critical=False))
            return steps
        P.append((re.compile(r"extract (?:every|all) zip", re.I), extract_zips))

        def rename_screenshots(goal, m):
            folder = _resolve_dir("desktop")
            p = Path(folder)
            steps = []
            if p.is_dir():
                shots = sorted([f for f in p.iterdir()
                                 if f.is_file() and "screenshot" in f.name.lower()])
                for i, f in enumerate(shots, 1):
                    new_name = f"Screenshot_{i:03d}{f.suffix}"
                    steps.append(PlanStepSpec(f"Rename {f.name} -> {new_name}", "file.rename",
                                               {"path": str(f), "new_name": new_name}))
            if not steps:
                steps.append(PlanStepSpec("No screenshots found", "file.rename",
                                           {"path": str(p / "__none__"), "new_name": "x"}, critical=False))
            return steps
        P.append((re.compile(r"rename (?:all|every) screenshot", re.I), rename_screenshots))

        def move_pdfs(goal, m):
            dest_m = re.search(r"into (?:my |the )?([\w\s]+)$", goal, re.I)
            dest_name = dest_m.group(1).strip() if dest_m else "Study Notes"
            src_dir = Path(_resolve_dir("downloads"))
            dest_dir = _HOME / dest_name
            steps = [PlanStepSpec(f"Create folder {dest_name}", "file.create_folder", {"path": str(dest_dir)})]
            if src_dir.is_dir():
                for pdf in src_dir.glob("*.pdf"):
                    steps.append(PlanStepSpec(f"Move {pdf.name}", "file.move",
                                               {"src": str(pdf), "dst": str(dest_dir / pdf.name)}))
            return steps
        P.append((re.compile(r"move (?:all |every )?pdf", re.I), move_pdfs))

        def move_by_extension(goal, m):
            # "move all files with the extension .pdf from Downloads to
            # Documents" / "move every .jpg file from downloads to
            # pictures" / "move pdf files from x to y" — any extension,
            # any two named folders (resolved via the same Known-Folder
            # lookup as everywhere else, so "downloads"/"documents"/etc.
            # work regardless of where the user relocated them).
            ext = m.group(1).lower().lstrip(".")
            src_name, dest_name = m.group(2).strip(), m.group(3).strip()
            src_dir = Path(_resolve_dir(src_name))
            dest_dir = Path(_resolve_dir(dest_name))
            steps = []
            if not dest_dir.exists():
                steps.append(PlanStepSpec(f"Create folder {dest_name}", "file.create_folder",
                                           {"path": str(dest_dir)}))
            if src_dir.is_dir():
                for f in sorted(src_dir.iterdir()):
                    if f.is_file() and f.suffix.lower().lstrip(".") == ext:
                        steps.append(PlanStepSpec(f"Move {f.name}", "file.move",
                                                   {"src": str(f), "dst": str(dest_dir / f.name)}))
            if len(steps) <= (1 if not dest_dir.exists() else 0):
                steps.append(PlanStepSpec(f"No .{ext} files found in {src_name}", "file.move",
                                           {"src": "", "dst": ""}, critical=False))
            return steps
        P.append((re.compile(
            r"move\s+(?:all\s+|every\s+)?(?:files?\s+)?(?:with\s+(?:the\s+)?extension\s+)?"
            r"\.?(\w{2,5})\s*(?:files?\s+)?from\s+(?:my\s+|the\s+)?([\w\s]+?)\s+(?:folder\s+)?"
            r"to\s+(?:my\s+|the\s+)?([\w\s]+?)(?:\s+folder)?$",
            re.I), move_by_extension))

        def close_all_except(goal, m):
            keep = m.group(1).strip()
            return [PlanStepSpec(f"Close everything except {keep}", "window.close_all_except", {"keep": keep})]
        P.append((re.compile(r"close (?:every|all)(?:thing)?\s*(?:browsers?)?\s*except (\w+)", re.I),
                  close_all_except))

        def mute_app(goal, m):
            app = m.group(1).strip()
            return [PlanStepSpec(f"Mute {app}", "media.mute_app", {"name": app, "mute": True})]
        P.append((re.compile(r"mute (\w+)", re.I), mute_app))

        def archive_old(goal, m):
            folder_m = re.search(r"archive (?:old |my )?([\w\s]+)$", goal, re.I)
            target = folder_m.group(1).strip() if folder_m else "Projects"
            src = _HOME / target
            dst = src.parent / f"{target} (Archive)" / f"{target}.zip"
            return [PlanStepSpec(f"Archive {target}", "file.compress", {"paths": [str(src)], "dst": str(dst)})]
        P.append((re.compile(r"archive (?:old|my)? ?projects?", re.I), archive_old))

        def delete_target(goal, m):
            # Whatever follows the trigger word is the (possibly
            # conversational: "it", "that download", "the report.pdf")
            # target. file.delete's underlying resolver — see
            # actions/context_resolver.py — turns this into a concrete
            # path, asks for clarification, or reports "not found";
            # it is never handed an empty/unvalidated path.
            target = (m.group(1) or "").strip() if m.groups() else ""
            target = re.sub(r"^(please\s+)", "", target, flags=re.I).strip()
            label = target if target else "it"
            return [PlanStepSpec(f"Delete {label}", "file.delete", {"path": target})]
        P.append((re.compile(r"^(?:please\s+)?(?:delete|remove|trash)\s*(.*)$", re.I), delete_target))

        def open_app(goal, m):
            name = m.group(1).strip()
            return [PlanStepSpec(f"Open {name}", "app.launch", {"name": name})]
        P.append((re.compile(r"^(?:open|launch|start) (.+)$", re.I), open_app))

        def close_app(goal, m):
            name = m.group(1).strip()
            return [PlanStepSpec(f"Close {name}", "app.close", {"name": name})]
        P.append((re.compile(r"^close (.+)$", re.I), close_app))

        def maximize_win(goal, m):
            name = m.group(1).strip()
            return [PlanStepSpec(f"Maximize {name}", "window.snap", {"title": name, "side": "max"})]
        P.append((re.compile(r"maximize (.+)", re.I), maximize_win))

        def snap_win(goal, m):
            name, side = m.group(1).strip(), m.group(2).lower()
            return [PlanStepSpec(f"Move {name} {side}", "window.snap", {"title": name, "side": side})]
        P.append((re.compile(r"(?:move|snap) (.+?) (left|right)$", re.I), snap_win))

        def set_volume(goal, m):
            level = int(m.group(1))
            return [PlanStepSpec(f"Set volume to {level}%", "media.set_volume", {"level": level})]
        P.append((re.compile(r"(?:set )?volume (?:to )?(\d{1,3})%?", re.I), set_volume))

        # ── Intent Chaining: contextual mode launchers (Part 4) ────────────
        # "I'm studying" / "study mode" / "study session"
        def study_mode(goal, m):
            return [
                PlanStepSpec("Enable focus mode", "power.focus_mode", {}),
                PlanStepSpec("Set volume to 30%", "media.set_volume", {"level": 30}),
                PlanStepSpec("Open browser", "app.launch", {"name": "msedge"}, critical=False),
                PlanStepSpec("Open notes", "app.launch", {"name": "notepad"}, critical=False),
            ]
        P.append((re.compile(r"(?:i'?m\s+)?(?:start(?:ing)?|begin(?:ning)?|switch(?:ing)?\s+to\s+)?study(?:\s+mode|\s+session)?", re.I), study_mode))

        # "coding session" / "I'm coding" / "start coding"
        def coding_mode(goal, m):
            return [
                PlanStepSpec("Open VS Code", "app.launch", {"name": "code"}, critical=False),
                PlanStepSpec("Open terminal", "app.launch", {"name": "wt"}, critical=False),
                PlanStepSpec("Open browser", "app.launch", {"name": "msedge"}, critical=False),
            ]
        P.append((re.compile(r"(?:i'?m\s+)?(?:start(?:ing)?|begin(?:ning)?)?\s*cod(?:ing|e)\s*(?:session|mode)?", re.I), coding_mode))
        P.append((re.compile(r"(?:start|begin|setup|set up)\s+(?:my\s+)?(?:coding|developer?|dev)\s+(?:session|workspace|mode|environment)", re.I), coding_mode))

        # "gaming mode" / "I'm gaming"
        def gaming_mode(goal, m):
            return [
                PlanStepSpec("Open Steam", "app.launch", {"name": "steam"}, critical=False),
                PlanStepSpec("Set volume to 60%", "media.set_volume", {"level": 60}),
            ]
        P.append((re.compile(r"(?:i'?m\s+)?(?:start(?:ing)?|begin(?:ning)?)?\s*gaming\s*(?:mode|session)?", re.I), gaming_mode))
        P.append((re.compile(r"(?:start|begin|setup|set up)\s+(?:my\s+)?gaming\s+(?:session|mode|setup)", re.I), gaming_mode))

        # "work mode" / "work session" / "I'm working"
        def work_mode(goal, m):
            return [
                PlanStepSpec("Open browser", "app.launch", {"name": "msedge"}, critical=False),
                PlanStepSpec("Open notes", "app.launch", {"name": "notepad"}, critical=False),
            ]
        P.append((re.compile(r"(?:i'?m\s+)?(?:start(?:ing)?\s+)?work(?:ing)?\s*(?:mode|session)?", re.I), work_mode))
        P.append((re.compile(r"(?:start|begin|set up)\s+(?:my\s+)?work(?:ing)?\s+(?:session|mode|setup)", re.I), work_mode))

        def lock_pc(goal, m):
            return [PlanStepSpec("Lock the PC", "power.lock", {})]
        P.append((re.compile(r"^lock(?: (?:the|my) (?:pc|computer))?$", re.I), lock_pc))

        def shutdown_pc(goal, m):
            return [PlanStepSpec("Shut down the PC", "power.shutdown", {"delay_seconds": 0})]
        P.append((re.compile(r"shut ?down", re.I), shutdown_pc))

        def restart_pc(goal, m):
            return [PlanStepSpec("Restart the PC", "power.restart", {"delay_seconds": 0})]
        P.append((re.compile(r"restart (?:the |my )?(?:pc|computer)", re.I), restart_pc))

        def sleep_pc(goal, m):
            return [PlanStepSpec("Sleep", "power.sleep", {})]
        P.append((re.compile(r"^(?:go to )?sleep$", re.I), sleep_pc))

        return P

    def build_plan(self, goal: str) -> AutomationPlan:
        goal = goal.strip()
        for pattern, builder in self._patterns:
            m = pattern.search(goal)
            if m:
                steps = builder(goal, m)
                return AutomationPlan(goal=goal, steps=steps)

        # Fallback: naive keyword match against every registered capability.
        # Try candidates in score order and skip any whose required
        # arguments we can't actually supply — e.g. window.move needs
        # title/x/y, which a bare keyword match has no way to fill in.
        # Without this check the executor used to call cap.run(**{}) and
        # crash with a raw TypeError instead of just trying the next,
        # more-appropriate candidate (or giving up cleanly).
        import inspect as _inspect
        candidates = registry.find_by_keywords(goal)
        for cap in candidates:
            kwargs: dict = {}
            if cap.name.startswith("file.") and cap.name != "file.organize_folder":
                for kw in cap.keywords:
                    idx = goal.lower().find(kw)
                    if idx != -1:
                        leftover = goal[idx + len(kw):].strip()
                        break
                else:
                    leftover = ""
                kwargs = {"path": leftover}

            try:
                sig = _inspect.signature(cap.run)
                missing_required = [
                    p.name for p in sig.parameters.values()
                    if p.default is _inspect.Parameter.empty
                    and p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)
                    and p.name not in kwargs
                ]
            except (TypeError, ValueError):
                missing_required = []  # can't introspect — assume it's fine

            if missing_required:
                log.debug(f"Skipping '{cap.name}' from keyword fallback — "
                          f"missing required args {missing_required} we can't infer from '{goal}'.")
                continue

            return AutomationPlan(goal=goal, steps=[
                PlanStepSpec(f"{cap.description or cap.name}", cap.name, kwargs)
            ])
        return AutomationPlan(goal=goal, steps=[])

    def _is_destructive(self, capability_name: str) -> bool:
        cap = registry.get(capability_name)
        return bool(cap and cap.destructive)

    def _check_confirmation(self, code: Optional[str]) -> tuple:
        """Delegates to the same store actions/confirmation.py uses, so a
        code set via the existing 'set_confirmation_code' tool works here
        too. Fails closed: any import/lookup error blocks execution."""
        try:
            from actions.confirmation import verify_confirmation_code
        except Exception as exc:
            return False, f"confirmation system unavailable: {exc}"
        if not code:
            return False, "confirmation code required"
        result = verify_confirmation_code(code)
        ok = not str(result).upper().startswith("ERROR")
        return ok, result

    # ── execution ─────────────────────────────────────────────────────────
    def run(self, goal: str, confirmation_code: Optional[str] = None) -> ExecutionReport:
        plan = self.build_plan(goal)
        if not plan.steps:
            log.info(f"AutomationEngine: no capability matched goal '{goal}'")
            return ExecutionReport(goal=goal, ok=False, summary="No matching automation found")

        # Destructive steps (shutdown/restart/sleep/hibernate/lock) must pass
        # through the SAME confirmation-code gate as actions/computer_settings.py
        # — this package must never become a side-door around it.
        destructive_steps = [s for s in plan.steps if self._is_destructive(s.capability)]
        if destructive_steps:
            gate_ok, gate_msg = self._check_confirmation(confirmation_code)
            if not gate_ok:
                names = ", ".join(s.description for s in destructive_steps)
                return ExecutionReport(goal=goal, ok=False,
                                        summary=f"Blocked ({gate_msg}): {names} require(s) a confirmation code")

        stop_flag = threading.Event()

        def _progress_pinger():
            """Publishes 'AutomationProgress' if the plan runs past ~1s,
            per the 'never leave long silence' interaction requirement."""
            waited = 0.0
            while not stop_flag.wait(1.0):
                waited += 1.0
                if event_bus is not None:
                    event_bus.publish("AutomationProgress", goal=goal, elapsed_s=waited)

        pinger = threading.Thread(target=_progress_pinger, daemon=True)
        pinger.start()
        try:
            report = execute_plan(plan)
        finally:
            stop_flag.set()
        return report

    def describe_capabilities(self) -> dict:
        """For debugging/UI — grouped capability list."""
        out: dict = {}
        for cap in registry.all():
            module = cap.name.split(".", 1)[0]
            out.setdefault(module, []).append(cap.name)
        return out


# Process-wide singleton.
automation_engine = AutomationEngine()


def run_goal(goal: str, confirmation_code: Optional[str] = None) -> str:
    """Plain-function entry point for main.py's `_lazy_import` dispatcher
    (which calls the imported attribute directly rather than an object's
    method). Returns a string, matching every other actions/* module's
    convention so it can be spoken/logged the same way."""
    report = automation_engine.run(goal, confirmation_code=confirmation_code)
    return report.summary
