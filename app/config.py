from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, env_prefix="OPS_", extra="ignore")

    database_url: str
    admin_password: str
    session_secret: str
    environment: str = "production"
    bind_host: str = "127.0.0.1"
    bind_port: int = 8000
    ollama_base_url: str = "http://192.168.50.60:11434"
    default_local_model: str = "qwen2.5:7b"
    mobile_bridge_secret: str = ""
    registry_path: str = "/opt/ops-orchestrator/repos/project-registry"
    redis_url: str = "redis://127.0.0.1:6379/0"
    worktrees_path: str = "/opt/ops-orchestrator/worktrees"
    artifacts_path: str = "/opt/ops-orchestrator/artifacts"
    attachments_path: str = "/opt/ops-orchestrator/attachments"
    attachment_max_bytes: int = 10 * 1024 * 1024
    attachment_max_count: int = 5
    homeops_bridge_url: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
