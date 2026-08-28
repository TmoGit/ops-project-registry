"""Read-only integration adapters.  They intentionally accept references, never secrets."""
import os
import shutil
import subprocess
from dataclasses import dataclass

import httpx


@dataclass
class Check:
    status: str
    message: str


class IntegrationAdapter:
    def __init__(self, config): self.config = config
    def validate_configuration(self) -> Check:
        if not self.config.configuration_json: return Check("NOT_CONFIGURED", "Non-secret configuration has not been supplied.")
        if self.config.provider.value != "OPENAI" and not self.config.credential_source:
            return Check("ERROR", "A credential source is required.")
        if self.config.credential_source not in {None, "CLI_SESSION"} and not self.config.credential_reference:
            return Check("ERROR", "A credential reference is required.")
        return Check("CONFIGURED", "Configuration is valid.")
    def test_connection(self) -> Check: return self.validate_configuration()


class OpenAIIntegration(IntegrationAdapter):
    def test_connection(self) -> Check:
        check = self.validate_configuration()
        if check.status != "CONFIGURED": return check
        executable = self.config.configuration_json.get("executable_path", "codex")
        path = shutil.which(executable) if "/" not in executable else executable if os.path.isfile(executable) else None
        if not path: return Check("ERROR", "Codex executable was not found.")
        try:
            result = subprocess.run([path, "--version"], text=True, capture_output=True, timeout=10)
        except (OSError, subprocess.SubprocessError): return Check("ERROR", "Codex executable could not be called.")
        if result.returncode: return Check("ERROR", "Codex executable returned an error.")
        return Check("CONNECTED", f"Codex CLI available. Default model: {self.config.configuration_json.get('model', 'not configured')}.")


class HttpIntegration(IntegrationAdapter):
    provider_name = "service"
    def test_connection(self) -> Check:
        check = self.validate_configuration()
        if check.status == "ERROR": return check
        base_url = self.config.configuration_json.get("base_url", "").rstrip("/")
        if not base_url.startswith(("https://", "http://")): return Check("ERROR", "A valid HTTPS or HTTP base URL is required.")
        if self.config.credential_source == "CLI_SESSION":
            return Check("ERROR", "CLI session credentials cannot be verified for this API integration.")
        token = self._credential_value()
        if not token: return Check("ERROR", f"Credential reference {self.config.credential_reference or '(missing)'} could not be resolved.")
        try:
            response = httpx.get(f"{base_url}{self.api_path}", headers={"Authorization": f"Bearer {token}"}, timeout=10)
            response.raise_for_status()
        except httpx.HTTPError: return Check("ERROR", f"{self.provider_name} API could not be reached or authorized.")
        return Check("CONNECTED", f"{self.provider_name} API is reachable and credential reference resolved.")

    def _credential_value(self) -> str | None:
        if self.config.credential_source == "ENVIRONMENT":
            return os.environ.get(self.config.credential_reference or "")
        if self.config.credential_source == "FILE":
            try:
                return open(self.config.credential_reference or "", encoding="utf-8").read().strip()
            except OSError:
                return None
        return None


class GitHubIntegration(HttpIntegration):
    provider_name, api_path = "GitHub", "/api/v3/user" # GitHub.com overrides below
    def test_connection(self) -> Check:
        base_url = self.config.configuration_json.get("base_url", "https://github.com").rstrip("/")
        if base_url == "https://github.com":
            original = self.config.configuration_json.get("base_url")
            self.config.configuration_json["base_url"] = "https://api.github.com"
            self.api_path = "/user"
            try: return super().test_connection()
            finally:
                if original is None: self.config.configuration_json.pop("base_url", None)
                else: self.config.configuration_json["base_url"] = original
        return super().test_connection()


class GiteaIntegration(HttpIntegration):
    provider_name, api_path = "Gitea", "/api/v1/user"


class AWSIntegration(IntegrationAdapter):
    def test_connection(self) -> Check:
        check = self.validate_configuration()
        if check.status == "ERROR": return check
        try:
            import boto3
            session = boto3.Session(profile_name=self.config.credential_reference if self.config.credential_source == "AWS_PROFILE" else None, region_name=self.config.configuration_json.get("region"))
            identity = session.client("sts").get_caller_identity()
        except Exception: return Check("ERROR", "AWS identity could not be verified with the configured credential chain.")
        return Check("CONNECTED", f"AWS identity verified: account {identity.get('Account', 'unknown')}, principal {identity.get('Arn', 'unknown')}.")


ADAPTERS = {"OPENAI": OpenAIIntegration, "GITHUB": GitHubIntegration, "GITEA": GiteaIntegration, "AWS": AWSIntegration}
