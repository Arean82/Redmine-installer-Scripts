#!/usr/bin/env python3
"""
setup_redmine_install.py
=============================================================================
Platinum Enterprise Redmine Core Installation & Provisioning Orchestrator
=============================================================================
Automates Sections 0 through 10 of the Redmine Installation Guide:
  - Section 1: Dedicated System User & Shared Group Setup
  - Section 2: Build Prerequisite & Native Header Packages
  - Section 3: Interactive Ruby Runtime (System Active vs. Top 5 rbenv Releases)
  - Section 4: Dynamic Redmine Archive Unpack (Local Path vs. Official Web Fetch)
  - Section 5: PostgreSQL Database Provisioning & PGroonga Activation
  - Section 6: Dynamic database.yml Configuration (Secure Generated Credentials)
  - Section 7: Bundler Dependency Install, Secret Token & DB Migration
  - Section 8: Asset Precompilation & Environment Verification
  - Section 9: Enterprise Permission Enforcement & Group Access
  - Section 10: Runtime PID Directory Provisioning
  - Output Manifest: Exports redmine_install_manifest.json in script directory
=============================================================================
"""

from __future__ import annotations

import datetime
import html
import json
import os
import secrets
import shutil
import string
import subprocess
import sys
import tarfile
import urllib.request
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Final


