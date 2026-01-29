import json
import logging
from typing import Annotated, Optional, List, Literal, Dict, Any

from pydantic import Field
from fastmcp import FastMCP, Context
from .core.config import Config
from .logging.global_logging import setup_logging
from .logging.session_logging import log_tool_call
from .core.connection import P4ConnectionManager

from .services.file_services import FileServices
from .services.server_services import ServerServices
from .services.shelve_services import ShelveServices
from .services.workspace_services import WorkspaceServices
from .services.changelist_services import ChangelistServices
from .services.job_services import JobServices

from .middleware.check_permission import CheckPermissionMiddleware

logger = logging.getLogger(__name__)

class P4MCPServer:
    """Perforce MCP Server with improved structure"""

    def __init__(self, session_id: str = None, readonly: bool = True, toolsets: list = []):
        self.readonly = readonly
        self.toolsets = toolsets
        self.session_id = session_id

        setup_logging()
        self.p4config = Config.load()
        self.p4_manager = P4ConnectionManager(self.p4config)

        if self.readonly:
            logger.info("Running in read-only mode. No write operations will be allowed.")
        else:
            logger.info("Running in read-write mode. Write operations are enabled.")
    
        logger.info(f"Enabled toolsets: {', '.join(self.toolsets) if self.toolsets else 'None'}")

        self.mcp = FastMCP("P4 MCP Server", middleware=[CheckPermissionMiddleware(self.p4_manager)])
        self._initialize_dependencies()
    
    def _initialize_dependencies(self) -> None:
        """Initialize all dependencies with proper error handling"""
        try:
            self._initialize_services()
            self._register_tools()
        except Exception as e:
            logger.error(f"Failed to initialize dependencies: {e}")
            raise

    def _initialize_services(self) -> None:
        """Initialize services used by tools"""
        self.server_services = ServerServices(self.p4_manager)
        self.workspace_services = WorkspaceServices(self.p4_manager)
        self.file_services = FileServices(self.p4_manager)
        self.changelist_services = ChangelistServices(self.p4_manager)
        self.shelve_services = ShelveServices(self.p4_manager)
        self.job_services = JobServices(self.p4_manager)

    def _apply_toolset_visibility(self) -> None:
        """Disable tools based on toolsets and readonly mode."""
        all_toolsets = {"files", "changelists", "shelves", "workspaces", "jobs"}
        disabled_tags = all_toolsets - set(self.toolsets)
        if disabled_tags:
            self.mcp.disable(tags=disabled_tags, components={"tool"})
        if self.readonly:
            self.mcp.disable(tags={"write"}, components={"tool"})
        if len(set(self.toolsets) - {"jobs"}) == 0:
            self.mcp.disable(names={"execute_delete"}, components={"tool"})

    def process_tool_logs(self, tool_name: str, result: dict, ctx: Context) -> dict:
        """Process incoming data and route to appropriate handler"""
        response = {}
        response['mcp_client'] = ctx.session.client_params.clientInfo.name if ctx and ctx.session and ctx.session.client_params else "Unknown"
        response['toolset'] = tool_name.split('_')[1] if '_' in tool_name else "unknown"
        response['tool_name'] = tool_name
        response['tool_action'] = result.get('action', 'unknown')
        response['status'] = result.get('status', 'unknown')
        response['p4_version'] = getattr(self.p4config, 'p4version', 'Unknown')

        logger.info('tool_call: %s', json.dumps(response))

        if self.session_id:
            log_tool_call(response, session_id=self.session_id)

    @staticmethod
    def requires_approval(tool_name: str, action: str, params: Dict[str, Any]) -> dict:
        """Handle dangerous operations that require user approval"""
        toolset = tool_name.split("_", 1)[-1]
        logger.warning(f"Approval required for dangerous operation: {action} on {toolset}")
        # Return a special response that forces MCP client to ask for approval
        return {
            "_meta": {
                "requires_approval": True,
                "approval_type": "dangerous_operation"
            },
            "type": "approval_required",
            "operation": tool_name,
            "message": (
                f"⚠️ DANGER: This will permanently delete the {toolset}. "
                "This action cannot be undone. Next operation should not be executed until the user approves."
            ),
            "details": {
                "source_tool": tool_name,
                "action": action,
                "params": params
            },
            "instruction": "User must explicitly approve this operation",
            "on_approval": "execute_delete"
        }

    def _register_tools(self):
        """Register read-only tools (always available)"""
        def _build_workspace_spec(
            name: str,
            root: Optional[str],
            description: Optional[str],
            options: Optional[str],
            line_end: Optional[str],
            view: Optional[List[str]],
        ) -> Dict[str, Any]:
            spec: Dict[str, Any] = {"Name": name}
            if root is not None:
                spec["Root"] = root
            if description is not None:
                spec["Description"] = description
            if options is not None:
                spec["Options"] = options
            if line_end is not None:
                spec["LineEnd"] = line_end
            if view is not None:
                spec["View"] = view
            return spec

        @self.mcp.tool(tags=["read", "server"])
        async def query_server(
            action: Annotated[
                Literal["server_info", "current_user"],
                Field(description="Select server information or current user details.")
            ],
            ctx: Context
        ) -> dict:
            """Get server info or current user info (READ permission)"""
            if action == "server_info":
                result = await self.server_services.get_server_info()
            else:
                result = await self.server_services.get_current_user()
            response = {"status": result.get("status"), "action": action, "data": result}
            self.process_tool_logs("query_server", response, ctx)
            return response

        @self.mcp.tool(tags=["read", "workspaces"])
        async def get_workspace(
            workspace_name: Annotated[
                str,
                Field(description="Workspace name to inspect.")
            ],
            detail: Annotated[
                Literal["spec", "type", "status"],
                Field(description="Which workspace detail to return.")
            ] = "spec",
            ctx: Context = None
        ) -> dict:
            """Get workspace spec, type, or status (READ permission)"""
            if detail == "spec":
                result = await self.workspace_services.get_workspace(workspace_name)
                action = "get"
            elif detail == "type":
                result = await self.workspace_services.get_workspace_type(workspace_name)
                action = "type"
            else:
                result = await self.workspace_services.get_workspace_status(workspace_name)
                action = "status"
            response = {"status": result.get("status"), "action": action, "data": result}
            self.process_tool_logs("get_workspace", response, ctx)
            return response

        @self.mcp.tool(tags=["read", "workspaces"])
        async def list_workspaces(
            user: Annotated[
                Optional[str],
                Field(description="Filter by user name.")
            ] = None,
            max_results: Annotated[
                int,
                Field(description="Maximum number of workspaces to return.", ge=1, le=1000)
            ] = 100,
            ctx: Context = None
        ) -> dict:
            """List available workspaces (READ permission)"""
            result = await self.workspace_services.list_workspaces(user or "", max_results)
            response = {"status": result.get("status"), "action": "list", "data": result}
            self.process_tool_logs("list_workspaces", response, ctx)
            return response

        @self.mcp.tool(tags=["read", "files"])
        async def get_file_content(
            file_path: Annotated[
                str,
                Field(description="Depot or local file path to read.")
            ],
            ctx: Context = None
        ) -> dict:
            """Get file content (READ permission)"""
            result = await self.file_services.get_file_content(file_path)
            response = {"status": result.get("status"), "action": "content", "data": result}
            self.process_tool_logs("get_file_content", response, ctx)
            return response

        @self.mcp.tool(tags=["read", "files"])
        async def get_file_history(
            file_path: Annotated[
                str,
                Field(description="Depot or local file path to inspect history.")
            ],
            max_results: Annotated[
                int,
                Field(description="Maximum number of history entries.", ge=1, le=1000)
            ] = 100,
            ctx: Context = None
        ) -> dict:
            """Get file history (READ permission)"""
            result = await self.file_services.get_file_history(file_path, max_results)
            response = {"status": result.get("status"), "action": "history", "data": result}
            self.process_tool_logs("get_file_history", response, ctx)
            return response

        @self.mcp.tool(tags=["read", "files"])
        async def get_file_info(
            file_path: Annotated[
                str,
                Field(description="Depot or local file path to inspect.")
            ],
            ctx: Context = None
        ) -> dict:
            """Get file info (READ permission)"""
            result = await self.file_services.get_file_info(file_path)
            response = {"status": result.get("status"), "action": "info", "data": result}
            self.process_tool_logs("get_file_info", response, ctx)
            return response

        @self.mcp.tool(tags=["read", "files"])
        async def get_file_metadata(
            file_path: Annotated[
                str,
                Field(description="Depot or local file path to inspect metadata.")
            ],
            ctx: Context = None
        ) -> dict:
            """Get file metadata (READ permission)"""
            result = await self.file_services.get_file_metadata(file_path)
            response = {"status": result.get("status"), "action": "metadata", "data": result}
            self.process_tool_logs("get_file_metadata", response, ctx)
            return response

        @self.mcp.tool(tags=["read", "files"])
        async def diff_files(
            file_path: Annotated[
                str,
                Field(description="First depot or local file path.")
            ],
            file2: Annotated[
                str,
                Field(description="Second depot or local file path.")
            ],
            diff2: Annotated[
                bool,
                Field(description="Use p4 diff2 for depot-to-depot diff.")
            ] = True,
            ctx: Context = None
        ) -> dict:
            """Diff two files (READ permission)"""
            result = await self.file_services.diff_files(file_path, file2, diff2)
            response = {"status": result.get("status"), "action": "diff", "data": result}
            self.process_tool_logs("diff_files", response, ctx)
            return response

        @self.mcp.tool(tags=["read", "files"])
        async def get_file_annotations(
            file_path: Annotated[
                str,
                Field(description="Depot or local file path to annotate.")
            ],
            ctx: Context = None
        ) -> dict:
            """Get file annotations (READ permission)"""
            result = await self.file_services.get_file_annotations(file_path)
            response = {"status": result.get("status"), "action": "annotations", "data": result}
            self.process_tool_logs("get_file_annotations", response, ctx)
            return response

        @self.mcp.tool(tags=["read", "changelists"])
        async def get_changelist(
            changelist_id: Annotated[
                str,
                Field(description="Changelist ID. Use 'default' for default changelist.")
            ],
            ctx: Context = None
        ) -> dict:
            """Get changelist details (READ permission)"""
            result = await self.changelist_services.get_changelist(changelist_id)
            response = {"status": result.get("status"), "action": "get", "message": result.get("message")}
            self.process_tool_logs("get_changelist", response, ctx)
            return response

        @self.mcp.tool(tags=["read", "changelists"])
        async def list_changelists(
            workspace_name: Annotated[
                Optional[str],
                Field(description="Filter by workspace name.")
            ] = None,
            status: Annotated[
                Optional[Literal["pending", "submitted"]],
                Field(description="Filter by changelist status.")
            ] = None,
            user: Annotated[
                Optional[str],
                Field(description="Filter by user name.")
            ] = None,
            depot_path: Annotated[
                Optional[str],
                Field(description="Filter by depot path.")
            ] = None,
            max_results: Annotated[
                int,
                Field(description="Maximum number of changelists to return.", ge=1, le=1000)
            ] = 100,
            ctx: Context = None
        ) -> dict:
            """List changelists (READ permission)"""
            result = await self.changelist_services.list_changelists(
                workspace_name,
                status,
                user,
                depot_path,
                max_results
            )
            response = {"status": result.get("status"), "action": "list", "message": result.get("message")}
            self.process_tool_logs("list_changelists", response, ctx)
            return response

        @self.mcp.tool(tags=["read", "shelves"])
        async def list_shelves(
            user: Annotated[
                Optional[str],
                Field(description="Filter by user name.")
            ] = None,
            max_results: Annotated[
                int,
                Field(description="Maximum number of shelves to return.", ge=1, le=1000)
            ] = 50,
            ctx: Context = None
        ) -> dict:
            """List shelves (READ permission)"""
            result = await self.shelve_services.list_shelves(user or "", max_results)
            response = {"status": result.get("status"), "action": "list", "message": result.get("message")}
            self.process_tool_logs("list_shelves", response, ctx)
            return response

        @self.mcp.tool(tags=["read", "shelves"])
        async def get_shelve_diff(
            changelist_id: Annotated[
                str,
                Field(description="Changelist ID to diff.")
            ],
            ctx: Context = None
        ) -> dict:
            """Get shelve diff (READ permission)"""
            result = await self.shelve_services.get_shelve_diff(changelist_id)
            response = {"status": result.get("status"), "action": "diff", "message": result.get("message")}
            self.process_tool_logs("get_shelve_diff", response, ctx)
            return response

        @self.mcp.tool(tags=["read", "shelves"])
        async def get_shelve_files(
            changelist_id: Annotated[
                str,
                Field(description="Changelist ID to list shelved files.")
            ],
            ctx: Context = None
        ) -> dict:
            """Get shelved files (READ permission)"""
            result = await self.shelve_services.get_shelve_files(changelist_id)
            response = {"status": result.get("status"), "action": "files", "message": result.get("message")}
            self.process_tool_logs("get_shelve_files", response, ctx)
            return response

        @self.mcp.tool(tags=["read", "jobs"])
        async def list_jobs(
            changelist_id: Annotated[
                str,
                Field(description="Changelist ID to list jobs from.")
            ],
            max_results: Annotated[
                int,
                Field(description="Maximum number of jobs to return.", ge=1, le=1000)
            ] = 50,
            ctx: Context = None
        ) -> dict:
            """List jobs from changelist (READ permission)"""
            result = await self.job_services.list_jobs_from_changelist(changelist_id, max_results)
            response = {"status": result.get("status"), "action": "list_jobs", "message": result.get("message")}
            self.process_tool_logs("list_jobs", response, ctx)
            return response

        @self.mcp.tool(tags=["read", "jobs"])
        async def get_job(
            job_id: Annotated[
                str,
                Field(description="Job ID to retrieve.")
            ],
            ctx: Context = None
        ) -> dict:
            """Get job details (READ permission)"""
            result = await self.job_services.get_job_details(job_id)
            response = {"status": result.get("status"), "action": "get_job", "message": result.get("message")}
            self.process_tool_logs("get_job", response, ctx)
            return response

        @self.mcp.tool(tags=["write", "workspaces"])
        async def create_workspace(
            name: Annotated[str, Field(description="Workspace name.")],
            root: Annotated[Optional[str], Field(description="Workspace root path.")] = None,
            description: Annotated[Optional[str], Field(description="Workspace description.")] = None,
            options: Annotated[Optional[str], Field(description="Workspace options string.")] = None,
            line_end: Annotated[Optional[str], Field(description="Line ending style.")] = None,
            view: Annotated[Optional[List[str]], Field(description="View mappings for the workspace.")] = None,
            ctx: Context = None
        ) -> dict:
            """Create a workspace (WRITE permission)"""
            spec = _build_workspace_spec(name, root, description, options, line_end, view)
            result = await self.workspace_services.create_workspace(spec)
            response = {"status": result.get("status"), "action": "create", "message": result.get("message")}
            self.process_tool_logs("create_workspace", response, ctx)
            return response

        @self.mcp.tool(tags=["write", "workspaces"])
        async def update_workspace(
            name: Annotated[str, Field(description="Workspace name.")],
            root: Annotated[Optional[str], Field(description="Workspace root path.")] = None,
            description: Annotated[Optional[str], Field(description="Workspace description.")] = None,
            options: Annotated[Optional[str], Field(description="Workspace options string.")] = None,
            line_end: Annotated[Optional[str], Field(description="Line ending style.")] = None,
            view: Annotated[Optional[List[str]], Field(description="View mappings for the workspace.")] = None,
            ctx: Context = None
        ) -> dict:
            """Update a workspace (WRITE permission)"""
            spec = _build_workspace_spec(name, root, description, options, line_end, view)
            result = await self.workspace_services.update_workspace(name, spec)
            response = {"status": result.get("status"), "action": "update", "message": result.get("message")}
            self.process_tool_logs("update_workspace", response, ctx)
            return response

        @self.mcp.tool(tags=["write", "workspaces"])
        async def delete_workspace(
            workspace_name: Annotated[str, Field(description="Workspace name to delete.")],
            ctx: Context = None
        ) -> dict:
            """Delete a workspace (WRITE permission)"""
            result = {"status": "warning", "action": "delete", "message": "Requires approval to delete workspace."}
            self.process_tool_logs("delete_workspace", result, ctx)
            return self.requires_approval(
                "delete_workspace",
                "delete",
                {"workspace_name": workspace_name}
            )

        @self.mcp.tool(tags=["write", "workspaces"])
        async def switch_workspace(
            workspace_name: Annotated[str, Field(description="Workspace name to switch to.")],
            ctx: Context = None
        ) -> dict:
            """Switch active workspace (WRITE permission)"""
            result = await self.workspace_services.switch_workspace(workspace_name)
            response = {"status": result.get("status"), "action": "switch", "message": result.get("message")}
            self.process_tool_logs("switch_workspace", response, ctx)
            return response

        @self.mcp.tool(tags=["write", "files"])
        async def add_files(
            file_paths: Annotated[List[str], Field(description="File paths to add.")],
            changelist: Annotated[str, Field(description="Changelist ID or 'default'.")] = "default",
            ctx: Context = None
        ) -> dict:
            """Add files (WRITE permission)"""
            result = await self.file_services.add_files(file_paths, changelist)
            response = {"status": result.get("status"), "action": "add", "message": result.get("message")}
            self.process_tool_logs("add_files", response, ctx)
            return response

        @self.mcp.tool(tags=["write", "files"])
        async def edit_files(
            file_paths: Annotated[List[str], Field(description="File paths to edit.")],
            changelist: Annotated[str, Field(description="Changelist ID or 'default'.")] = "default",
            ctx: Context = None
        ) -> dict:
            """Open files for edit (WRITE permission)"""
            result = await self.file_services.edit_files(file_paths, changelist)
            response = {"status": result.get("status"), "action": "edit", "message": result.get("message")}
            self.process_tool_logs("edit_files", response, ctx)
            return response

        @self.mcp.tool(tags=["write", "files"])
        async def move_files(
            source_paths: Annotated[List[str], Field(description="Source file paths to move.")],
            target_paths: Annotated[List[str], Field(description="Target file paths to move to.")],
            changelist: Annotated[str, Field(description="Changelist ID or 'default'.")] = "default",
            ctx: Context = None
        ) -> dict:
            """Move/rename files (WRITE permission)"""
            result = await self.file_services.move_files(source_paths, target_paths, changelist)
            response = {"status": result.get("status"), "action": "move", "message": result.get("message")}
            self.process_tool_logs("move_files", response, ctx)
            return response

        @self.mcp.tool(tags=["write", "files"])
        async def delete_files(
            file_paths: Annotated[List[str], Field(description="File paths to delete.")],
            changelist: Annotated[str, Field(description="Changelist ID or 'default'.")] = "default",
            ctx: Context = None
        ) -> dict:
            """Delete files (WRITE permission)"""
            result = {"status": "warning", "action": "delete", "message": "Requires approval to delete files."}
            self.process_tool_logs("delete_files", result, ctx)
            return self.requires_approval(
                "delete_files",
                "delete",
                {"file_paths": file_paths, "changelist": changelist}
            )

        @self.mcp.tool(tags=["write", "files"])
        async def revert_files(
            file_paths: Annotated[List[str], Field(description="File paths to revert.")],
            changelist: Annotated[str, Field(description="Changelist ID or 'default'.")] = "default",
            ctx: Context = None
        ) -> dict:
            """Revert files (WRITE permission)"""
            result = await self.file_services.revert_files(file_paths, changelist)
            response = {"status": result.get("status"), "action": "revert", "message": result.get("message")}
            self.process_tool_logs("revert_files", response, ctx)
            return response

        @self.mcp.tool(tags=["write", "files"])
        async def reconcile_files(
            file_paths: Annotated[Optional[List[str]], Field(description="File paths to reconcile.")] = None,
            changelist: Annotated[str, Field(description="Changelist ID or 'default'.")] = "default",
            ctx: Context = None
        ) -> dict:
            """Reconcile files (WRITE permission)"""
            result = await self.file_services.reconcile_files(file_paths or [], changelist)
            response = {"status": result.get("status"), "action": "reconcile", "message": result.get("message")}
            self.process_tool_logs("reconcile_files", response, ctx)
            return response

        @self.mcp.tool(tags=["write", "files"])
        async def resolve_files(
            file_paths: Annotated[Optional[List[str]], Field(description="File paths to resolve.")] = None,
            changelist: Annotated[str, Field(description="Changelist ID or 'default'.")] = "default",
            mode: Annotated[
                Optional[Literal["auto", "safe", "force", "preview", "theirs", "yours"]],
                Field(description="Resolve mode for conflicts.")
            ] = "auto",
            ctx: Context = None
        ) -> dict:
            """Resolve files (WRITE permission)"""
            result = await self.file_services.resolve_files(file_paths or [], changelist, mode)
            response = {"status": result.get("status"), "action": "resolve", "message": result.get("message")}
            self.process_tool_logs("resolve_files", response, ctx)
            return response

        @self.mcp.tool(tags=["write", "files"])
        async def sync_files(
            file_paths: Annotated[List[str], Field(description="File paths to sync.")],
            force: Annotated[bool, Field(description="Force sync even if files are up-to-date.")] = False,
            ctx: Context = None
        ) -> dict:
            """Sync files (WRITE permission)"""
            result = await self.file_services.sync_files(file_paths, force)
            response = {"status": result.get("status"), "action": "sync", "message": result.get("message")}
            self.process_tool_logs("sync_files", response, ctx)
            return response

        @self.mcp.tool(tags=["write", "changelists"])
        async def create_changelist(
            description: Annotated[str, Field(description="Changelist description.")],
            ctx: Context = None
        ) -> dict:
            """Create a changelist (WRITE permission)"""
            result = await self.changelist_services.create_changelist(description)
            response = {"status": result.get("status"), "action": "create", "message": result.get("message")}
            self.process_tool_logs("create_changelist", response, ctx)
            return response

        @self.mcp.tool(tags=["write", "changelists"])
        async def update_changelist(
            changelist_id: Annotated[str, Field(description="Changelist ID to update.")],
            description: Annotated[str, Field(description="Updated changelist description.")],
            ctx: Context = None
        ) -> dict:
            """Update a changelist (WRITE permission)"""
            result = await self.changelist_services.update_changelist(changelist_id, description)
            response = {"status": result.get("status"), "action": "update", "message": result.get("message")}
            self.process_tool_logs("update_changelist", response, ctx)
            return response

        @self.mcp.tool(tags=["write", "changelists"])
        async def submit_changelist(
            changelist_id: Annotated[str, Field(description="Changelist ID to submit.")],
            ctx: Context = None
        ) -> dict:
            """Submit a changelist (WRITE permission)"""
            result = await self.changelist_services.submit_changelist(changelist_id)
            response = {"status": result.get("status"), "action": "submit", "message": result.get("message")}
            self.process_tool_logs("submit_changelist", response, ctx)
            return response

        @self.mcp.tool(tags=["write", "changelists"])
        async def delete_changelist(
            changelist_id: Annotated[str, Field(description="Changelist ID to delete.")],
            ctx: Context = None
        ) -> dict:
            """Delete a changelist (WRITE permission)"""
            result = {"status": "warning", "action": "delete", "message": "Requires approval to delete changelist."}
            self.process_tool_logs("delete_changelist", result, ctx)
            return self.requires_approval(
                "delete_changelist",
                "delete",
                {"changelist_id": changelist_id}
            )

        @self.mcp.tool(tags=["write", "changelists"])
        async def reopen_files(
            changelist_id: Annotated[str, Field(description="Target changelist ID.")],
            file_paths: Annotated[List[str], Field(description="File paths to move to the changelist.")],
            ctx: Context = None
        ) -> dict:
            """Move files to a changelist (WRITE permission)"""
            result = await self.changelist_services.move_files_to_changelist(changelist_id, file_paths)
            response = {"status": result.get("status"), "action": "move_files", "message": result.get("message")}
            self.process_tool_logs("reopen_files", response, ctx)
            return response

        @self.mcp.tool(tags=["write", "shelves"])
        async def shelve_files(
            changelist_id: Annotated[str, Field(description="Changelist ID to shelve.")],
            file_paths: Annotated[List[str], Field(description="File paths to shelve.")],
            force: Annotated[bool, Field(description="Force shelve operation.")] = False,
            ctx: Context = None
        ) -> dict:
            """Shelve files (WRITE permission)"""
            result = await self.shelve_services.shelve_files(changelist_id, file_paths, force)
            response = {"status": result.get("status"), "action": "shelve", "message": result.get("message")}
            self.process_tool_logs("shelve_files", response, ctx)
            return response

        @self.mcp.tool(tags=["write", "shelves"])
        async def unshelve_files(
            changelist_id: Annotated[str, Field(description="Shelved changelist ID to unshelve.")],
            file_paths: Annotated[Optional[List[str]], Field(description="File paths to unshelve.")] = None,
            force: Annotated[bool, Field(description="Force unshelve operation.")] = False,
            ctx: Context = None
        ) -> dict:
            """Unshelve files (WRITE permission)"""
            result = await self.shelve_services.unshelve_files(changelist_id, file_paths or [], force)
            response = {"status": result.get("status"), "action": "unshelve", "message": result.get("message")}
            self.process_tool_logs("unshelve_files", response, ctx)
            return response

        @self.mcp.tool(tags=["write", "shelves"])
        async def update_shelve(
            changelist_id: Annotated[str, Field(description="Shelved changelist ID to update.")],
            file_paths: Annotated[List[str], Field(description="File paths to update in the shelve.")],
            force: Annotated[bool, Field(description="Force update operation.")] = False,
            ctx: Context = None
        ) -> dict:
            """Update a shelved changelist (WRITE permission)"""
            result = await self.shelve_services.update_shelve(changelist_id, file_paths, force)
            response = {"status": result.get("status"), "action": "update", "message": result.get("message")}
            self.process_tool_logs("update_shelve", response, ctx)
            return response

        @self.mcp.tool(tags=["write", "shelves"])
        async def delete_shelve(
            changelist_id: Annotated[str, Field(description="Shelved changelist ID to delete.")],
            file_paths: Annotated[Optional[List[str]], Field(description="File paths to delete from the shelve.")] = None,
            ctx: Context = None
        ) -> dict:
            """Delete a shelved changelist (WRITE permission)"""
            result = {"status": "warning", "action": "delete", "message": "Requires approval to delete shelve."}
            self.process_tool_logs("delete_shelve", result, ctx)
            return self.requires_approval(
                "delete_shelve",
                "delete",
                {"changelist_id": changelist_id, "file_paths": file_paths or []}
            )

        @self.mcp.tool(tags=["write", "shelves"])
        async def unshelve_to_changelist(
            changelist_id: Annotated[str, Field(description="Shelved changelist ID to unshelve.")],
            target_changelist: Annotated[str, Field(description="Target changelist ID or 'default'.")] = "default",
            ctx: Context = None
        ) -> dict:
            """Unshelve to another changelist (WRITE permission)"""
            result = await self.shelve_services.unshelve_to_changelist(changelist_id, target_changelist)
            response = {"status": result.get("status"), "action": "unshelve_to_changelist", "message": result.get("message")}
            self.process_tool_logs("unshelve_to_changelist", response, ctx)
            return response

        @self.mcp.tool(tags=["write", "jobs"])
        async def modify_jobs(
            action: Annotated[
                Literal["link_job", "unlink_job"],
                Field(description="Link or unlink a job from a changelist.")
            ],
            changelist_id: Annotated[str, Field(description="Changelist ID.")],
            job_id: Annotated[str, Field(description="Job ID.")],
            ctx: Context = None
        ) -> dict:
            """Link or unlink jobs (WRITE permission)"""
            if action == "link_job":
                result = await self.job_services.link_job_to_changelist(changelist_id, job_id)
            else:
                result = await self.job_services.unlink_job_from_changelist(changelist_id, job_id)
            response = {"status": result.get("status"), "action": action, "message": result.get("message")}
            self.process_tool_logs("modify_jobs", response, ctx)
            return response

        @self.mcp.tool(tags=["write", "delete"])
        async def execute_delete(
            source_tool: Annotated[
                Literal["delete_workspace", "delete_changelist", "delete_files", "delete_shelve"],
                Field(description="Source delete tool that initiated approval.")
            ],
            workspace_name: Annotated[
                Optional[str],
                Field(description="Workspace name to delete.")
            ] = None,
            changelist_id: Annotated[
                Optional[str],
                Field(description="Changelist ID for changelist or shelve deletes.")
            ] = None,
            file_paths: Annotated[
                Optional[List[str]],
                Field(description="File paths for file or shelve deletes.")
            ] = None,
            changelist: Annotated[
                str,
                Field(description="Changelist ID for file deletes.")
            ] = "default",
            ctx: Context = None
        ) -> dict:
            """Execute an approved delete operation (WRITE permission)"""
            toolset_map = {
                "delete_workspace": "workspaces",
                "delete_changelist": "changelists",
                "delete_files": "files",
                "delete_shelve": "shelves"
            }
            toolset = toolset_map.get(source_tool, "")
            if toolset not in self.toolsets:
                result = {"status": "error", "action": "delete", "message": f"Toolset not allowed: {toolset}"}
                self.process_tool_logs("execute_delete", result, ctx)
                return result

            if source_tool == "delete_workspace":
                if not workspace_name:
                    result = {"status": "error", "action": "delete", "message": "workspace_name is required"}
                else:
                    result = await self.workspace_services.delete_workspace(workspace_name)
            elif source_tool == "delete_changelist":
                if not changelist_id:
                    result = {"status": "error", "action": "delete", "message": "changelist_id is required"}
                else:
                    result = await self.changelist_services.delete_changelist(changelist_id)
            elif source_tool == "delete_files":
                if not file_paths:
                    result = {"status": "error", "action": "delete", "message": "file_paths are required"}
                else:
                    result = await self.file_services.delete_files(file_paths, changelist or "default")
            elif source_tool == "delete_shelve":
                if not changelist_id:
                    result = {"status": "error", "action": "delete", "message": "changelist_id is required"}
                else:
                    result = await self.shelve_services.delete_shelve(changelist_id, file_paths or [])
            else:
                result = {"status": "error", "action": "delete", "message": f"Unknown source tool: {source_tool}"}

            response = {"status": result.get("status"), "action": "delete", "message": result.get("message", result)}
            self.process_tool_logs("execute_delete", response, ctx)
            return response

        self._apply_toolset_visibility()
        

    def run(self):
        """Run the MCP server"""
        self.mcp.run()