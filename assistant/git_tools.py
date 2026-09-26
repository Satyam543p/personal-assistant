"""
Git & Repository Intelligence Subsystem for Jarvis.
Provides inspection of Git repository branches, dirty working trees,
diff summaries, and commit workflows, safeguarding project switches.
"""

import abc
import logging
import os
import subprocess

logger = logging.getLogger("jarvis.git")

try:
    from assistant.tools import Tool
except ModuleNotFoundError:
    from tools import Tool


class GitService(abc.ABC):
    @abc.abstractmethod
    def get_status(self, repo_path: str) -> dict:
        """Returns branch, dirty status, staged, and unstaged files."""
        pass

    @abc.abstractmethod
    def get_diff_summary(self, repo_path: str, staged_only: bool = False) -> str:
        """Returns concise diff summary of modifications."""
        pass

    @abc.abstractmethod
    def commit_changes(self, repo_path: str, message: str) -> dict:
        """Stages tracked modifications and creates a git commit."""
        pass


class LocalGitService(GitService):
    def _run_git(self, repo_path: str, args: list[str]) -> tuple[int, str, str]:
        cmd = ["git"] + args
        try:
            proc = subprocess.run(
                cmd,
                cwd=repo_path,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="ignore",
                timeout=10
            )
            return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
        except FileNotFoundError:
            return -1, "", "git executable not found on system PATH."
        except subprocess.TimeoutExpired:
            return -1, "", "git command timed out."
        except Exception as e:
            return -1, "", str(e)

    def is_git_repo(self, repo_path: str) -> bool:
        if not os.path.isdir(repo_path):
            return False
        code, stdout, _ = self._run_git(repo_path, ["rev-parse", "--is-inside-work-tree"])
        return code == 0 and stdout == "true"

    def get_status(self, repo_path: str) -> dict:
        if not self.is_git_repo(repo_path):
            return {
                "is_git": False,
                "repo_path": repo_path,
                "message": "Not a valid Git repository."
            }

        # Query branch
        _, branch, _ = self._run_git(repo_path, ["rev-parse", "--abbrev-ref", "HEAD"])
        if not branch:
            branch = "unknown"

        # Query porcelain status
        code, status_out, err = self._run_git(repo_path, ["status", "--porcelain=v1"])
        
        staged = []
        unstaged = []
        untracked = []

        if code == 0 and status_out:
            for line in status_out.splitlines():
                if len(line) < 3:
                    continue
                index_stat = line[0]
                work_stat = line[1]
                filepath = line[3:].strip()

                if index_stat in ("M", "A", "D", "R"):
                    staged.append(filepath)
                if work_stat in ("M", "D"):
                    unstaged.append(filepath)
                elif index_stat == "?" and work_stat == "?":
                    untracked.append(filepath)

        is_dirty = len(staged) > 0 or len(unstaged) > 0

        return {
            "is_git": True,
            "repo_path": repo_path,
            "branch": branch,
            "is_dirty": is_dirty,
            "staged_count": len(staged),
            "unstaged_count": len(unstaged),
            "untracked_count": len(untracked),
            "staged_files": staged[:20],
            "unstaged_files": unstaged[:20],
            "untracked_files": untracked[:20]
        }

    def get_diff_summary(self, repo_path: str, staged_only: bool = False) -> str:
        if not self.is_git_repo(repo_path):
            return "Not a valid Git repository."

        args = ["diff", "--stat"]
        if staged_only:
            args = ["diff", "--cached", "--stat"]

        code, stdout, err = self._run_git(repo_path, args)
        if code != 0:
            return f"Git diff error: {err}"
        return stdout if stdout else "No modifications detected."

    def commit_changes(self, repo_path: str, message: str) -> dict:
        if not self.is_git_repo(repo_path):
            raise ValueError(f"Path '{repo_path}' is not a valid Git repository.")

        if not message.strip():
            raise ValueError("Commit message cannot be empty.")

        # Stage tracked changes
        add_code, _, add_err = self._run_git(repo_path, ["add", "-u"])
        if add_code != 0:
            raise RuntimeError(f"git add -u failed: {add_err}")

        # Commit
        commit_code, commit_out, commit_err = self._run_git(repo_path, ["commit", "-m", message])
        if commit_code != 0:
            raise RuntimeError(f"git commit failed: {commit_err or commit_out}")

        return {
            "status": "success",
            "message": message,
            "output": commit_out
        }


class GitStatusTool(Tool):
    def __init__(self, git_service: GitService = None):
        declaration = {
            "inputs": {
                "repo_path": {"type": "string", "default": "."}
            },
            "side_effects": "Inspects git branch, staged/unstaged changes, and untracked files",
            "timeout_ms": 5000,
            "memory_limit_mb": 50
        }
        super().__init__("git_status", "read_only", declaration)
        self.git = git_service or LocalGitService()

    async def execute(self, executor, **kwargs) -> dict:
        repo_path = kwargs.get("repo_path") or kwargs.get("folder_path") or "."
        if repo_path == ".":
            repo_path = os.getcwd()

        status_info = self.git.get_status(repo_path)
        if not status_info.get("is_git"):
            return {
                "status": "warning",
                "git_status": status_info,
                "response": f"Path '{repo_path}' is not a git repository."
            }

        branch = status_info["branch"]
        dirty_str = "DIRTY (uncommitted changes present)" if status_info["is_dirty"] else "CLEAN"
        msg = f"Git Repo: {branch} ({dirty_str}) | Staged: {status_info['staged_count']} | Unstaged: {status_info['unstaged_count']} | Untracked: {status_info['untracked_count']}"

        return {
            "status": "success",
            "git_status": status_info,
            "response": msg
        }


class GitDiffTool(Tool):
    def __init__(self, git_service: GitService = None):
        declaration = {
            "inputs": {
                "repo_path": {"type": "string", "default": "."},
                "staged_only": {"type": "boolean", "default": False}
            },
            "side_effects": "Summarizes code modifications via git diff stat",
            "timeout_ms": 5000,
            "memory_limit_mb": 50
        }
        super().__init__("git_diff_summary", "read_only", declaration)
        self.git = git_service or LocalGitService()

    async def execute(self, executor, **kwargs) -> dict:
        repo_path = kwargs.get("repo_path") or kwargs.get("folder_path") or "."
        if repo_path == ".":
            repo_path = os.getcwd()
        staged_only = kwargs.get("staged_only", False)

        diff_summary = self.git.get_diff_summary(repo_path, staged_only=staged_only)
        return {
            "status": "success",
            "diff_summary": diff_summary,
            "response": f"Git diff summary:\n{diff_summary}"
        }


class GitCommitTool(Tool):
    def __init__(self, git_service: GitService = None):
        declaration = {
            "inputs": {
                "repo_path": {"type": "string", "default": "."},
                "message": {"type": "string"}
            },
            "side_effects": "Stages tracked changes and creates a Git commit",
            "timeout_ms": 10000,
            "memory_limit_mb": 50
        }
        super().__init__("git_commit", "destructive", declaration)
        self.git = git_service or LocalGitService()

    async def execute(self, executor, **kwargs) -> dict:
        repo_path = kwargs.get("repo_path") or kwargs.get("folder_path") or "."
        if repo_path == ".":
            repo_path = os.getcwd()
        message = kwargs.get("message") or kwargs.get("commit_message", "")

        if not message:
            raise ValueError("Commit message parameter 'message' is required.")

        result = self.git.commit_changes(repo_path, message)
        return {
            "status": "success",
            "commit_result": result,
            "response": f"Successfully created Git commit: '{message}'"
        }
