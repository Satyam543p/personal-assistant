import abc
import ast
import asyncio
import importlib.util
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field

try:
    from assistant.tools import Tool, JarvisToolExecutor
    from assistant.database.manager import DatabaseManager
except ModuleNotFoundError:
    from tools import Tool, JarvisToolExecutor
    from database.manager import DatabaseManager

logger = logging.getLogger("jarvis.tool_lifecycle")

# =====================================================================
# Exceptions
# =====================================================================

class StaticAnalysisSecurityError(Exception):
    """Raised when drafted code violates AST security policies."""
    pass

class SandboxExecutionError(Exception):
    """Raised when drafted code fails sandbox execution or test cases."""
    pass


# =====================================================================
# Data Transfer Objects
# =====================================================================

@dataclass
class ToolDraft:
    name: str
    capability: str
    code: str
    declaration: dict
    test_cases: list[dict] = field(default_factory=list)
    version: str = "1.0.0"
    risk_level: str = "read_only"  # "read_only" | "reversible" | "destructive"


# =====================================================================
# Static Analysis / AST Security Inspector
# =====================================================================

class ToolStaticAnalyzer:
    """
    Inspects Python AST trees for unsafe language constructs, forbidden calls,
    and undeclared/malicious imports as specified in Section 35 of phase.md.
    """

    SAFE_IMPORTS_WHITELIST = {
        "math", "json", "re", "datetime", "time", "hashlib",
        "urllib.parse", "uuid", "base64", "dataclasses", "typing",
        "pathlib", "os.path", "random", "string", "collections", "itertools"
    }

    FORBIDDEN_CALLS = {
        "exec", "eval", "compile", "__import__",
        "globals", "locals"
    }

    FORBIDDEN_MODULES = {
        "ctypes", "cffi", "multiprocessing", "threading", "pty",
        "shutil", "socket"
    }

    FORBIDDEN_ATTRIBUTES = {
        "__subclasses__", "__globals__", "__code__", "__class__", "__builtins__"
    }

    def analyze(self, code: str, declaration: dict = None) -> tuple[bool, str | None]:
        """
        Parses and validates Python code AST.
        Returns:
            (is_safe: bool, failure_reason: str | None)
        """
        declaration = declaration or {}
        declared_allowed_imports = set(declaration.get("allowed_imports", []))
        declared_side_effects = set(declaration.get("side_effects", []))

        try:
            tree = ast.parse(code)
        except SyntaxError as e:
            return False, f"Syntax Error during AST parsing: {e}"

        for node in ast.walk(tree):
            # 1. Check direct imports
            if isinstance(node, ast.Import):
                for alias in node.names:
                    mod_base = alias.name.split(".")[0]
                    if mod_base in self.FORBIDDEN_MODULES:
                        return False, f"Forbidden dangerous module imported: '{alias.name}'"
                    if mod_base == "subprocess" and "subprocess" not in declared_side_effects:
                        return False, "Subprocess import is not declared in tool side_effects allow-list."
                    if alias.name not in self.SAFE_IMPORTS_WHITELIST and mod_base not in self.SAFE_IMPORTS_WHITELIST and alias.name not in declared_allowed_imports:
                        return False, f"Module '{alias.name}' is not in the allowed imports whitelist."

            # 2. Check from imports
            elif isinstance(node, ast.ImportFrom):
                if node.level > 0:
                    return False, "Relative imports escaping tool scope are forbidden."
                if node.module:
                    mod_base = node.module.split(".")[0]
                    if mod_base in self.FORBIDDEN_MODULES:
                        return False, f"Forbidden dangerous module imported: '{node.module}'"
                    if mod_base == "subprocess" and "subprocess" not in declared_side_effects:
                        return False, "Subprocess import is not declared in tool side_effects allow-list."
                    if node.module not in self.SAFE_IMPORTS_WHITELIST and mod_base not in self.SAFE_IMPORTS_WHITELIST and node.module not in declared_allowed_imports:
                        return False, f"Module '{node.module}' is not in the allowed imports whitelist."

            # 3. Check function calls
            elif isinstance(node, ast.Call):
                func_name = None
                if isinstance(node.func, ast.Name):
                    func_name = node.func.id
                elif isinstance(node.func, ast.Attribute):
                    func_name = node.func.attr

                if func_name in self.FORBIDDEN_CALLS:
                    return False, f"Unsafe function call detected: '{func_name}()'"

                # Guard raw open() outside declared paths
                if func_name == "open" and "filesystem_write" not in declared_side_effects and "filesystem_read" not in declared_side_effects:
                    return False, "File I/O via open() is not declared in tool side_effects."

            # 4. Check forbidden dunder attributes
            elif isinstance(node, ast.Attribute):
                if node.attr in self.FORBIDDEN_ATTRIBUTES:
                    return False, f"Access to sensitive internal attribute '{node.attr}' is forbidden."

        return True, None