class AnsiColor:
    """ANSI color escape sequences for terminal output formatting."""
    HEADER = "\033[95m"
    BLUE = "\033[94m"
    CYAN = "\033[96m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    RED = "\033[91m"
    BOLD = "\033[1m"
    RESET = "\033[0m"


class DualLogger:
    """Tee logger writing clean output to disk while displaying styled terminal text."""

    _log_file: Path | None = None

    @classmethod
    def _strip_ansi(cls, text: str) -> str:
        """Strip ANSI escape sequences using exact character inspection without regex."""
        result = []
        in_escape = False
        for char in text:
            if char == "\x1B":
                in_escape = True
            elif in_escape:
                if char in "mABCDHIJKHfsuhlrRJK":
                    in_escape = False
            else:
                result.append(char)
        return "".join(result)

    @classmethod
    def init(cls, log_path: Path) -> None:
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            cls._log_file = log_path
            with open(cls._log_file, "a", encoding="utf-8") as f:
                f.write(f"\n{'='*76}\n")
                f.write(f"Installation Session: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write(f"{'='*76}\n")
        except Exception as e:
            print(f"{AnsiColor.YELLOW}[WARN] Could not initialize log at {log_path}: {e}{AnsiColor.RESET}")

    @classmethod
    def write_disk(cls, message: str) -> None:
        if cls._log_file:
            try:
                clean_msg = cls._strip_ansi(message)
                timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                with open(cls._log_file, "a", encoding="utf-8") as f:
                    f.write(f"[{timestamp}] {clean_msg}\n")
            except Exception:
                pass

    @classmethod
    def info(cls, message: str) -> None:
        print(f"{AnsiColor.BLUE}[INFO]{AnsiColor.RESET} {message}")
        cls.write_disk(f"[INFO] {message}")

    @classmethod
    def success(cls, message: str) -> None:
        print(f"{AnsiColor.GREEN}[SUCCESS]{AnsiColor.RESET} {message}")
        cls.write_disk(f"[SUCCESS] {message}")

    @classmethod
    def warn(cls, message: str) -> None:
        print(f"{AnsiColor.YELLOW}[WARN]{AnsiColor.RESET} {message}")
        cls.write_disk(f"[WARN] {message}")

    @classmethod
    def error(cls, message: str) -> None:
        print(f"{AnsiColor.RED}[ERROR]{AnsiColor.RESET} {message}", file=sys.stderr)
        cls.write_disk(f"[ERROR] {message}")

    @classmethod
    def step(cls, title: str) -> None:
        separator = "=" * 76
        formatted = f"\n{AnsiColor.CYAN}{separator}\n[*] {title}\n{separator}{AnsiColor.RESET}"
        print(formatted)
        cls.write_disk(f"\n{'='*76}\n[*] {title}\n{'='*76}")


@dataclass
class InstallParams:
    """Enterprise container holding user-selected deployment parameters."""
    install_path: Path
    system_user: str = "redmineuser"
    shared_group: str = "sharedgroup"
    redmine_archive_path: Path | None = None
    redmine_version: str = "7.0.3"
    selected_ruby: str = ""
    rbenv_root: Path = field(default_factory=lambda: Path("/home/redmineuser/.rbenv"))
    db_name_prod: str = "redmine"
    db_name_dev: str = "redmine_dev"
    db_name_test: str = "redmine_test"
    db_user_prod: str = "redmine"
    db_pass_prod: str = ""
    db_user_dev: str = "redmine_dev_user"
    db_pass_dev: str = ""
    db_user_test: str = "redmine_test_user"
    db_pass_test: str = ""
    enable_pgroonga: bool = False
    migrate_all_envs: bool = False
    default_lang: str = "en"


class SystemInspector:
    """Executes host checks, user discovery, and subprocess commands."""

    @staticmethod
    def is_root() -> bool:
        return os.geteuid() == 0

    @staticmethod
    def run_command(cmd: list[str] | str, check: bool = True, as_user: str | None = None, shell: bool = False, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
        if as_user and as_user != os.environ.get("USER", ""):
            if isinstance(cmd, list):
                cmd_str = " ".join(f"'{arg}'" if " " in arg or "$" in arg else arg for arg in cmd)
            else:
                cmd_str = cmd
            full_cmd = ["sudo", "-u", as_user, "bash", "-c", cmd_str]
            res = subprocess.run(full_cmd, check=check, text=True, capture_output=True, env=env)
            DualLogger.write_disk(f"Command (as {as_user}): {cmd_str} -> ReturnCode: {res.returncode}")
            return res

        if isinstance(cmd, str) and not shell:
            import shlex
            cmd = shlex.split(cmd)

        res = subprocess.run(cmd, check=check, text=True, capture_output=True, shell=shell, env=env)
        DualLogger.write_disk(f"Command: {cmd} -> ReturnCode: {res.returncode}")
        return res

    @staticmethod
    def is_command_available(command: str) -> bool:
        return shutil.which(command) is not None

    @staticmethod
    def is_service_active(service: str) -> bool:
        res = subprocess.run(["systemctl", "is-active", "--quiet", service])
        return res.returncode == 0


@dataclass(frozen=True)
class RubyVersionCandidate:
    """Data representation of a Ruby release candidate and its compatibility from setup_ruby.py."""
    version_str: str
    major: int
    minor: int
    patch: int

    @classmethod
    def from_string(cls, raw: str) -> RubyVersionCandidate | None:
        token = raw.strip()
        parts = token.split(".")
        if len(parts) == 3 and all(p.isdigit() for p in parts):
            return cls(
                version_str=token,
                major=int(parts[0]),
                minor=int(parts[1]),
                patch=int(parts[2]),
            )
        return None

    @property
    def semantic_tuple(self) -> tuple[int, int, int]:
        return (self.major, self.minor, self.patch)

    @property
    def compatibility_annotation(self) -> str:
        return ""


class RubyVersionManager:
    """Dynamically queries latest upstream Ruby definitions from ruby-build."""

    @staticmethod
    def get_system_ruby() -> str | None:
        try:
            res = subprocess.run(["ruby", "-v"], capture_output=True, text=True)
            if res.returncode == 0:
                tokens = res.stdout.strip().split()
                if len(tokens) >= 2 and tokens[0] == "ruby":
                    ver_candidate = tokens[1]
                    parts = ver_candidate.split(".")
                    if len(parts) == 3 and all(p.isdigit() for p in parts):
                        return ver_candidate
                return res.stdout.strip()
        except Exception:
            pass
        return None

    @staticmethod
    def fetch_top_5_latest_releases(rbenv_plugins_dir: Path | None = None) -> list[RubyVersionCandidate]:
        if rbenv_plugins_dir and (rbenv_plugins_dir / "ruby-build").is_dir():
            res_git = subprocess.run(["which", "git"], capture_output=True, text=True)
            if res_git.returncode == 0:
                subprocess.run(["git", "-C", str(rbenv_plugins_dir / "ruby-build"), "pull"], capture_output=True)

        candidates: list[RubyVersionCandidate] = []
        try:
            res = subprocess.run(["rbenv", "install", "-l"], capture_output=True, text=True)
            if res.returncode == 0:
                for line in res.stdout.splitlines():
                    candidate = RubyVersionCandidate.from_string(line)
                    if candidate:
                        candidates.append(candidate)
        except Exception:
            pass

        if not candidates:
            return []

        # Find the highest major version available (e.g. 4 if present, otherwise 3)
        highest_major = max(c.major for c in candidates)
        major_candidates = [c for c in candidates if c.major == highest_major]
        major_candidates.sort(key=lambda item: item.semantic_tuple, reverse=True)
        return major_candidates[:5]


class Installer:
    """Encapsulates Sections 0 through 10 of the Redmine installation pipeline."""

    def __init__(self, params: InstallParams, manifest_output_path: Path) -> None:
        self.params = params
        self.manifest_path = manifest_output_path
        self.rbenv_shims = self.params.rbenv_root / "shims"

    def step1_create_user_and_group(self) -> None:
        DualLogger.step("Section 1: Creating Dedicated Redmine System User & Shared Group")

        res_grp = subprocess.run(["getent", "group", self.params.shared_group], capture_output=True, text=True)
        if res_grp.returncode != 0:
            DualLogger.info(f"Creating group '{self.params.shared_group}'...")
            SystemInspector.run_command(["groupadd", "--system", self.params.shared_group])
            DualLogger.success(f"Group '{self.params.shared_group}' created.")
        else:
            DualLogger.info(f"Group '{self.params.shared_group}' already exists.")

        res_usr = subprocess.run(["id", "-u", self.params.system_user], capture_output=True, text=True)
        if res_usr.returncode != 0:
            DualLogger.info(f"Creating system user '{self.params.system_user}'...")
            cmd = [
                "adduser", "--system", "--group",
                "--shell", "/bin/bash",
                "--home", f"/home/{self.params.system_user}",
                self.params.system_user
            ]
            SystemInspector.run_command(cmd)
            DualLogger.success(f"User '{self.params.system_user}' created.")
        else:
            DualLogger.info(f"User '{self.params.system_user}' already exists.")

        SystemInspector.run_command(["usermod", "-aG", "sudo", self.params.system_user])
        SystemInspector.run_command(["usermod", "-aG", self.params.shared_group, self.params.system_user])
        DualLogger.success(f"User '{self.params.system_user}' assigned to groups 'sudo' and '{self.params.shared_group}'.")

    def step2_install_prerequisites(self) -> None:
        DualLogger.step("Section 2: Installing Build Prerequisites & Native Header Packages")
        packages = [
            "build-essential", "libssl-dev", "libreadline-dev", "zlib1g-dev",
            "libmagickwand-dev", "imagemagick", "libcurl4-openssl-dev", "libffi-dev",
            "unzip", "curl", "wget", "git", "libgdbm-dev", "libyaml-dev",
            "libpq-dev", "ca-certificates"
        ]
        DualLogger.info("Verifying and installing native build packages...")
        SystemInspector.run_command(["apt", "update", "-y"])
        SystemInspector.run_command(["apt", "install", "-y"] + packages)
        DualLogger.success("Build prerequisites and native development libraries verified.")

    def step3_configure_ruby(self) -> None:
        DualLogger.step("Section 3: Setting up Ruby Runtime Environment")

        if self.params.selected_ruby == "system":
            sys_ruby = shutil.which("ruby")
            if sys_ruby:
                res = subprocess.run(["ruby", "-v"], capture_output=True, text=True)
                DualLogger.success(f"Using system-wide active Ruby: {res.stdout.strip()}")
                return
            else:
                DualLogger.error("System Ruby not found on PATH.")
                sys.exit(1)

        user_home = Path(f"/home/{self.params.system_user}")
        rbenv_dir = user_home / ".rbenv"
        plugins_dir = rbenv_dir / "plugins" / "ruby-build"

        if not rbenv_dir.exists():
            DualLogger.info(f"Cloning rbenv for {self.params.system_user}...")
            SystemInspector.run_command(
                f"git clone https://github.com/rbenv/rbenv.git '{rbenv_dir}'",
                as_user=self.params.system_user,
                shell=True
            )

        if not plugins_dir.exists():
            DualLogger.info("Cloning ruby-build plugin...")
            SystemInspector.run_command(
                f"mkdir -p '{rbenv_dir}/plugins' && git clone https://github.com/rbenv/ruby-build.git '{plugins_dir}'",
                as_user=self.params.system_user,
                shell=True
            )
        else:
            DualLogger.info("Updating ruby-build definitions...")
            subprocess.run(f"cd '{plugins_dir}' && git pull", shell=True, capture_output=True)

        bashrc = user_home / ".bashrc"
        bashrc_text = bashrc.read_text(encoding="utf-8") if bashrc.exists() else ""
        if "rbenv init" not in bashrc_text:
            profile_lines = (
                '\n# rbenv configuration\n'
                'export PATH="$HOME/.rbenv/bin:$PATH"\n'
                'eval "$(rbenv init - bash)"\n'
            )
            with open(bashrc, "a", encoding="utf-8") as f:
                f.write(profile_lines)
            DualLogger.info("Configured .bashrc with rbenv environment hooks.")

        installed_rubies_res = subprocess.run(
            f"sudo -u {self.params.system_user} bash -c 'export PATH=\"$HOME/.rbenv/bin:$PATH\" && eval \"$(rbenv init - bash)\" && rbenv versions --bare'",
            shell=True, capture_output=True, text=True
        )
        installed_rubies = [r.strip() for r in installed_rubies_res.stdout.splitlines() if r.strip()]

        if self.params.selected_ruby not in installed_rubies:
            DualLogger.info(f"Compiling and installing Ruby {self.params.selected_ruby}...")
            cmd = f"export PATH=\"$HOME/.rbenv/bin:$PATH\" && eval \"$(rbenv init - bash)\" && rbenv install {self.params.selected_ruby}"
            SystemInspector.run_command(cmd, as_user=self.params.system_user, shell=True)
            DualLogger.success(f"Ruby {self.params.selected_ruby} compiled successfully.")
        else:
            DualLogger.info(f"Ruby {self.params.selected_ruby} is already compiled in rbenv.")

        set_global_cmd = f"export PATH=\"$HOME/.rbenv/bin:$PATH\" && eval \"$(rbenv init - bash)\" && rbenv global {self.params.selected_ruby}"
        SystemInspector.run_command(set_global_cmd, as_user=self.params.system_user, shell=True)

        DualLogger.info("Verifying Bundler in rbenv...")
        b_cmd = f"export PATH=\"$HOME/.rbenv/bin:$PATH\" && eval \"$(rbenv init - bash)\" && gem install bundler --no-document"
        SystemInspector.run_command(b_cmd, as_user=self.params.system_user, shell=True)
        DualLogger.success(f"Ruby {self.params.selected_ruby} & Bundler configured for {self.params.system_user}.")

    def step4_unpack_redmine(self) -> None:
        DualLogger.step("Section 4: Unpacking Redmine Application Source Code")

        archive = self.params.redmine_archive_path
        if not archive or not archive.exists():
            download_url = f"https://www.redmine.org/releases/redmine-{self.params.redmine_version}.tar.gz"
            archive = Path(f"/tmp/redmine-{self.params.redmine_version}.tar.gz")
            DualLogger.info(f"Downloading Redmine {self.params.redmine_version} from {download_url}...")
            urllib.request.urlretrieve(download_url, archive)
            DualLogger.success(f"Downloaded source archive: {archive}")

        dest = self.params.install_path
        dest.parent.mkdir(parents=True, exist_ok=True)

        extract_tmp = Path(f"/tmp/redmine_unpack_{int(datetime.datetime.now().timestamp())}")
        extract_tmp.mkdir(parents=True, exist_ok=True)

        DualLogger.info(f"Extracting {archive}...")
        if str(archive).endswith(".zip"):
            with zipfile.ZipFile(archive, 'r') as zip_ref:
                zip_ref.extractall(extract_tmp)
        else:
            with tarfile.open(archive, 'r:*') as tar_ref:
                tar_ref.extractall(extract_tmp)

        subdirs = [d for d in extract_tmp.iterdir() if d.is_dir()]
        extracted_folder = subdirs[0] if subdirs else extract_tmp

        if dest.exists():
            DualLogger.warn(f"Target path {dest} already exists. Creating backup before replacing...")
            backup_path = dest.with_name(f"{dest.name}_backup_{datetime.datetime.now().strftime('%Y%m%d%H%M%S')}")
            shutil.move(str(dest), str(backup_path))
            DualLogger.info(f"Existing installation backed up to: {backup_path}")

        shutil.move(str(extracted_folder), str(dest))
        shutil.rmtree(extract_tmp, ignore_errors=True)

        SystemInspector.run_command(["chown", "-R", f"{self.params.system_user}:{self.params.shared_group}", str(dest)])
        DualLogger.success(f"Redmine {self.params.redmine_version} deployed cleanly to: {dest}")

    def step5_provision_postgresql(self) -> None:
        DualLogger.step("Section 5: Provisioning PostgreSQL Databases and Roles")

        if not SystemInspector.is_command_available("psql"):
            DualLogger.info("PostgreSQL is not installed. Installing standard distribution packages...")
            SystemInspector.run_command(["apt", "install", "-y", "postgresql", "postgresql-contrib", "libpq-dev"])

        if not SystemInspector.is_service_active("postgresql"):
            SystemInspector.run_command(["systemctl", "enable", "postgresql"])
            SystemInspector.run_command(["systemctl", "start", "postgresql"])

        db_configs = [
            ("Production", self.params.db_name_prod, self.params.db_user_prod, self.params.db_pass_prod),
        ]
        if self.params.migrate_all_envs:
            db_configs.extend([
                ("Development", self.params.db_name_dev, self.params.db_user_dev, self.params.db_pass_dev),
                ("Test", self.params.db_name_test, self.params.db_user_test, self.params.db_pass_test),
            ])

        for env_label, db_name, db_user, db_pass in db_configs:
            DualLogger.info(f"Provisioning PostgreSQL role '{db_user}' for {env_label}...")
            role_sql = (
                f"DO $$\n"
                f"BEGIN\n"
                f"  IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = '{db_user}') THEN\n"
                f"    CREATE ROLE {db_user} LOGIN CREATEDB PASSWORD '{db_pass}';\n"
                f"  ELSE\n"
                f"    ALTER ROLE {db_user} WITH PASSWORD '{db_pass}';\n"
                f"  END IF;\n"
                f"END\n"
                f"$$;"
            )
            subprocess.run(f"sudo -u postgres psql -c \"{role_sql}\"", shell=True, check=True)
            DualLogger.success(f"PostgreSQL role '{db_user}' configured.")

            DualLogger.info(f"Provisioning PostgreSQL database '{db_name}' (Owner: '{db_user}')...")
            db_sql = (
                f"SELECT 'CREATE DATABASE {db_name} OWNER {db_user} ENCODING ''UTF8''' "
                f"WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = '{db_name}')\\gexec"
            )
            subprocess.run(f"sudo -u postgres psql -c \"{db_sql}\"", shell=True, check=True)
            DualLogger.success(f"PostgreSQL database '{db_name}' ready.")

            if self.params.enable_pgroonga:
                pgroonga_cmd = f"sudo -u postgres psql -d {db_name} -c 'CREATE EXTENSION IF NOT EXISTS pgroonga;'"
                res = subprocess.run(pgroonga_cmd, shell=True, capture_output=True, text=True)
                if res.returncode == 0:
                    DualLogger.success(f"PGroonga extension active in database: {db_name}")
                else:
                    DualLogger.warn(f"PGroonga extension notice for {db_name}: Package may need manual installation.")

        if not self.params.enable_pgroonga:
            DualLogger.info("PGroonga extension skipped as requested.")

        DualLogger.success("PostgreSQL roles and databases fully provisioned and verified.")

    def step6_configure_database_yml(self) -> None:
        DualLogger.step("Section 6: Generating Database Connection Profile (config/database.yml)")

        db_yml_path = self.params.install_path / "config" / "database.yml"
        content = f"""# Redmine Database Configuration (Platinum Provisioned)
production:
  adapter: postgresql
  database: {self.params.db_name_prod}
  host: localhost
  username: {self.params.db_user_prod}
  password: "{self.params.db_pass_prod}"
  encoding: utf8

development:
  adapter: postgresql
  database: {self.params.db_name_dev}
  host: localhost
  username: {self.params.db_user_dev}
  password: "{self.params.db_pass_dev}"
  encoding: utf8

test:
  adapter: postgresql
  database: {self.params.db_name_test}
  host: localhost
  username: {self.params.db_user_test}
  password: "{self.params.db_pass_test}"
  encoding: utf8
"""
        db_yml_path.write_text(content, encoding="utf-8")
        SystemInspector.run_command(["chown", f"{self.params.system_user}:{self.params.shared_group}", str(db_yml_path)])
        SystemInspector.run_command(["chmod", "660", str(db_yml_path)])
        DualLogger.success(f"Generated secure database configuration: {db_yml_path}")

    def step7_bundle_and_migrate(self) -> None:
        DualLogger.step("Section 7: Installing Gems, Generating Secret Token & Migrating Databases")

        exec_prefix = f"cd '{self.params.install_path}' && export PATH=\"/home/{self.params.system_user}/.rbenv/bin:$PATH\" && eval \"$(rbenv init - bash)\" && "

        DualLogger.info("Running bundle install (downloading & compiling Ruby gems)...")
        SystemInspector.run_command(f"{exec_prefix}bundle install", as_user=self.params.system_user, shell=True)
        DualLogger.success("Ruby gems successfully resolved and installed.")

        DualLogger.info("Generating secret token...")
        SystemInspector.run_command(f"{exec_prefix}bundle exec rake generate_secret_token", as_user=self.params.system_user, shell=True)
        DualLogger.success("Secret encryption token generated.")

        envs_to_migrate = ["production", "development", "test"] if self.params.migrate_all_envs else ["production"]
        for env_name in envs_to_migrate:
            DualLogger.info(f"Running database migrations for environment: {env_name}...")
            cmd = f"{exec_prefix}RAILS_ENV={env_name} bundle exec rake db:migrate"
            SystemInspector.run_command(cmd, as_user=self.params.system_user, shell=True)

        DualLogger.info(f"Loading default configuration data (Language: {self.params.default_lang})...")
        cmd_load = f"{exec_prefix}RAILS_ENV=production bundle exec rake redmine:load_default_data REDMINE_LANG={self.params.default_lang}"
        SystemInspector.run_command(cmd_load, as_user=self.params.system_user, shell=True)
        DualLogger.success("Database schema migrated and default seed data loaded.")

    def step8_precompile_assets(self) -> None:
        DualLogger.step("Section 8: Precompiling Core Application Assets")
        exec_prefix = f"cd '{self.params.install_path}' && export PATH=\"/home/{self.params.system_user}/.rbenv/bin:$PATH\" && eval \"$(rbenv init - bash)\" && "
        cmd = f"{exec_prefix}RAILS_ENV=production bundle exec rake assets:precompile"
        SystemInspector.run_command(cmd, as_user=self.params.system_user, shell=True)
        DualLogger.success("Assets precompiled successfully for production.")

    def step9_enforce_permissions(self) -> None:
        DualLogger.step("Section 9: Enforcing Enterprise Ownership & Permissions")

        dest = self.params.install_path
        for sub in ["tmp", "tmp/pdf", "tmp/pids", "public/plugin_assets", "files", "log"]:
            d = dest / sub
            d.mkdir(parents=True, exist_ok=True)

        SystemInspector.run_command(["chown", "-R", f"{self.params.system_user}:{self.params.shared_group}", str(dest)])

        writable_dirs = [str(dest / sub) for sub in ["files", "log", "tmp", "public/plugin_assets"]]
        SystemInspector.run_command(["chmod", "-R", "775"] + writable_dirs)
        DualLogger.success(f"Enterprise ownership ({self.params.system_user}:{self.params.shared_group}) & 775 permissions enforced.")

    def step10_setup_pid_directory(self) -> None:
        DualLogger.step("Section 10: Configuring Runtime PID Directory")
        pid_dir = self.params.install_path / "tmp" / "pids"
        pid_dir.mkdir(parents=True, exist_ok=True)
        SystemInspector.run_command(["chown", "-R", f"{self.params.system_user}:{self.params.shared_group}", str(pid_dir)])
        SystemInspector.run_command(["chmod", "775", str(pid_dir)])
        DualLogger.success(f"Runtime PID directory ready: {pid_dir}")

    def export_manifest(self) -> None:
        DualLogger.step("Exporting Installation Manifest JSON")

        manifest_data = {
            "timestamp": datetime.datetime.now().isoformat(),
            "installer_version": "1.0.0-platinum",
            "application": {
                "name": "Redmine",
                "version": self.params.redmine_version,
                "install_path": str(self.params.install_path),
                "source_archive": str(self.params.redmine_archive_path) if self.params.redmine_archive_path else "downloaded_from_official_upstream"
            },
            "system_identity": {
                "user": self.params.system_user,
                "group": self.params.shared_group,
                "home_directory": f"/home/{self.params.system_user}",
                "shell": "/bin/bash"
            },
            "runtime_environment": {
                "ruby_version": self.params.selected_ruby,
                "rbenv_root": str(self.params.rbenv_root)
            },
            "database": {
                "engine": "postgresql",
                "host": "localhost",
                "port": 5432,
                "environments": {
                    "production": {
                        "database": self.params.db_name_prod,
                        "user": self.params.db_user_prod,
                        "password": self.params.db_pass_prod
                    },
                    "development": {
                        "database": self.params.db_name_dev,
                        "user": self.params.db_user_dev,
                        "password": self.params.db_pass_dev
                    },
                    "test": {
                        "database": self.params.db_name_test,
                        "user": self.params.db_user_test,
                        "password": self.params.db_pass_test
                    }
                }
            },
            "post_install_status": {
                "database_migrated": True,
                "default_language": self.params.default_lang,
                "assets_precompiled": True,
                "permissions_enforced": True
            }
        }

        try:
            self.manifest_path.write_text(json.dumps(manifest_data, indent=2), encoding="utf-8")
            SystemInspector.run_command(["chmod", "660", str(self.manifest_path)])
            DualLogger.success(f"Installation manifest successfully written to:\n  {self.manifest_path.resolve()}")
        except Exception as e:
            DualLogger.error(f"Failed to write manifest JSON: {e}")


def prompt_user_installation() -> InstallParams:
    print(f"\n{AnsiColor.HEADER}{'='*76}")
    print("  REDMINE 7.0.3 PLATINUM CORE INSTALLER (SECTIONS 0 - 10)")
    print(f"{'='*76}{AnsiColor.RESET}\n")

    # 1. Target Directory Option
    print(f"{AnsiColor.BOLD}1. Select Redmine Target Installation Directory:{AnsiColor.RESET}")
    print("   [1] Standard Enterprise Root: /data/redmine (Recommended)")
    print("   [2] Distribution Standard:   /opt/redmine")
    print("   [3] Web Server Root:         /var/www/redmine")
    print("   [4] Custom Path")

    path_choice = input("\nSelect choice [1-4, Default: 1]: ").strip()
    if path_choice == "2":
        install_path = Path("/opt/redmine")
    elif path_choice == "3":
        install_path = Path("/var/www/redmine")
    elif path_choice == "4":
        custom_in = input("Enter custom installation path: ").strip()
        install_path = Path(custom_in) if custom_in else Path("/data/redmine")
    else:
        install_path = Path("/data/redmine")

    DualLogger.info(f"Target directory set: {install_path}")

    # 2. Archive Selection
    print(f"\n{AnsiColor.BOLD}2. Redmine Source Archive Selection:{AnsiColor.RESET}")
    archive_in = input("Enter path to downloaded Redmine archive (.tar.gz / .zip) [Press Enter to auto-download]: ").strip()
    archive_path = Path(archive_in) if archive_in and Path(archive_in).exists() else None

    redmine_ver = "7.0.3"
    if not archive_path:
        ver_in = input(f"Enter Redmine version to download [Default: 7.0.3]: ").strip()
        if ver_in:
            redmine_ver = ver_in

    # 3. Ruby Runtime Selection
    print(f"\n{AnsiColor.BOLD}3. Ruby Runtime Selection:{AnsiColor.RESET}")
    print("-" * 74)
    sys_ruby = RubyVersionManager.get_system_ruby()
    candidates = RubyVersionManager.fetch_top_5_latest_releases(Path("/home/redmineuser/.rbenv/plugins"))

    if sys_ruby:
        print(f"   [S] Current Active System Ruby: {AnsiColor.YELLOW}{sys_ruby}{AnsiColor.RESET}")
    else:
        print("   [S] System Ruby (Not installed - selecting this will prompt you to install system ruby)")

    if candidates:
        for idx, cand in enumerate(candidates, 1):
            latest_badge = f" {AnsiColor.GREEN}(Latest Recommended){AnsiColor.RESET}" if idx == 1 else ""
            print(f"   [{idx}] Ruby {cand.version_str}{latest_badge}")
        print("-" * 74)
        default_choice = "1"
        prompt_text = f"\nSelect Ruby version [1-{len(candidates)} or S, Default: 1 ({candidates[0].version_str})]: "
    else:
        print("   No rbenv versions discovered from ruby-build definitions.")
        print("-" * 74)
        default_choice = "S" if sys_ruby else "custom"
        prompt_text = f"\nSelect Ruby version [S to use system ruby, or enter version e.g. 4.0.9]: "

    while True:
        ruby_choice = input(prompt_text).strip()
        if not ruby_choice:
            ruby_choice = default_choice

        if ruby_choice.upper() == "S":
            if not sys_ruby:
                print(f"{AnsiColor.YELLOW}[!] System Ruby is not installed on this machine.{AnsiColor.RESET}")
                print("    Please install system ruby via your package manager (e.g., sudo apt install -y ruby-full),")
                print("    or select one of the local rbenv versions [1-5] / enter a version (e.g. 4.0.9).")
                continue
            selected_ruby = "system"
            break
        elif ruby_choice.isdigit() and candidates and 1 <= int(ruby_choice) <= len(candidates):
            selected_ruby = candidates[int(ruby_choice) - 1].version_str
            break
        else:
            # User specified an explicit local version (e.g. 4.0.9)
            selected_ruby = ruby_choice.strip()
            break

    DualLogger.info(f"Selected Ruby runtime: {selected_ruby}")

    # 4. System Identity Configuration
    print(f"\n{AnsiColor.BOLD}4. System User & Shared Group Identity:{AnsiColor.RESET}")
    user_in = input("Enter system username to run Redmine [Default: redmineuser]: ").strip()
    system_user = user_in if user_in else "redmineuser"

    group_in = input(f"Enter shared group name for '{system_user}' [Default: sharedgroup]: ").strip()
    shared_group = group_in if group_in else "sharedgroup"

    DualLogger.info(f"System identity configured: user='{system_user}', group='{shared_group}'")

    # 5. Database Provisioning & Migration Scope
    print(f"\n{AnsiColor.BOLD}5. Database Provisioning & Migration Scope:{AnsiColor.RESET}")
    print("   [1] Production environment only (Recommended)")
    print("   [2] All environments (Production, Development, Test)")
    mig_choice = input("\nSelect database scope [1-2, Default: 1]: ").strip()
    migrate_all = mig_choice == "2"

    # 6. PostgreSQL Database Identity & Credentials
    print(f"\n{AnsiColor.BOLD}6. PostgreSQL Database Configuration:{AnsiColor.RESET}")

    def prompt_db_env(env_label: str, def_user: str, def_db: str) -> tuple[str, str, str]:
        print(f"\n   {AnsiColor.CYAN}[{env_label} Database Configuration]{AnsiColor.RESET}")
        db_in = input(f"   Enter {env_label} database name [Default: {def_db}]: ").strip()
        db_name = db_in if db_in else def_db

        user_in = input(f"   Enter database username for '{db_name}' [Default: {def_user}]: ").strip()
        db_user = user_in if user_in else def_user

        pwd_choice = input(f"   Do you want to enter a custom password for '{db_user}'? [y/N]: ").strip().lower()
        if pwd_choice in ["y", "yes"]:
            while True:
                custom_pwd = input(f"   Enter custom password for '{db_user}': ").strip()
                if custom_pwd:
                    db_pwd = custom_pwd
                    break
                print(f"   {AnsiColor.YELLOW}Password cannot be empty.{AnsiColor.RESET}")
        else:
            alphabet = string.ascii_letters + string.digits
            db_pwd = "".join(secrets.choice(alphabet) for _ in range(24))
            DualLogger.info(f"Generated secure 24-character database password for '{db_user}' ({env_label})")

        return db_name, db_user, db_pwd

    db_prod, user_prod, pwd_prod = prompt_db_env("Production", "redmine", "redmine")

    if migrate_all:
        db_dev, user_dev, pwd_dev = prompt_db_env("Development", "redmine_dev_user", "redmine_dev")
        db_test, user_test, pwd_test = prompt_db_env("Test", "redmine_test_user", "redmine_test")
    else:
        # Auto-generate background defaults for dev & test in database.yml without pestering the user
        alphabet = string.ascii_letters + string.digits
        db_dev, user_dev, pwd_dev = "redmine_dev", "redmine_dev_user", "".join(secrets.choice(alphabet) for _ in range(24))
        db_test, user_test, pwd_test = "redmine_test", "redmine_test_user", "".join(secrets.choice(alphabet) for _ in range(24))

    # 7. PGroonga Full-Text Search Extension
    print(f"\n{AnsiColor.BOLD}7. PostgreSQL Search Extension (PGroonga):{AnsiColor.RESET}")
    pgroonga_choice = input("Enable PGroonga full-text search extension on Redmine databases? [y/N, Default: N]: ").strip().lower()
    enable_pgroonga = pgroonga_choice in ["y", "yes"]

    # 8. Default Language
    print(f"\n{AnsiColor.BOLD}8. Default Seed Language:{AnsiColor.RESET}")
    lang_in = input("Enter default data seed language (e.g., en, de, fr, es) [Default: en]: ").strip().lower()
    default_lang = lang_in if lang_in else "en"

    return InstallParams(
        install_path=install_path,
        system_user=system_user,
        shared_group=shared_group,
        redmine_archive_path=archive_path,
        redmine_version=redmine_ver,
        selected_ruby=selected_ruby,
        rbenv_root=Path(f"/home/{system_user}/.rbenv"),
        db_name_prod=db_prod,
        db_name_dev=db_dev,
        db_name_test=db_test,
        db_user_prod=user_prod,
        db_pass_prod=pwd_prod,
        db_user_dev=user_dev,
        db_pass_dev=pwd_dev,
        db_user_test=user_test,
        db_pass_test=pwd_test,
        enable_pgroonga=enable_pgroonga,
        migrate_all_envs=migrate_all,
        default_lang=default_lang
    )


def main() -> None:
    if not SystemInspector.is_root():
        print(f"{AnsiColor.RED}[ERROR] This installer provisions system users, native packages, and PostgreSQL.")
        print(f"Please run with administrative privileges: sudo python3 setup_redmine_install.py{AnsiColor.RESET}")
        sys.exit(1)

    script_dir = Path(__file__).resolve().parent
    manifest_path = script_dir / "redmine_install_manifest.json"
    log_path = script_dir / "setup_redmine_install.log"

    DualLogger.init(log_path)
    DualLogger.info(f"Installer log stream active: {log_path}")

    try:
        params = prompt_user_installation()
        installer = Installer(params, manifest_path)

        installer.step1_create_user_and_group()
        installer.step2_install_prerequisites()
        installer.step3_configure_ruby()
        installer.step4_unpack_redmine()
        installer.step5_provision_postgresql()
        installer.step6_configure_database_yml()
        installer.step7_bundle_and_migrate()
        installer.step8_precompile_assets()
        installer.step9_enforce_permissions()
        installer.step10_setup_pid_directory()

        installer.export_manifest()

        print(f"\n{AnsiColor.GREEN}{'='*76}")
        print("  REDMINE SECTIONS 0-10 INSTALLATION COMPLETE & VERIFIED")
        print(f"{'='*76}{AnsiColor.RESET}")
        print(f"\nManifest JSON saved: {manifest_path}")
        print(f"To configure systemd services, Puma, and Nginx (Sections 11-19), run:")
        print(f"  sudo python3 {script_dir}/setup_redmine_services.py\n")

    except Exception as e:
        DualLogger.error(f"Installation halted due to error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print(f"\n{AnsiColor.YELLOW}[NOTICE] Installation cancelled by user. Exiting cleanly.{AnsiColor.RESET}\n")
        sys.exit(130)
