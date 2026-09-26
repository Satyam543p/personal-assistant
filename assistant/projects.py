import abc
import json
import time
import logging
import difflib
from dataclasses import dataclass

logger = logging.getLogger("jarvis.projects")

# =====================================================================
# Exceptions
# =====================================================================

class AmbiguousProjectError(Exception):
    """Raised when fuzzy project resolution yields multiple matching candidates with identical confidence."""
    def __init__(self, candidates):
        self.candidates = candidates
        super().__init__(f"Ambiguous project resolution: matches {candidates}")

# =====================================================================
# Data Transfer Object
# =====================================================================

@dataclass
class ProjectMetadata:
    id: str
    name: str
    aliases: list[str]
    folder_path: str
    preferred_editor: str | None = None
    run_command: str | None = None
    tags: list[str] | None = None
    description: str | None = None
    related_notes: str | None = None
    project_type: str | None = None
    git_repo_info: dict | None = None
    last_opened_at: int | None = None
    last_modified_at: int | None = None
    is_pinned: bool = False
    is_archived: bool = False
    active_workspace_id: str | None = None
    metadata: dict | None = None
    created_at: int = 0
    updated_at: int = 0

# =====================================================================
# Interfaces
# =====================================================================

class ProjectRegistry(abc.ABC):
    @abc.abstractmethod
    def resolve_project(self, project_name: str) -> ProjectMetadata | None:
        """
        Fuzzy matches a project name or alias.
        Returns resolved ProjectMetadata, or None if unresolvable.
        Raises AmbiguousProjectError if multiple candidates tie.
        """
        pass

    @abc.abstractmethod
    def get_project(self, project_id: str) -> ProjectMetadata | None:
        """Retrieve project by unique ID."""
        pass

    @abc.abstractmethod
    def list_projects(self, include_archived: bool = False) -> list[ProjectMetadata]:
        """List all projects in the registry."""
        pass

    @abc.abstractmethod
    def add_project(self, project: ProjectMetadata) -> bool:
        """Add a new project to the registry."""
        pass

    @abc.abstractmethod
    def update_project(self, project_id: str, updates: dict) -> bool:
        """Update metadata fields for a specific project."""
        pass

    @abc.abstractmethod
    def delete_project(self, project_id: str) -> bool:
        """Delete a project from the registry."""
        pass

# =====================================================================
# SQLite Implementation
# =====================================================================