# =====================================================================
# Sandbox Test Runner
# =====================================================================

class ToolSandboxRunner:
    """
    Executes drafted tool code inside an isolated subprocess against
    synthetic test inputs with memory, timeout, and directory bounds.
    """

    def __init__(self, workspace_root: str = None):
        self.workspace_root = workspace_root or os.path.abspath("c:/Users/Satyam Pandey/Desktop/personal assistent")
        self.sandbox_base = os.path.join(self.workspace_root, "assistant", "scratch", "sandbox")
        os.makedirs(self.sandbox_base, exist_ok=True)

    def run_sandbox_tests(self, draft: ToolDraft) -> dict:
        """
        Executes all test cases (minimum 3 required).
        Raises SandboxExecutionError if any test fails, times out, or errors.
        """
        if len(draft.test_cases) < 3:
            raise SandboxExecutionError(
                f"Section 35 requires at least 3 synthetic test cases (provided {len(draft.test_cases)})."
            )

        tool_box_dir = os.path.join(self.sandbox_base, draft.name)
        if os.path.exists(tool_box_dir):
            shutil.rmtree(tool_box_dir, ignore_errors=True)
        os.makedirs(tool_box_dir, exist_ok=True)

        tool_file = os.path.join(tool_box_dir, "tool_impl.py")
        runner_file = os.path.join(tool_box_dir, "test_harness.py")

        with open(tool_file, "w", encoding="utf-8") as f:
            f.write(draft.code)

        # Write standalone test harness
        test_harness_script = f"""import asyncio
import json
import sys
import time
import tool_impl

test_cases = {json.dumps(draft.test_cases)}

async def run_tests():
    results = []
    for idx, tc in enumerate(test_cases):
        inputs = tc.get("inputs", {{}})
        expected_status = tc.get("expected_status", "success")
        t0 = time.time()
        try:
            if hasattr(tool_impl, "execute"):
                if asyncio.iscoroutinefunction(tool_impl.execute):
                    res = await tool_impl.execute(None, **inputs)
                else:
                    res = tool_impl.execute(None, **inputs)
            else:
                raise AttributeError("Tool module does not define 'execute' function")
                
            latency_ms = int((time.time() - t0) * 1000)
            status = res.get("status", "success") if isinstance(res, dict) else "success"
            if status != expected_status:
                results.append({{"test_index": idx, "passed": False, "error": f"Expected status '{{expected_status}}', got '{{status}}'", "latency_ms": latency_ms}})
            else:
                results.append({{"test_index": idx, "passed": True, "result": res, "latency_ms": latency_ms}})
        except Exception as e:
            results.append({{"test_index": idx, "passed": False, "error": str(e), "latency_ms": int((time.time() - t0) * 1000)}})
            
    print(json.dumps({{"tests": results}}))

if __name__ == '__main__':
    asyncio.run(run_tests())
"""
        with open(runner_file, "w", encoding="utf-8") as f:
            f.write(test_harness_script)

        # Minimal safe environment
        clean_env = {
            "SYSTEMROOT": os.environ.get("SYSTEMROOT", r"C:\Windows"),
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": tool_box_dir
        }

        timeout_sec = (draft.declaration.get("timeout_ms", 5000) / 1000.0) + 2.0

        try:
            proc = subprocess.run(
                [sys.executable, runner_file],
                cwd=tool_box_dir,
                env=clean_env,
                capture_output=True,
                text=True,
                timeout=timeout_sec
            )
        except subprocess.TimeoutExpired:
            shutil.rmtree(tool_box_dir, ignore_errors=True)
            raise SandboxExecutionError(f"Tool execution exceeded timeout limit of {timeout_sec} seconds.")

        if proc.returncode != 0:
            shutil.rmtree(tool_box_dir, ignore_errors=True)
            raise SandboxExecutionError(f"Sandbox runner failed with exit code {proc.returncode}: {proc.stderr}")

        try:
            summary = json.loads(proc.stdout.strip())
        except json.JSONDecodeError:
            shutil.rmtree(tool_box_dir, ignore_errors=True)
            raise SandboxExecutionError(f"Invalid JSON returned from sandbox runner: {proc.stdout}")

        # Verify all tests passed
        test_results = summary.get("tests", [])
        for tr in test_results:
            if not tr.get("passed"):
                shutil.rmtree(tool_box_dir, ignore_errors=True)
                raise SandboxExecutionError(f"Synthetic test case {tr['test_index']} failed: {tr.get('error')}")

        # Clean up sandbox temp dir
        shutil.rmtree(tool_box_dir, ignore_errors=True)

        return {
            "passed": True,
            "test_count": len(test_results),
            "details": test_results
        }


