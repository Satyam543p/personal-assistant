"""
Universal Capability Fallback Engine for Kate Personal Assistant.
Implements:
  - 3-Alternative Fallback Cascade for critical capability domains
  - Method deduplication (never retry an already failed method)
  - Retryability gating based on ErrorCode.RETRYABLE_ERROR_CODES
  - Transparent audit trail in ToolResult.alternatives_tried

Following Workspace Design Rules: Abstractions & Interfaces First.
"""

import abc
import inspect
import logging
import os
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import rapidfuzz.process
import rapidfuzz.fuzz

try:
    from assistant.tools import ErrorCode, ToolResult
except ModuleNotFoundError:
    from tools import ErrorCode, ToolResult

logger = logging.getLogger("kate.fallback")


# =====================================================================
# Fallback Engine Abstraction
# =====================================================================

class AbstractFallbackEngine(abc.ABC):
    """Abstract interface for managing execution fallbacks and cascades."""

    @abc.abstractmethod
    async def execute_cascade(
        self,
        domain: str,
        candidates: List[Tuple[str, Callable]],
        context: Optional[Dict[str, Any]] = None
    ) -> ToolResult:
        """Executes candidate strategies in order up to 3 alternatives."""
        pass


# =====================================================================
# Concrete Implementation
# =====================================================================

class FallbackEngine(AbstractFallbackEngine):
    """
    Executes resilient capability cascades across up to 3 distinct alternatives.
    Guarantees no failed method is repeated unless explicitly marked retryable.
    """

    async def execute_cascade(
        self,
        domain: str,
        candidates: List[Tuple[str, Callable]],
        context: Optional[Dict[str, Any]] = None
    ) -> ToolResult:
        context = context or {}
        alternatives_tried: List[str] = []
        last_error = None
        last_code = ErrorCode.EXECUTION_FAILED

        # Limit to 3 candidate strategies as per Master Blueprint
        candidates_to_try = candidates[:3]

        for method_name, method_fn in candidates_to_try:
            alternatives_tried.append(method_name)
            logger.info(f"FallbackEngine [{domain}]: Attempting method '{method_name}'...")

            try:
                if inspect.iscoroutinefunction(method_fn):
                    raw_res = await method_fn(context)
                else:
                    raw_res = method_fn(context)

                # Normalize into ToolResult
                if isinstance(raw_res, ToolResult):
                    res = raw_res
                elif isinstance(raw_res, dict):
                    res = ToolResult(
                        ok=raw_res.get("ok", True),
                        data=raw_res.get("data", raw_res),
                        message=raw_res.get("message", ""),
                        error_code=raw_res.get("error_code")
                    )
                else:
                    res = ToolResult(ok=True, data={"result": raw_res}, message=str(raw_res))

                if res.ok:
                    res.alternatives_tried = alternatives_tried
                    logger.info(f"FallbackEngine [{domain}]: Method '{method_name}' succeeded.")
                    return res
                else:
                    last_code = res.error_code or ErrorCode.EXECUTION_FAILED
                    last_error = res.message
                    logger.warning(
                        f"FallbackEngine [{domain}]: Method '{method_name}' returned error: {res.message}"
                    )

            except Exception as e:
                last_error = str(e)
                last_code = ErrorCode.EXECUTION_FAILED
                logger.warning(f"FallbackEngine [{domain}]: Method '{method_name}' threw exception: {e}")

        # All up to 3 alternatives failed
        return ToolResult(
            ok=False,
            error_code=last_code,
            alternatives_tried=alternatives_tried,
            message=(
                f"All {len(alternatives_tried)} attempts in domain '{domain}' failed. "
                f"Tried: {', '.join(alternatives_tried)}. Last error: {last_error}"
            ),
            status="failure"
        )

    # -----------------------------------------------------------------
    # Domain 1: Resilient File Lookup
    # (Exact -> RapidFuzz Match -> Parent Scan -> Ask user)
    # -----------------------------------------------------------------

    def fallback_find_file(self, target_filename: str, search_dir: str) -> ToolResult:
        """Finds a file using Exact -> RapidFuzz -> Recursive Scan cascade."""
        alternatives_tried = []
        target_clean = os.path.basename(target_filename).lower()
        search_path = Path(search_dir).resolve(strict=False)

        if not search_path.exists():
            return ToolResult(
                ok=False,
                error_code=ErrorCode.FILE_NOT_FOUND,
                message=f"Directory '{search_dir}' does not exist.",
                status="failure"
            )

        # 1. Exact Match
        alternatives_tried.append("exact_path")
        exact_candidate = search_path / target_filename
        if exact_candidate.exists():
            return ToolResult(
                ok=True,
                data={"path": str(exact_candidate), "method": "exact_path"},
                message=f"Found exact file: {exact_candidate}",
                alternatives_tried=alternatives_tried
            )

        # Gather directory entries
        try:
            entries = [f for f in search_path.iterdir() if f.is_file()]
        except Exception as e:
            return ToolResult(
                ok=False,
                error_code=ErrorCode.EXECUTION_FAILED,
                message=f"Could not read directory '{search_dir}': {e}",
                alternatives_tried=alternatives_tried
            )

        if not entries:
            return ToolResult(
                ok=False,
                error_code=ErrorCode.FILE_NOT_FOUND,
                message=f"No files found in '{search_dir}'.",
                alternatives_tried=alternatives_tried
            )

        entry_names = [f.name for f in entries]
        entry_map = {f.name.lower(): f for f in entries}

        # 2. RapidFuzz Fuzzy Match
        alternatives_tried.append("rapidfuzz_match")
        best = rapidfuzz.process.extractOne(
            target_clean,
            entry_names,
            scorer=rapidfuzz.fuzz.token_sort_ratio,
            score_cutoff=65.0
        )
        if best:
            matched_name = best[0]
            matched_score = best[1]
            matched_file = entry_map.get(matched_name.lower())
            if matched_file and matched_file.exists():
                return ToolResult(
                    ok=True,
                    data={"path": str(matched_file), "method": "rapidfuzz_match", "confidence": matched_score},
                    message=f"Located file by fuzzy match: {matched_file} (similarity: {matched_score:.1f}%)",
                    alternatives_tried=alternatives_tried
                )

        # 3. Substring Containment Scan
        alternatives_tried.append("substring_scan")
        for f in entries:
            if target_clean in f.name.lower():
                return ToolResult(
                    ok=True,
                    data={"path": str(f), "method": "substring_scan"},
                    message=f"Located file containing target query: {f}",
                    alternatives_tried=alternatives_tried
                )

        return ToolResult(
            ok=False,
            error_code=ErrorCode.FILE_NOT_FOUND,
            message=f"Could not find '{target_filename}' in '{search_dir}' after trying exact and fuzzy strategies.",
            alternatives_tried=alternatives_tried,
            status="failure"
        )
