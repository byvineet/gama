"""
actions/dynamic_tool_creator.py — Gama Dynamic Tool Creation & Self-Extension Engine
========================================================================================
Enables Gama to dynamically author, validate, load, and register new tool modules
at runtime without requiring a restart.

Author: Vineet Machchal / Antigravity
"""

import ast
import importlib
import importlib.util
import logging
import os
import sys
import types
from typing import Any, Dict, Optional

logger = logging.getLogger("gama.actions.dynamic_tool_creator")

_CUSTOM_TOOLS_DIR = os.path.join(os.path.dirname(__file__), "custom_tools")
os.makedirs(_CUSTOM_TOOLS_DIR, exist_ok=True)

# Register custom tools directory in sys.path if not present
if _CUSTOM_TOOLS_DIR not in sys.path:
    sys.path.insert(0, _CUSTOM_TOOLS_DIR)

_DYNAMIC_REGISTRY: Dict[str, Any] = {}


def validate_python_code(code: str) -> bool:
    """Validate that code string is valid Python syntax and contains no obvious syntax errors."""
    try:
        ast.parse(code)
        return True
    except SyntaxError as e:
        logger.error(f"Syntax validation failed for dynamic tool: {e}")
        return False


def create_dynamic_tool(tool_name: str, code: str, overwrite: bool = True) -> str:
    """Create, write, validate, and dynamically load a new Python tool module.

    Args:
        tool_name: Snake_case filename identifier for the tool (e.g. 'custom_calc').
        code: Python source code containing the tool implementation.
        overwrite: Whether to overwrite existing custom tool file.

    Returns:
        Confirmation message with status.
    """
    clean_name = "".join(c if c.isalnum() or c == "_" else "_" for c in tool_name).lower()
    if not clean_name.endswith(".py"):
        filename = f"{clean_name}.py"
        module_name = clean_name
    else:
        filename = clean_name
        module_name = clean_name[:-3]

    filepath = os.path.join(_CUSTOM_TOOLS_DIR, filename)

    if not overwrite and os.path.exists(filepath):
        return f"Tool file {filename} already exists and overwrite is set to False."

    # Validate AST
    if not validate_python_code(code):
        return f"Failed to create tool '{module_name}': Code contains Python syntax errors."

    # Save source file
    try:
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(code)
    except Exception as e:
        logger.error(f"Error saving dynamic tool file: {e}")
        return f"Failed to write tool file: {e}"

    # Load module dynamically
    load_res = load_dynamic_tool(module_name)
    if load_res:
        return f"Successfully created and loaded dynamic tool '{module_name}'. It is now ready for use!"
    else:
        return f"Tool file created at '{filename}', but failed to import dynamically."


def load_dynamic_tool(module_name: str) -> Optional[types.ModuleType]:
    """Dynamically import or reload a custom tool module from the custom_tools directory."""
    filepath = os.path.join(_CUSTOM_TOOLS_DIR, f"{module_name}.py")
    if not os.path.exists(filepath):
        logger.warning(f"Dynamic tool file not found: {filepath}")
        return None

    try:
        spec = importlib.util.spec_from_file_location(f"actions.custom_tools.{module_name}", filepath)
        if spec is None or spec.loader is None:
            return None

        mod = importlib.util.module_from_spec(spec)
        sys.modules[f"actions.custom_tools.{module_name}"] = mod
        spec.loader.exec_module(mod)

        _DYNAMIC_REGISTRY[module_name] = mod
        logger.info(f"Dynamically loaded custom tool module: {module_name}")
        return mod
    except Exception as e:
        logger.error(f"Error loading dynamic tool module '{module_name}': {e}", exc_info=True)
        return None


def execute_dynamic_tool(tool_name: str, function_name: Optional[str] = None, **kwargs) -> Any:
    """Execute a function inside a dynamically loaded tool module."""
    mod = _DYNAMIC_REGISTRY.get(tool_name) or load_dynamic_tool(tool_name)
    if mod is None:
        return f"Dynamic tool '{tool_name}' is not loaded."

    fn_to_call = function_name or tool_name
    if hasattr(mod, fn_to_call):
        fn = getattr(mod, fn_to_call)
        try:
            return fn(**kwargs)
        except Exception as e:
            logger.error(f"Execution error in dynamic tool '{tool_name}.{fn_to_call}': {e}")
            return f"Error executing dynamic tool '{tool_name}': {e}"
    elif hasattr(mod, "run"):
        try:
            return mod.run(**kwargs)
        except Exception as e:
            return f"Error executing dynamic tool '{tool_name}.run': {e}"
    else:
        return f"Dynamic tool '{tool_name}' has no entry point '{fn_to_call}' or 'run()'."


def list_dynamic_tools() -> Dict[str, str]:
    """List all registered dynamic self-extended tools and their status."""
    res = {}
    if not os.path.exists(_CUSTOM_TOOLS_DIR):
        return res

    for item in os.listdir(_CUSTOM_TOOLS_DIR):
        if item.endswith(".py") and not item.startswith("__"):
            mod_name = item[:-3]
            is_loaded = mod_name in _DYNAMIC_REGISTRY
            res[mod_name] = "Loaded & Active" if is_loaded else "Available"
    return res


def dynamic_tool_creator(action: str = "list", **kwargs) -> Any:
    """Tool entrypoint for Gama dynamic tool management.

    Actions:
      - 'create': Requires `name` and `code`.
      - 'list': List all dynamically created tools.
      - 'execute': Execute a dynamic tool. Requires `name` and optional parameters.
    """
    action = (action or "list").lower().strip()
    if action == "list":
        tools = list_dynamic_tools()
        if not tools:
            return "No dynamic self-extension tools created yet."
        formatted = "\n".join(f"- {name}: {status}" for name, status in tools.items())
        return f"Dynamic Self-Extended Tools:\n{formatted}"

    if action == "create":
        name = kwargs.get("name") or kwargs.get("tool_name")
        code = kwargs.get("code") or kwargs.get("script")
        if not name or not code:
            return "Error: 'create' requires 'name' and 'code' parameters."
        return create_dynamic_tool(name, code, overwrite=kwargs.get("overwrite", True))

    if action == "execute":
        name = kwargs.get("name") or kwargs.get("tool_name")
        if not name:
            return "Error: 'execute' requires 'name' parameter."
        fn_name = kwargs.get("function_name")
        args = kwargs.get("args", {})
        return execute_dynamic_tool(name, function_name=fn_name, **args)

    return "Unknown action. Supported: list, create, execute."


__all__ = ["dynamic_tool_creator", "create_dynamic_tool", "load_dynamic_tool", "execute_dynamic_tool", "list_dynamic_tools"]