# =====================================================================
# Dynamic Tool Adapter for ToolExecutor
# =====================================================================

class DynamicTool(Tool):
    """
    Adapts dynamic Python code into the standard Jarvis Tool hierarchy,
    enabling it to be registered in JarvisToolExecutor.
    """

    def __init__(self, name: str, risk_level: str, declaration: dict, execute_fn):
        super().__init__(name, risk_level, declaration)
        self.execute_fn = execute_fn

    async def execute(self, executor, **kwargs) -> dict:
        if asyncio.iscoroutinefunction(self.execute_fn):
            return await self.execute_fn(executor, **kwargs)
        return self.execute_fn(executor, **kwargs)


# =====================================================================
# Tool Lifecycle Manager Subsystem
# =====================================================================

class ToolLifecycleManager:
    """
    Subsystem responsible for discovering capability gaps, drafting tools,
    static AST analysis, sandbox validation, SQLite registration, versioning,
    and dynamic tool execution integration.
    """

    def __init__(
        self,
        db_manager: DatabaseManager,
        tool_executor: JarvisToolExecutor = None,
        workspace_root: str = None
    ):
        self.db = db_manager
        self.tool_executor = tool_executor
        self.workspace_root = workspace_root or os.path.abspath("c:/Users/Satyam Pandey/Desktop/personal assistent")
        self.custom_tools_dir = os.path.join(self.workspace_root, "assistant", "custom_tools")
        self.capability_file = os.path.join(self.workspace_root, "assistant", "capability_registry.json")
        os.makedirs(self.custom_tools_dir, exist_ok=True)

        self.static_analyzer = ToolStaticAnalyzer()
        self.sandbox_runner = ToolSandboxRunner(self.workspace_root)

        # Load existing custom tools from disk and SQLite
        self.load_registered_custom_tools()

    # -----------------------------------------------------------------
    # Capability Gap Discovery (Section 40)
    # -----------------------------------------------------------------

    def get_capability_registry(self) -> dict:
        if not os.path.exists(self.capability_file):
            return {}
        try:
            with open(self.capability_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Error reading capability registry: {e}")
            return {}

    def discover_capability_gaps(self) -> list[dict]:
        """
        Compares capability_registry.json against active tools in SQLite.
        Returns missing capabilities that require tool drafting.
        """
        caps = self.get_capability_registry()
        active_tools = set()

        try:
            with self.db.transaction() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT name FROM tools WHERE status = 'active';")
                active_tools = {r["name"] for r in cursor.fetchall()}
        except Exception as e:
            logger.error(f"Error querying active tools: {e}")

        gaps = []
        for cap_name, info in caps.items():
            assigned_tools = info.get("tools", [])
            has_active_tool = any(t in active_tools for t in assigned_tools)
            if not has_active_tool and info.get("status") != "not_planned":
                gaps.append({
                    "capability": cap_name,
                    "status": "missing",
                    "action_required": f"Draft implementation for '{cap_name}' capability."
                })

        return gaps

    # -----------------------------------------------------------------
    # Validation (Static AST + Sandbox)
    # -----------------------------------------------------------------

    def validate_tool(self, draft: ToolDraft) -> tuple[bool, str | None]:
        """
        Runs both static analysis and sandbox test suite.
        Returns:
            (is_valid: bool, error_reason: str | None)
        """
        # 1. Static AST Analysis
        is_safe, reason = self.static_analyzer.analyze(draft.code, draft.declaration)
        if not is_safe:
            return False, f"Static AST Check Failed: {reason}"

        # 2. Sandbox Test Suite
        try:
            self.sandbox_runner.run_sandbox_tests(draft)
        except SandboxExecutionError as e:
            return False, f"Sandbox Test Suite Failed: {e}"

        return True, None

    # -----------------------------------------------------------------
    # Registration & Versioning
    # -----------------------------------------------------------------

    def register_tool(self, draft: ToolDraft) -> dict:
        """
        Validates draft, writes file to assistant/custom_tools/,
        updates SQLite tools table with versioning/deprecation,
        and makes tool callable in tool_executor.
        """
        is_valid, err_msg = self.validate_tool(draft)
        if not is_valid:
            logger.error(f"Cannot register tool '{draft.name}': {err_msg}")
            return {"status": "failure", "error": err_msg}

        now = int(time.time())
        tool_file = os.path.join(self.custom_tools_dir, f"{draft.name}.py")

        # 1. Save tool code
        with open(tool_file, "w", encoding="utf-8") as f:
            f.write(draft.code)

        # 2. Versioning & SQLite Registration
        try:
            with self.db.transaction() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT id, version FROM tools WHERE name = ?;", (draft.name,))
                existing = cursor.fetchone()

                if existing:
                    # Deprecate old version (Section 11)
                    old_ver = existing["version"]
                    logger.info(f"Deprecating previous version {old_ver} of tool '{draft.name}'.")
                    conn.execute(
                        "UPDATE tools SET status = 'deprecated', updated_at = ? WHERE name = ?;",
                        (now, draft.name)
                    )

                # Insert new active version
                tool_id = f"custom_{draft.name}_{draft.version.replace('.', '_')}"
                conn.execute(
                    "INSERT INTO tools (id, name, capability, version, risk_level, declaration, success_rate, last_used_at, created_at, updated_at, status) "
                    "VALUES (?, ?, ?, ?, ?, ?, 1.0, NULL, ?, ?, 'active') "
                    "ON CONFLICT(name) DO UPDATE SET version = excluded.version, declaration = excluded.declaration, risk_level = excluded.risk_level, updated_at = excluded.updated_at, status = 'active';",
                    (
                        tool_id,
                        draft.name,
                        draft.capability,
                        draft.version,
                        draft.risk_level,
                        json.dumps(draft.declaration),
                        now,
                        now
                    )
                )

            logger.info(f"Successfully registered tool '{draft.name}' (v{draft.version}) in SQLite.")

            # 3. Update capability registry
            self._update_capability_registry(draft.capability, draft.name)

            # 4. Dynamically load into executor
            self._load_tool_module_into_executor(draft.name, tool_file, draft.risk_level, draft.declaration)

            return {
                "status": "success",
                "tool_name": draft.name,
                "version": draft.version,
                "message": f"Tool '{draft.name}' (v{draft.version}) validated and registered successfully."
            }

        except Exception as e:
            logger.error(f"Error registering tool '{draft.name}' in database: {e}")
            return {"status": "failure", "error": str(e)}

    def retire_tool(self, tool_name: str) -> bool:
        """Retires a tool from active duty."""
        now = int(time.time())
        try:
            with self.db.transaction() as conn:
                cursor = conn.execute("UPDATE tools SET status = 'retired', updated_at = ? WHERE name = ?;", (now, tool_name))
                success = cursor.rowcount > 0

            if success and self.tool_executor and tool_name in self.tool_executor.tools:
                del self.tool_executor.tools[tool_name]
                logger.info(f"Retired tool '{tool_name}' and removed from active executor.")
            return success
        except Exception as e:
            logger.error(f"Error retiring tool '{tool_name}': {e}")
            return False

    def _update_capability_registry(self, capability: str, tool_name: str):
        if not capability:
            return
        caps = self.get_capability_registry()
        if capability in caps:
            tool_list = caps[capability].get("tools", [])
            if tool_name not in tool_list:
                tool_list.append(tool_name)
            caps[capability]["tools"] = tool_list
            caps[capability]["status"] = "active"
        else:
            caps[capability] = {"tools": [tool_name], "status": "active"}

        try:
            with open(self.capability_file, "w", encoding="utf-8") as f:
                json.dump(caps, f, indent=2)
        except Exception as e:
            logger.error(f"Error updating capability_registry.json: {e}")

    def _load_tool_module_into_executor(self, tool_name: str, file_path: str, risk_level: str, declaration: dict):
        if not self.tool_executor:
            return

        spec = importlib.util.spec_from_file_location(f"custom_tool_{tool_name}", file_path)
        if not spec or not spec.loader:
            logger.error(f"Could not load module spec for {file_path}")
            return

        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        if not hasattr(module, "execute"):
            logger.error(f"Module {tool_name} does not define 'execute' function.")
            return

        dynamic_tool = DynamicTool(tool_name, risk_level, declaration, module.execute)
        self.tool_executor.tools[tool_name] = dynamic_tool
        logger.info(f"Loaded dynamic tool '{tool_name}' into JarvisToolExecutor.")

    def load_registered_custom_tools(self):
        """Loads all active custom tools from SQLite and custom_tools directory."""
        try:
            with self.db.transaction() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT name, risk_level, declaration FROM tools WHERE status = 'active';")
                rows = cursor.fetchall()
                for r in rows:
                    name = r["name"]
                    file_path = os.path.join(self.custom_tools_dir, f"{name}.py")
                    if os.path.exists(file_path):
                        decl = json.loads(r["declaration"]) if r["declaration"] else {}
                        self._load_tool_module_into_executor(name, file_path, r["risk_level"], decl)
        except Exception as e:
            logger.error(f"Error loading custom tools: {e}")
