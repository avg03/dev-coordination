from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ProjectStatus(str, Enum):
    PLANNING = "planning"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"


class TaskStatus(str, Enum):
    PLANNED = "planned"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    BLOCKED = "blocked"


class TaskPriority(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ComponentType(str, Enum):
    SERVICE = "service"
    MODULE = "module"
    FRONTEND = "frontend"
    BACKEND = "backend"
    DATABASE = "database"
    API = "api"
    CONFIG = "config"
    OTHER = "other"


class CommunicationPreference(str, Enum):
    GROUP = "group"
    PERSONAL = "personal"
    BOTH = "both"


class SourceType(str, Enum):
    TEXT = "text"
    PDF = "pdf"
    IMAGE = "image"
    MIXED = "mixed"


class _BaseModel(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="ignore")

    @field_validator("*")
    @classmethod
    def _clean_list_fields(cls, value):
        if isinstance(value, list) and value and all(isinstance(item, str) for item in value):
            return [item.strip() for item in value if item and item.strip()]
        return value


class Project(_BaseModel):
    name: str = Field(default="", description="Short project name.")
    description: str = Field(default="", description="Summary of what the project is.")
    objective: str = Field(default="", description="Primary goal the project is trying to achieve.")
    status: ProjectStatus = Field(default=ProjectStatus.PLANNING, description="Current project lifecycle status.")
    domain: str = Field(default="", description="Business/technical domain of the project.")


class PlannedTask(_BaseModel):
    id: str = Field(default="", description="Unique task identifier.")
    title: str = Field(default="", description="Short task title.")
    description: str = Field(default="", description="What the task involves.")
    status: TaskStatus = Field(default=TaskStatus.PLANNED, description="Task execution status.")
    priority: TaskPriority = Field(default=TaskPriority.MEDIUM, description="Task priority level.")


class WorkScope(_BaseModel):
    current_goal: str = Field(default="", description="The immediate goal currently being worked toward.")
    planned_tasks: list[PlannedTask] = Field(default_factory=list, description="Tasks planned for the project.")
    constraints: list[str] = Field(default_factory=list, description="Technical, resource or time constraints.")
    important_decisions: list[str] = Field(default_factory=list, description="Key decisions already made.")


class ArchitectureComponent(_BaseModel):
    id: str = Field(default="", description="Unique component identifier.")
    name: str = Field(default="", description="Component name.")
    type: ComponentType = Field(default=ComponentType.OTHER, description="Component category.")
    description: str = Field(default="", description="What the component does.")
    repository_paths: list[str] = Field(default_factory=list, description="Repo paths owned by this component.")
    responsible_teams: list[str] = Field(default_factory=list, description="Teams responsible for this component.")


class ArchitectureDependency(_BaseModel):
    source_component: str = Field(default="", description="Component that depends on another.")
    target_component: str = Field(default="", description="Component being depended upon.")
    relationship: str = Field(default="", description="Nature of the dependency, e.g. consumes, imports, calls.")
    description: str = Field(default="", description="Why the dependency exists.")


class Architecture(_BaseModel):
    components: list[ArchitectureComponent] = Field(default_factory=list)
    dependencies: list[ArchitectureDependency] = Field(default_factory=list)


class Stakeholder(_BaseModel):
    id: str = Field(default="", description="Unique stakeholder identifier.")
    name: str = Field(default="", description="Person or role name.")
    role: str = Field(default="", description="Role in the organization, e.g. backend engineer.")
    team: str = Field(default="", description="Team this stakeholder belongs to.")
    responsibilities: list[str] = Field(default_factory=list)
    owned_components: list[str] = Field(default_factory=list)
    communication_preference: CommunicationPreference = Field(
        default=CommunicationPreference.BOTH,
        description="Whether they should be reached via group channels, personal DMs or both.",
    )


class TeamContext(_BaseModel):
    team_id: str = Field(default="", description="Unique team identifier.")
    name: str = Field(default="", description="Team name.")
    responsibilities: list[str] = Field(default_factory=list)
    owned_components: list[str] = Field(default_factory=list)
    members: list[str] = Field(default_factory=list)


class PlannedDependency(_BaseModel):
    description: str = Field(default="", description="What the dependency between tasks is.")
    source_task: str = Field(default="", description="Task that must happen first.")
    dependent_task: str = Field(default="", description="Task that relies on the source task.")
    affected_team: str = Field(default="", description="Team most affected by this dependency.")


class RawContext(_BaseModel):
    source_type: SourceType = Field(default=SourceType.TEXT, description="Format of the original input.")
    original_description: str = Field(default="", description="The raw input text provided by the user.")


class Metadata(_BaseModel):
    created_at: str = Field(default="", description="ISO timestamp when the context was created.")
    updated_at: str = Field(default="", description="ISO timestamp of the last update.")
    version: int = Field(default=1, description="Schema version of this context model.")


class AgentAnalysisOutput(_BaseModel):
    project: Project = Field(default_factory=Project)
    work_scope: WorkScope = Field(default_factory=WorkScope)
    architecture: Architecture = Field(default_factory=Architecture)
    stakeholders: list[Stakeholder] = Field(default_factory=list)
    team_context: list[TeamContext] = Field(default_factory=list)
    planned_dependencies: list[PlannedDependency] = Field(default_factory=list)
    success_criteria: list[str] = Field(default_factory=list)


class ProjectContextOutput(AgentAnalysisOutput):
    raw_context: RawContext = Field(default_factory=RawContext)
    metadata: Metadata = Field(default_factory=Metadata)


CONTEXT_AGENT_INSTRUCTION = """
You are a senior engineering architect analyzing a project description.

Your job is to read the provided project description and convert it into a
structured understanding of the project. The description may be informal,
so infer reasonable structure but do NOT invent facts that the text does not
support. When something is unknown, leave it empty rather than guessing.

Analyze and extract, at minimum:

1. project: name, description, objective, status (planning|in_progress|completed),
   domain.
2. work_scope: the current goal being worked toward, the planned tasks
   (each with id, title, description, status, priority), constraints, and
   important decisions mentioned.
3. architecture: the main components (id, name, type, description,
   repository_paths, responsible_teams) and the dependencies between them
   (source_component, target_component, relationship, description).
4. stakeholders: people or roles involved (id, name, role, team,
   responsibilities, owned_components, communication_preference).
5. team_context: the teams involved (team_id, name, responsibilities,
   owned_components, members).
6. planned_dependencies: task ordering dependencies (description,
   source_task, dependent_task, affected_team).
7. success_criteria: measurable outcomes that would mean the work succeeded.

Respond only with the structured JSON matching the requested schema. Do not
add commentary, markdown fences, or text outside the JSON.
"""
