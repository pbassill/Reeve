import os
import secrets
from pathlib import Path


class Settings:
    def __init__(self) -> None:
        self.data_dir = Path(os.environ.get("MANAGE_DATA_DIR", "/data"))
        self.data_dir.mkdir(parents=True, exist_ok=True)

        self.database_url = os.environ.get(
            "MANAGE_DATABASE_URL", f"sqlite:///{self.data_dir / 'manage.db'}"
        )

        self.secret_key = os.environ.get("MANAGE_SECRET_KEY") or self._load_or_create_secret()

        self.bootstrap_admin_user = os.environ.get("MANAGE_ADMIN_USER", "admin")
        self.bootstrap_admin_password = os.environ.get("MANAGE_ADMIN_PASSWORD")

        self.public_url = os.environ.get("MANAGE_PUBLIC_URL", "").rstrip("/")
        self.checkin_interval_seconds = int(os.environ.get("MANAGE_CHECKIN_INTERVAL", "30"))
        self.offline_after_seconds = int(os.environ.get("MANAGE_OFFLINE_AFTER", "120"))
        self.max_task_output_chars = 64 * 1024

        # LDAP / AD (all optional — empty MANAGE_LDAP_URL disables LDAP).
        self.ldap_url = os.environ.get("MANAGE_LDAP_URL", "").strip()
        self.ldap_bind_dn = os.environ.get("MANAGE_LDAP_BIND_DN", "").strip()
        self.ldap_bind_password = os.environ.get("MANAGE_LDAP_BIND_PASSWORD", "")
        self.ldap_user_search_base = os.environ.get("MANAGE_LDAP_USER_SEARCH_BASE", "").strip()
        self.ldap_user_filter = os.environ.get(
            "MANAGE_LDAP_USER_FILTER", "(sAMAccountName={username})"
        )
        self.ldap_admin_group_dn = os.environ.get("MANAGE_LDAP_ADMIN_GROUP_DN", "").strip()
        self.ldap_use_ssl = os.environ.get("MANAGE_LDAP_USE_SSL", "").lower() in ("1", "true", "yes")

    def _load_or_create_secret(self) -> str:
        secret_path = self.data_dir / "secret_key"
        if secret_path.exists():
            return secret_path.read_text().strip()
        value = secrets.token_urlsafe(48)
        secret_path.write_text(value)
        try:
            os.chmod(secret_path, 0o600)
        except OSError:
            pass
        return value


settings = Settings()