class SQLiteProjectRegistry(ProjectRegistry):
    def __init__(self, db_manager):
        self.db = db_manager

    def _row_to_project(self, row) -> ProjectMetadata:
        # Helper to safely parse JSON arrays/dictionaries
        def parse_json_list(col):
            if not col:
                return []
            try:
                val = json.loads(col)
                return val if isinstance(val, list) else []
            except json.JSONDecodeError:
                return []

        def parse_json_dict(col):
            if not col:
                return {}
            try:
                val = json.loads(col)
                return val if isinstance(val, dict) else {}
            except json.JSONDecodeError:
                return {}

        return ProjectMetadata(
            id=row["id"],
            name=row["name"],
            aliases=parse_json_list(row["aliases"]),
            folder_path=row["folder_path"],
            preferred_editor=row["preferred_editor"],
            run_command=row["run_command"],
            tags=parse_json_list(row["tags"]),
            description=row["description"],
            related_notes=row["related_notes"],
            project_type=row["project_type"],
            git_repo_info=parse_json_dict(row["git_repo_info"]),
            last_opened_at=row["last_opened_at"],
            last_modified_at=row["last_modified_at"],
            is_pinned=bool(row["is_pinned"]),
            is_archived=bool(row["is_archived"]),
            active_workspace_id=row["active_workspace_id"],
            metadata=parse_json_dict(row["metadata"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"]
        )

    def get_project(self, project_id: str) -> ProjectMetadata | None:
        try:
            with self.db.transaction() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT id, name, aliases, folder_path, preferred_editor, run_command, tags, description, "
                    "related_notes, project_type, git_repo_info, last_opened_at, last_modified_at, is_pinned, "
                    "is_archived, active_workspace_id, metadata, created_at, updated_at FROM projects WHERE id = ?;",
                    (project_id,)
                )
                row = cursor.fetchone()
                return self._row_to_project(row) if row else None
        except Exception as e:
            logger.error(f"Error fetching project '{project_id}': {e}")
            return None

    def list_projects(self, include_archived: bool = False) -> list[ProjectMetadata]:
        try:
            with self.db.transaction() as conn:
                cursor = conn.cursor()
                if include_archived:
                    cursor.execute("SELECT * FROM projects ORDER BY is_pinned DESC, last_opened_at DESC;")
                else:
                    cursor.execute("SELECT * FROM projects WHERE is_archived = 0 ORDER BY is_pinned DESC, last_opened_at DESC;")
                rows = cursor.fetchall()
                return [self._row_to_project(row) for row in rows]
        except Exception as e:
            logger.error(f"Error listing projects: {e}")
            return []

    def add_project(self, project: ProjectMetadata) -> bool:
        try:
            now = int(time.time())
            with self.db.transaction() as conn:
                conn.execute(
                    "INSERT INTO projects (id, name, aliases, folder_path, preferred_editor, run_command, tags, "
                    "description, related_notes, project_type, git_repo_info, last_opened_at, last_modified_at, "
                    "is_pinned, is_archived, active_workspace_id, metadata, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);",
                    (
                        project.id,
                        project.name,
                        json.dumps(project.aliases or []),
                        project.folder_path,
                        project.preferred_editor,
                        project.run_command,
                        json.dumps(project.tags or []),
                        project.description,
                        project.related_notes,
                        project.project_type,
                        json.dumps(project.git_repo_info or {}),
                        project.last_opened_at or now,
                        project.last_modified_at,
                        1 if project.is_pinned else 0,
                        1 if project.is_archived else 0,
                        project.active_workspace_id,
                        json.dumps(project.metadata or {}),
                        project.created_at or now,
                        project.updated_at or now
                    )
                )
            logger.info(f"Successfully added project '{project.name}' (ID: {project.id})")
            return True
        except Exception as e:
            logger.error(f"Failed to add project '{project.name}': {e}")
            return False

    def update_project(self, project_id: str, updates: dict) -> bool:
        if not updates:
            return True
        
        # Whitelist of allowed columns for safety
        allowed_fields = {
            "name", "aliases", "folder_path", "preferred_editor", "run_command", "tags",
            "description", "related_notes", "project_type", "git_repo_info", "last_opened_at",
            "last_modified_at", "is_pinned", "is_archived", "active_workspace_id", "metadata"
        }
        
        set_clauses = []
        params = []
        
        for k, v in updates.items():
            if k not in allowed_fields:
                continue
            
            set_clauses.append(f"{k} = ?")
            # Serialize JSON columns
            if k in ("aliases", "tags", "git_repo_info", "metadata"):
                params.append(json.dumps(v))
            elif k in ("is_pinned", "is_archived"):
                params.append(1 if v else 0)
            else:
                params.append(v)
                
        if not set_clauses:
            return True
            
        set_clauses.append("updated_at = ?")
        params.append(int(time.time()))
        params.append(project_id)
        
        query = f"UPDATE projects SET {', '.join(set_clauses)} WHERE id = ?;"
        
        try:
            with self.db.transaction() as conn:
                cursor = conn.execute(query, tuple(params))
                success = cursor.rowcount > 0
            if success:
                logger.info(f"Updated project '{project_id}' fields: {list(updates.keys())}")
            return success
        except Exception as e:
            logger.error(f"Failed updating project '{project_id}': {e}")
            return False

    def delete_project(self, project_id: str) -> bool:
        try:
            with self.db.transaction() as conn:
                cursor = conn.execute("DELETE FROM projects WHERE id = ?;", (project_id,))
                success = cursor.rowcount > 0
            if success:
                logger.info(f"Deleted project '{project_id}' from registry.")
            return success
        except Exception as e:
            logger.error(f"Failed deleting project '{project_id}': {e}")
            return False

    # =====================================================================
    # Name and Alias Resolution Heuristics
    # =====================================================================
    def resolve_project(self, project_name: str) -> ProjectMetadata | None:
        if not project_name:
            return None
            
        query_str = project_name.lower().strip()
        projects = self.list_projects(include_archived=False)
        if not projects:
            return None

        candidates = []  # list of tuple (ProjectMetadata, confidence)

        for proj in projects:
            # 1. Exact Canonical Match or ID Match (confidence = 1.0)
            if proj.name.lower().strip() == query_str or proj.id.lower().strip() == query_str:
                logger.info(f"Registry resolved exact match for '{project_name}' -> '{proj.name}' ({proj.id})")
                return proj

            # 2. Exact Alias Match (confidence = 0.95)
            if any(alias.lower().strip() == query_str for alias in proj.aliases):
                logger.info(f"Registry resolved exact alias match for '{project_name}' -> alias of '{proj.name}'")
                candidates.append((proj, 0.95))
                continue

            # Substring / Containment Match (confidence = 0.88)
            if (query_str in proj.name.lower() or query_str in proj.id.lower() or
                proj.name.lower() in query_str or proj.id.lower() in query_str):
                candidates.append((proj, 0.88))
                continue

            # 3. Fuzzy Canonical Name Match (ratio * 0.90)
            c_score = difflib.SequenceMatcher(None, proj.name.lower(), query_str).ratio()
            if c_score >= 0.70:
                candidates.append((proj, round(c_score * 0.90, 4)))

            # 4. Fuzzy Alias Match (ratio * 0.85)
            for alias in proj.aliases:
                a_score = difflib.SequenceMatcher(None, alias.lower(), query_str).ratio()
                if a_score >= 0.70:
                    candidates.append((proj, round(a_score * 0.85, 4)))

        if not candidates:
            return None

        # Filter duplicates: keep the highest score for each project ID
        deduped = {}
        for proj, score in candidates:
            if proj.id not in deduped or score > deduped[proj.id][1]:
                deduped[proj.id] = (proj, score)
                
        sorted_candidates = sorted(deduped.values(), key=lambda x: x[1], reverse=True)
        
        # If there's only one candidate, return it
        if len(sorted_candidates) == 1:
            best_proj, best_score = sorted_candidates[0]
            logger.info(f"Fuzzy resolved project '{best_proj.name}' with score {best_score}")
            return best_proj
            
        # Tie-breaker logic for multiple candidates
        best_proj, best_score = sorted_candidates[0]
        second_proj, second_score = sorted_candidates[1]
        
        if best_score > second_score:
            logger.info(f"Fuzzy resolved top project '{best_proj.name}' with score {best_score} (runner up: {second_score})")
            return best_proj
            
        # We have a score tie. Apply tie-breaker:
        # "prefer the one with the more recent last_opened_at. If still tied, present both."
        # Find all candidates tied with the best score
        tied_candidates = [proj for proj, score in sorted_candidates if score == best_score]
        
        # Sort tied candidates by last_opened_at descending (recent first, treat None as 0)
        def get_last_opened(p):
            return p.last_opened_at if p.last_opened_at is not None else 0
            
        tied_sorted = sorted(tied_candidates, key=get_last_opened, reverse=True)
        
        opened_first = get_last_opened(tied_sorted[0])
        opened_second = get_last_opened(tied_sorted[1])
        
        if opened_first > opened_second:
            logger.info(f"Tied score resolved by last_opened_at recency -> '{tied_sorted[0].name}'")
            return tied_sorted[0]
            
        # Still tied. Raise AmbiguousProjectError so the router can construct options.
        logger.warning(f"Ambiguity tie unresolved. Tied projects: {[p.name for p in tied_candidates]}")
        raise AmbiguousProjectError(tied_candidates)
