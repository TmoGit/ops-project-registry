from pydantic import BaseModel, Field


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
