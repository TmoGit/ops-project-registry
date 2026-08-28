from pydantic import BaseModel, Field
from typing import Literal


class IntakeCreate(BaseModel):
    raw_request: str = Field(min_length=1)


class IntakeView(BaseModel):
    id: int
    status: str
    raw_request: str


class ApprovalRequest(BaseModel):
    actor: str = "local-admin"
    project_key: str = Field(min_length=2, max_length=32)
    project_name: str = Field(min_length=1, max_length=255)
    task_title: str = Field(min_length=1, max_length=255)
    task_description: str = Field(min_length=1)


class TriageResult(BaseModel):
    task_type: str
    complexity: int = Field(ge=1, le=5)
    risk: Literal["low", "medium", "high", "critical"]
    estimated_context_tokens: int = Field(ge=0)
    estimated_files: int = Field(ge=0)
    requires_host_write: bool
    requires_database_change: bool
    requires_production_change: bool
    parallelizable: bool
    recommended_executor: Literal["qwen", "codex"]
    recommended_agents: int = Field(ge=1)
    clarification_required: bool
    clarification_questions: list[str]
    reason: str


class ClarificationAnswer(BaseModel):
    answer: str = Field(min_length=1)
