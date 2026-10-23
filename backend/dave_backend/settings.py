from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DAVE_", extra="ignore")
    redis_url: str = "redis://localhost:6379/0"
    ssh_host: str = "cluster"
    remote_root: str = "dave-cluster"
    ssh_config: str | None = None
    ssh_identity: str | None = None
    use_ssh_agent: bool = False
    poll_seconds: float = Field(default=5.0, ge=1)
    command_timeout: float = Field(default=30.0, gt=0, le=120)
    ssh_output_limit: int = Field(default=1_048_576, ge=1024, le=16 * 1024 * 1024)
    worker_lock_seconds: int = Field(default=30, ge=3)
