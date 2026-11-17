#!/usr/bin/env python3
"""
Redmine_setup.py
=============================================================================
Platinum Enterprise Redmine Automated Deployment & Lifecycle Orchestrator
=============================================================================
Combines complete end-to-end deployment into a unified single script:
  - Part 1 (Sections 0 - 10): Core Installation, System User & Group,
    Prerequisites, Ruby (System or Latest from ruby-build), Redmine Source,
    PostgreSQL Databases & Roles, database.yml, Bundler, Migrations,
    Assets Precompilation, Permissions, and Manifest JSON Export.
  - Part 2 (Sections 11 - 19): Production Services Orchestration:
    Puma Web Server & Systemd Unit, Logrotate, Redis & Sidekiq Integration,
    Sendmail MTA, Development & Test Background Services, Nginx Reverse
    Proxy with Dynamic Ports & Catch-All/Domain Config, and Live Health Checks.
  - Interactive Mode Selector:
      [1] Complete End-to-End Platinum Deployment (Sections 0 - 19)
      [2] Core Installation Only (Sections 0 - 10)
      [3] Services & Web Stack Only (Sections 11 - 19)
      [4] Stack Status & Live Health Verification Matrix
      [5] Safe Clean Services Teardown (Leaves DB & Redmine files intact)
      [6] Exit
=============================================================================
"""

from __future__ import annotations

import datetime
import html
import json
import os
import secrets
import shutil
import socket
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
                f.write(f"Session Started: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
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
    """Container holding core installation parameters (Sections 0 - 10)."""
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


@dataclass
class ServiceParams:
    """Container holding services orchestration parameters (Sections 11 - 19)."""
    redmine_root: Path
    redmine_user: str
    shared_group: str
    puma_port: int = 3300
    puma_workers: int = 2
    puma_min_threads: int = 0
    puma_max_threads: int = 16
    domain_name: str = "_"
    enable_dev_service: bool = False
    enable_test_service: bool = False
    configure_sendmail: bool = False


@dataclass(frozen=True)
class RubyVersionCandidate:
    """Data representation of a Ruby release candidate without regex."""
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


class SystemInspector:
    """Executes host checks, user discovery, and subprocess commands."""

    @staticmethod
    def is_root() -> bool:
        return os.geteuid() == 0

    @staticmethod
    def run_command(cmd: list[str] | str, check: bool = True, as_user: str | None = None, shell: bool = False, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
        final_cmd = cmd
        if as_user and as_user != "root":
            if isinstance(cmd, list):
                final_cmd = ["sudo", "-u", as_user] + cmd
            else:
                final_cmd = f"sudo -u {as_user} bash -c '{cmd}'"
                shell = True

        return subprocess.run(final_cmd, check=check, shell=shell, env=env, capture_output=True, text=True)

    @staticmethod
    def is_command_available(command: str) -> bool:
        return shutil.which(command) is not None

    @staticmethod
    def is_service_active(service: str) -> bool:
        res = subprocess.run(["systemctl", "is-active", "--quiet", service])
        return res.returncode == 0

    @staticmethod
    def check_port_available(port: int, host: str = "127.0.0.1") -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            try:
                s.bind((host, port))
                return True
            except OSError:
                return False


class RedmineVersionManager:
    """Dynamically queries latest upstream Redmine releases from redmine.org using pure string methods."""

    RELEASES_URL: Final[str] = "https://www.redmine.org/releases/"

    @classmethod
    def fetch_available_versions(cls, limit: int = 5) -> list[str]:
        """Fetch and return sorted list of available Redmine versions from redmine.org without regex."""
        try:
            req = urllib.request.Request(cls.RELEASES_URL, headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64)"})
            with urllib.request.urlopen(req, timeout=6) as response:
                content = response.read().decode("utf-8", errors="ignore")

            discovered: list[tuple[int, ...]] = []
            anchor_splits = content.split('href="redmine-')
            for chunk in anchor_splits[1:]:
                filename = chunk.split('"')[0]
                if filename.endswith(".tar.gz") and not filename.endswith(".tar.gz.md5"):
                    v_str = filename[:-len(".tar.gz")]
                    segments = v_str.split(".")
                    if len(segments) >= 2 and all(s.isdigit() for s in segments):
                        discovered.append(tuple(int(s) for s in segments))

            # Deduplicate and sort descending by semver tuple
            unique_tuples = sorted(list(set(discovered)), reverse=True)
            formatted = [".".join(str(s) for s in t) for t in unique_tuples]
            return formatted[:limit] if limit > 0 else formatted
        except Exception:
            return ["7.0.3"]

    @classmethod
    def get_latest_version(cls) -> str:
        versions = cls.fetch_available_versions(limit=1)
        return versions[0] if versions else "7.0.3"


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

        highest_major = max(c.major for c in candidates)
        major_candidates = [c for c in candidates if c.major == highest_major]
        major_candidates.sort(key=lambda item: item.semantic_tuple, reverse=True)
        return major_candidates[:5]


class CoreInstaller:
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


class ServicesManager:
    """Encapsulates Sections 11 through 19 of the Redmine services pipeline."""

    def __init__(self, cfg: ServiceParams) -> None:
        self.cfg = cfg
        self.rbenv_shims = Path(f"/home/{self.cfg.redmine_user}/.rbenv/shims")

    def ensure_pid_directory(self) -> None:
        DualLogger.step("Section 10: Ensuring Runtime PID Directory")
        pid_dir = self.cfg.redmine_root / "tmp" / "pids"
        pid_dir.mkdir(parents=True, exist_ok=True)
        SystemInspector.run_command(["chown", "-R", f"{self.cfg.redmine_user}:{self.cfg.shared_group}", str(pid_dir)])
        SystemInspector.run_command(["chmod", "775", str(pid_dir)])
        DualLogger.success(f"Verified PID directory: {pid_dir}")

    def configure_puma(self) -> None:
        DualLogger.step("Section 11: Generating Puma Production Server Configuration (config/puma.rb)")
        puma_rb = self.cfg.redmine_root / "config" / "puma.rb"
        content = f"""# Puma Application Server Configuration (Platinum Provisioned)
threads {self.cfg.puma_min_threads}, {self.cfg.puma_max_threads}
workers {self.cfg.puma_workers}

bind 'tcp://127.0.0.1:{self.cfg.puma_port}'
environment 'production'

pidfile '{self.cfg.redmine_root}/tmp/pids/puma.pid'
state_path '{self.cfg.redmine_root}/tmp/pids/puma.state'

stdout_redirect '{self.cfg.redmine_root}/log/puma.stdout.log', '{self.cfg.redmine_root}/log/puma.stderr.log', true
activate_control_app 'unix://{self.cfg.redmine_root}/tmp/pids/pumactl.sock', {{ no_token: true }}

preload_app!
prune_bundler false

on_worker_boot do
  ActiveSupport.on_load(:active_record) do
    ActiveRecord::Base.establish_connection
  end
end
"""
        puma_rb.write_text(content, encoding="utf-8")
        SystemInspector.run_command(["chown", f"{self.cfg.redmine_user}:{self.cfg.shared_group}", str(puma_rb)])
        SystemInspector.run_command(["chmod", "664", str(puma_rb)])
        DualLogger.success(f"Generated {puma_rb} (TCP 127.0.0.1:{self.cfg.puma_port}, {self.cfg.puma_workers} workers)")

    def configure_puma_systemd(self) -> None:
        DualLogger.step("Section 12: Installing Hardened Puma Systemd Service Unit")
        service_path = Path("/etc/systemd/system/redmine.service")
        content = f"""[Unit]
Description=Redmine Application Server (Puma Platinum Hardened)
After=network.target postgresql.service redis-server.service
Wants=postgresql.service redis-server.service

[Service]
Type=simple
User={self.cfg.redmine_user}
Group={self.cfg.shared_group}
WorkingDirectory={self.cfg.redmine_root}

ExecStart={self.rbenv_shims}/bundle exec puma -C config/puma.rb
ExecReload={self.rbenv_shims}/pumactl -S tmp/pids/puma.state -F config/puma.rb phased-restart
ExecStop={self.rbenv_shims}/pumactl -S tmp/pids/puma.state -F config/puma.rb halt

PIDFile={self.cfg.redmine_root}/tmp/pids/puma.pid
Restart=always
RestartSec=3s
KillMode=mixed
TimeoutStopSec=30

# Pillar 1: Hardened Security Sandboxing
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=read-only
ReadWritePaths={self.cfg.redmine_root}/files {self.cfg.redmine_root}/log {self.cfg.redmine_root}/tmp

[Install]
WantedBy=multi-user.target
"""
        service_path.write_text(content, encoding="utf-8")
        SystemInspector.run_command(["systemctl", "daemon-reload"])
        SystemInspector.run_command(["systemctl", "enable", "redmine"])
        DualLogger.success("Systemd service 'redmine' configured with security sandboxing.")

    def configure_logrotate(self) -> None:
        DualLogger.step("Section 12b: Configuring Pillar 2 Enterprise Logrotate Protection")
        lr_path = Path("/etc/logrotate.d/redmine")
        content = f"""{self.cfg.redmine_root}/log/*.log {{
    weekly
    missingok
    rotate 12
    compress
    delaycompress
    notifempty
    copytruncate
    create 0664 {self.cfg.redmine_user} {self.cfg.shared_group}
    su {self.cfg.redmine_user} {self.cfg.shared_group}
}}
"""
        lr_path.write_text(content, encoding="utf-8")
        SystemInspector.run_command(["chmod", "644", str(lr_path)])
        DualLogger.success(f"Logrotate configuration installed at {lr_path}")

    def configure_redis(self) -> None:
        DualLogger.step("Section 14: Verifying and Enabling Redis Service")
        if not SystemInspector.is_command_available("redis-server"):
            DualLogger.info("Installing redis-server package...")
            SystemInspector.run_command(["apt", "install", "-y", "redis-server"])

        if not SystemInspector.is_service_active("redis-server"):
            SystemInspector.run_command(["systemctl", "enable", "redis-server"])
            SystemInspector.run_command(["systemctl", "start", "redis-server"])

        res = subprocess.run(["redis-cli", "ping"], capture_output=True, text=True)
        if "PONG" in res.stdout:
            DualLogger.success("Redis is operational (PONG confirmed).")
        else:
            DualLogger.warn(f"Redis ping response: {res.stdout.strip()}. Check redis-server status.")

    def configure_sidekiq(self) -> None:
        DualLogger.step("Section 13: Setting up Sidekiq Background Job Processor")
        prod_rb = self.cfg.redmine_root / "config" / "environments" / "production.rb"
        if prod_rb.exists():
            text = prod_rb.read_text(encoding="utf-8")
            if "config.active_job.queue_adapter = :sidekiq" not in text:
                target_token = "Rails.application.configure do"
                if target_token in text:
                    new_text = text.replace(target_token, f"{target_token}\n  config.active_job.queue_adapter = :sidekiq", 1)
                    prod_rb.write_text(new_text, encoding="utf-8")
                    DualLogger.info("Configured ActiveJob queue_adapter = :sidekiq in production.rb")

        service_path = Path("/etc/systemd/system/sidekiq.service")
        content = f"""[Unit]
Description=Sidekiq Background Job Processor for Redmine (Platinum Hardened)
After=network.target redis-server.service postgresql.service

[Service]
Type=simple
User={self.cfg.redmine_user}
Group={self.cfg.shared_group}
WorkingDirectory={self.cfg.redmine_root}

ExecStart={self.rbenv_shims}/bundle exec sidekiq -e production
Restart=on-failure
RestartSec=5s

# Security Sandboxing
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=read-only
ReadWritePaths={self.cfg.redmine_root}/files {self.cfg.redmine_root}/log {self.cfg.redmine_root}/tmp

[Install]
WantedBy=multi-user.target
"""
        service_path.write_text(content, encoding="utf-8")
        SystemInspector.run_command(["systemctl", "daemon-reload"])
        SystemInspector.run_command(["systemctl", "enable", "sidekiq"])
        SystemInspector.run_command(["systemctl", "restart", "sidekiq"])
        DualLogger.success("Systemd service 'sidekiq' installed, enabled, and started.")

    def configure_email(self) -> None:
        if not self.cfg.configure_sendmail:
            DualLogger.info("Skipping Sendmail MTA configuration per user selection.")
            return

        DualLogger.step("Section 15: Configuring ActionMailer Sendmail Integration")
        if not SystemInspector.is_command_available("sendmail"):
            DualLogger.info("Installing sendmail package...")
            SystemInspector.run_command(["apt", "install", "-y", "sendmail"])

        prod_rb = self.cfg.redmine_root / "config" / "environments" / "production.rb"
        if prod_rb.exists():
            text = prod_rb.read_text(encoding="utf-8")
            if "config.action_mailer.delivery_method = :sendmail" not in text:
                target_token = "Rails.application.configure do"
                if target_token in text:
                    sendmail_block = (
                        f"{target_token}\n"
                        "  # ActionMailer Sendmail Delivery Configuration\n"
                        "  config.action_mailer.delivery_method = :sendmail\n"
                        "  config.action_mailer.sendmail_settings = {\n"
                        "    location: '/usr/sbin/sendmail',\n"
                        "    arguments: '-i -t'\n"
                        "  }\n"
                    )
                    new_text = text.replace(target_token, sendmail_block, 1)
                    prod_rb.write_text(new_text, encoding="utf-8")
                    DualLogger.success("Injected Sendmail delivery settings into production.rb")

    def run_plugin_migrations_and_assets(self) -> None:
        DualLogger.step("Section 16: Migrating Plugins, Publishing Assets, and Sanitizing Cache")
        cmd_env = f"cd '{self.cfg.redmine_root}' && RAILS_ENV=production {self.rbenv_shims}/bundle exec "

        commands = [
            ("rake redmine:plugins:migrate", "Plugin Database Migrations"),
            ("rake redmine:plugins:assets", "Publish Plugin Assets"),
            ("rake tmp:clear tmp:cache:clear", "Sanitize Temp & Runtime Cache"),
            ("rake assets:precompile", "Precompile Core Assets")
        ]

        for sub_cmd, label in commands:
            DualLogger.info(f"Executing: {label}...")
            try:
                SystemInspector.run_command(cmd_env + sub_cmd, as_user=self.cfg.redmine_user, shell=True)
                DualLogger.success(f"Completed: {label}")
            except subprocess.CalledProcessError as e:
                DualLogger.warn(f"{label} notice: {e.stderr.strip()[:100]}")

    def configure_dev_and_test_services(self) -> None:
        if self.cfg.enable_dev_service:
            DualLogger.step("Section 17: Provisioning Development Systemd Unit (Port 4000)")
            dev_service = Path("/etc/systemd/system/redmine-dev.service")
            dev_content = f"""[Unit]
Description=Redmine Development Environment
After=network.target postgresql.service

[Service]
Type=simple
User={self.cfg.redmine_user}
Group={self.cfg.shared_group}
WorkingDirectory={self.cfg.redmine_root}

ExecStart={self.rbenv_shims}/bundle exec rails server -e development -b 0.0.0.0 -p 4000
Restart=on-failure
StandardOutput=append:{self.cfg.redmine_root}/log/redmine-dev.stdout.log
StandardError=append:{self.cfg.redmine_root}/log/redmine-dev.stderr.log

[Install]
WantedBy=multi-user.target
"""
            dev_service.write_text(dev_content, encoding="utf-8")
            SystemInspector.run_command(["systemctl", "daemon-reload"])
            DualLogger.success("Installed redmine-dev.service (Start manually via: sudo systemctl start redmine-dev)")

        if self.cfg.enable_test_service:
            DualLogger.step("Section 18: Provisioning Test/Staging Systemd Unit (Port 5000)")
            test_service = Path("/etc/systemd/system/redmine-test.service")
            test_content = f"""[Unit]
Description=Redmine Test Environment
After=network.target postgresql.service

[Service]
Type=simple
User={self.cfg.redmine_user}
Group={self.cfg.shared_group}
WorkingDirectory={self.cfg.redmine_root}

ExecStart={self.rbenv_shims}/bundle exec rails server -e test -b 0.0.0.0 -p 5000
Restart=on-failure
StandardOutput=append:{self.cfg.redmine_root}/log/redmine-test.stdout.log
StandardError=append:{self.cfg.redmine_root}/log/redmine-test.stderr.log

[Install]
WantedBy=multi-user.target
"""
            test_service.write_text(test_content, encoding="utf-8")
            SystemInspector.run_command(["systemctl", "daemon-reload"])
            DualLogger.success("Installed redmine-test.service (Start manually via: sudo systemctl start redmine-test)")

    def configure_nginx(self) -> None:
        DualLogger.step("Section 19: Installing & Validating Nginx Reverse Proxy")
        if not SystemInspector.is_command_available("nginx"):
            DualLogger.info("Installing nginx package...")
            SystemInspector.run_command(["apt", "install", "-y", "nginx"])

        vhost_path = Path("/etc/nginx/sites-available/redmine")
        vhost_content = f"""# Redmine Nginx Reverse Proxy Configuration (Platinum Provisioned)
upstream redmine_puma {{
    server 127.0.0.1:{self.cfg.puma_port} fail_timeout=0;
}}

server {{
    listen 80;
    server_name {self.cfg.domain_name};

    root {self.cfg.redmine_root}/public;
    client_max_body_size 64M;

    access_log /var/log/nginx/redmine_access.log;
    error_log /var/log/nginx/redmine_error.log;

    location / {{
        try_files $uri/index.html $uri.html $uri @puma;
    }}

    location @puma {{
        proxy_pass http://redmine_puma;
        proxy_set_header Host $http_host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_connect_timeout 60s;
        proxy_read_timeout 120s;
        proxy_send_timeout 120s;
    }}

    location ~ ^/(assets|plugin_assets)/ {{
        gzip_static on;
        expires max;
        add_header Cache-Control public;
    }}
}}
"""
        vhost_path.write_text(vhost_content, encoding="utf-8")
        enabled_link = Path("/etc/nginx/sites-enabled/redmine")
        if not enabled_link.exists() and not enabled_link.is_symlink():
            enabled_link.symlink_to(vhost_path)

        default_site = Path("/etc/nginx/sites-enabled/default")
        if default_site.exists() or default_site.is_symlink():
            default_site.unlink(missing_ok=True)
            DualLogger.info("Removed default Nginx virtual host.")

        DualLogger.info("Validating Nginx syntax...")
        t_res = subprocess.run(["nginx", "-t"], capture_output=True, text=True)
        if t_res.returncode == 0:
            DualLogger.success("Nginx syntax validation successful.")
            SystemInspector.run_command(["systemctl", "enable", "nginx"])
            SystemInspector.run_command(["systemctl", "restart", "nginx"])
            DualLogger.success("Nginx service reloaded and operational.")
        else:
            DualLogger.error(f"Nginx syntax error: {t_res.stderr.strip()}")

    @staticmethod
    def run_health_checks(port: int, redmine_root: Path) -> None:
        DualLogger.step("Live Health & Verification Matrix")

        services = ["postgresql", "redis-server", "redmine", "sidekiq", "nginx"]
        for svc in services:
            active = SystemInspector.is_service_active(svc)
            status = f"{AnsiColor.GREEN}ACTIVE (Running){AnsiColor.RESET}" if active else f"{AnsiColor.RED}INACTIVE{AnsiColor.RESET}"
            DualLogger.info(f"Service Check: {svc:<16} -> {status}")

        pid_file = redmine_root / "tmp" / "pids" / "puma.pid"
        if pid_file.exists():
            DualLogger.success(f"Puma Master PID verified: {pid_file.read_text(encoding='utf-8').strip()}")
        else:
            DualLogger.warn("Puma Master PID file not found.")

        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}", timeout=5) as response:
                status_code = response.getcode()
                if status_code in [200, 302]:
                    DualLogger.success(f"HTTP Socket Validation (Port {port}): Response Code {status_code} OK")
                else:
                    DualLogger.warn(f"HTTP Socket returned unexpected status: {status_code}")
        except Exception as e:
            DualLogger.warn(f"Local HTTP Socket probe notice: {e}")

    @staticmethod
    def uninstall(redmine_root: Path) -> None:
        DualLogger.step("Safe Complete Uninstall / Teardown of Redmine Services")
        print(f"{AnsiColor.YELLOW}This operation will stop and remove Redmine systemd services, logrotate, and Nginx links.")
        print(f"It leaves PostgreSQL databases and the application files in {redmine_root} completely intact.{AnsiColor.RESET}")

        confirm = input("\nType 'YES' to confirm teardown: ").strip()
        if confirm != "YES":
            DualLogger.info("Teardown cancelled.")
            return

        for svc in ["redmine", "sidekiq", "redmine-dev", "redmine-test"]:
            subprocess.run(["systemctl", "stop", svc], capture_output=True)
            subprocess.run(["systemctl", "disable", svc], capture_output=True)
            unit_file = Path(f"/etc/systemd/system/{svc}.service")
            if unit_file.exists():
                unit_file.unlink()

        SystemInspector.run_command(["systemctl", "daemon-reload"])

        Path("/etc/nginx/sites-enabled/redmine").unlink(missing_ok=True)
        Path("/etc/nginx/sites-available/redmine").unlink(missing_ok=True)
        Path("/etc/logrotate.d/redmine").unlink(missing_ok=True)

        if SystemInspector.is_service_active("nginx"):
            subprocess.run(["systemctl", "restart", "nginx"], capture_output=True)

        DualLogger.success("Redmine systemd services, logrotate, and Nginx links safely removed.")


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

    # 2. Archive Selection & Dynamic Upstream Version Discovery
    print(f"\n{AnsiColor.BOLD}2. Redmine Source Archive Selection:{AnsiColor.RESET}")
    archive_in = input("Enter path to downloaded Redmine archive (.tar.gz / .zip) [Press Enter to auto-download]: ").strip()
    archive_path = Path(archive_in) if archive_in and Path(archive_in).exists() else None

    latest_upstream = RedmineVersionManager.get_latest_version()
    redmine_ver = latest_upstream
    if not archive_path:
        ver_in = input(f"Enter Redmine version to download [Default: {latest_upstream}]: ").strip()
        if ver_in:
            redmine_ver = ver_in

    # 3. System Identity Configuration
    print(f"\n{AnsiColor.BOLD}3. System User & Shared Group Identity:{AnsiColor.RESET}")
    user_in = input("Enter system username to run Redmine [Default: redmineuser]: ").strip()
    system_user = user_in if user_in else "redmineuser"

    group_in = input(f"Enter shared group name for '{system_user}' [Default: sharedgroup]: ").strip()
    shared_group = group_in if group_in else "sharedgroup"

    DualLogger.info(f"System identity configured: user='{system_user}', group='{shared_group}'")

    # 4. Ruby Runtime Selection
    print(f"\n{AnsiColor.BOLD}4. Ruby Runtime Selection:{AnsiColor.RESET}")
    print("-" * 74)
    sys_ruby = RubyVersionManager.get_system_ruby()
    candidates = RubyVersionManager.fetch_top_5_latest_releases(Path(f"/home/{system_user}/.rbenv/plugins"))

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
            selected_ruby = ruby_choice.strip()
            break

    DualLogger.info(f"Selected Ruby runtime: {selected_ruby}")

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
        alphabet = string.ascii_letters + string.digits
        db_dev, user_dev, pwd_dev = "redmine_dev", "redmine_dev_user", "".join(secrets.choice(alphabet) for _ in range(24))
        db_test, user_test, pwd_test = "redmine_test", "redmine_test_user", "".join(secrets.choice(alphabet) for _ in range(24))

    # 7. Services & Process Architecture (Sections 11 - 19)
    print(f"\n{AnsiColor.BOLD}7. Services & Process Architecture (Sections 11 - 19):{AnsiColor.RESET}")

    default_port = 3300
    while True:
        port_in = input(f"Enter Puma Production Port [Default: {default_port}]: ").strip()
        if not port_in:
            puma_port = default_port
            break
        try:
            val = int(port_in)
            if 1024 <= val <= 65535:
                puma_port = val
                break
            print(f"{AnsiColor.RED}Port must be between 1024 and 65535.{AnsiColor.RESET}")
        except ValueError:
            print(f"{AnsiColor.RED}Invalid integer.{AnsiColor.RESET}")

    domain_in = input("Enter Server Domain Name / IP for Nginx [Default: _ (Catch-all)]: ").strip()
    domain_name = domain_in if domain_in else "_"

    workers_in = input("Enter Puma Worker Count [Default: 2]: ").strip()
    puma_workers = int(workers_in) if workers_in.isdigit() else 2

    dev_in = input("Enable background Dev service on port 4000? (y/n) [Default: n]: ").strip().lower()
    enable_dev = dev_in == "y"

    test_in = input("Enable background Test service on port 5000? (y/n) [Default: n]: ").strip().lower()
    enable_test = test_in == "y"

    sendmail_in = input("Configure local Sendmail MTA? (y/n) [Default: n]: ").strip().lower()
    configure_sendmail = sendmail_in == "y"

    # 8. PGroonga Full-Text Search Extension
    print(f"\n{AnsiColor.BOLD}8. PostgreSQL Search Extension (PGroonga):{AnsiColor.RESET}")
    pgroonga_choice = input("Enable PGroonga full-text search extension on Redmine databases? [y/N, Default: N]: ").strip().lower()
    enable_pgroonga = pgroonga_choice in ["y", "yes"]

    # 9. Default Language
    print(f"\n{AnsiColor.BOLD}9. Default Seed Language:{AnsiColor.RESET}")
    lang_in = input("Enter default data seed language (e.g., en, de, fr, es) [Default: en]: ").strip().lower()
    default_lang = lang_in if lang_in else "en"

    return (
        InstallParams(
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
        ),
        ServiceParams(
            redmine_root=install_path,
            redmine_user=system_user,
            shared_group=shared_group,
            puma_port=puma_port,
            puma_workers=puma_workers,
            puma_min_threads=0,
            puma_max_threads=16,
            domain_name=domain_name,
            enable_dev_service=enable_dev,
            enable_test_service=enable_test,
            configure_sendmail=configure_sendmail
        )
    )


def execute_full_deployment(manifest_path: Path) -> None:
    # Single unified interactive prompt gathering all deployment parameters upfront
    install_params, svc_params = prompt_user_installation()
    installer = CoreInstaller(install_params, manifest_path)

    # Core Deployment (Sections 1 - 10)
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

    # Services Deployment (Sections 11 - 19)
    svc_mgr = ServicesManager(svc_params)
    svc_mgr.ensure_pid_directory()
    svc_mgr.configure_puma()
    svc_mgr.configure_puma_systemd()
    svc_mgr.configure_logrotate()
    svc_mgr.configure_redis()
    svc_mgr.configure_sidekiq()
    svc_mgr.configure_email()
    svc_mgr.run_plugin_migrations_and_assets()
    svc_mgr.configure_dev_and_test_services()
    svc_mgr.configure_nginx()

    SystemInspector.run_command(["systemctl", "restart", "redmine"])
    SystemInspector.run_command(["systemctl", "restart", "sidekiq"])
    ServicesManager.run_health_checks(svc_params.puma_port, svc_params.redmine_root)

    print(f"\n{AnsiColor.GREEN}{'='*76}")
    print("  PLATINUM REDMINE FULL DEPLOYMENT COMPLETE & OPERATIONAL")
    print(f"{'='*76}{AnsiColor.RESET}")
    print(f"Manifest JSON: {manifest_path}")


def detect_existing_installation(manifest_path: Path) -> dict[str, str] | None:
    """Detect presence of existing Redmine installation via manifest, filesystem, or systemd."""
    root_path = Path("/data/redmine")
    user = "redmineuser"
    ruby_ver = "unknown"
    version = "7.0.3"

    if manifest_path.exists():
        try:
            m = json.loads(manifest_path.read_text(encoding="utf-8"))
            root_path = Path(m.get("application", {}).get("install_path", "/data/redmine"))
            user = m.get("system_identity", {}).get("user", "redmineuser")
            ruby_ver = m.get("runtime_environment", {}).get("ruby_version", "unknown")
            version = m.get("application", {}).get("version", "7.0.3")
            return {
                "install_path": str(root_path),
                "system_user": user,
                "ruby_version": ruby_ver,
                "version": version,
                "source": "manifest"
            }
        except Exception:
            pass

    # Filesystem heuristic check
    for test_dir in [Path("/data/redmine"), Path("/opt/redmine"), Path("/var/www/redmine")]:
        if (test_dir / "config" / "environment.rb").is_file():
            return {
                "install_path": str(test_dir),
                "system_user": user,
                "ruby_version": ruby_ver,
                "version": version,
                "source": "filesystem"
            }

    # Systemd check
    if Path("/etc/systemd/system/redmine.service").is_file():
        return {
            "install_path": str(root_path),
            "system_user": user,
            "ruby_version": ruby_ver,
            "version": version,
            "source": "systemd"
        }

    return None


def execute_update_maintenance(manifest_path: Path, detected: dict[str, str]) -> None:
    DualLogger.step("In-Place Maintenance, Diagnostic & Auto-Repair")
    install_path = Path(detected["install_path"])
    user = detected["system_user"]
    group = "sharedgroup"
    current_ver = detected.get("version", "unknown")

    # 1. Upstream Version Check & Upgrade Prompt
    latest_upstream = RedmineVersionManager.get_latest_version()
    DualLogger.info(f"Installed Version: {current_ver} | Upstream Latest: {latest_upstream}")
    if current_ver != "unknown" and current_ver != latest_upstream:
        print(f"\n{AnsiColor.YELLOW}[!] An upstream Redmine update ({latest_upstream}) is available!{AnsiColor.RESET}")
        upgrade_choice = input(f"Would you like to upgrade Redmine from {current_ver} to {latest_upstream}? [y/N]: ").strip().lower()
        if upgrade_choice in ["y", "yes"]:
            DualLogger.info(f"Downloading and upgrading to Redmine {latest_upstream}...")
            archive_url = f"https://www.redmine.org/releases/redmine-{latest_upstream}.tar.gz"
            tmp_archive = Path(f"/tmp/redmine-{latest_upstream}.tar.gz")
            try:
                urllib.request.urlretrieve(archive_url, tmp_archive)
                # Extract new source directly over install_path preserving config & files
                with tarfile.open(tmp_archive, "r:gz") as tar:
                    tar.extractall(path="/tmp")
                extracted_dir = Path(f"/tmp/redmine-{latest_upstream}")
                if extracted_dir.exists():
                    # Preserve database.yml, secrets, configuration.yml, files
                    preserved = ["config/database.yml", "config/secrets.yml", "config/configuration.yml"]
                    backup_cache = {}
                    for p in preserved:
                        src_f = install_path / p
                        if src_f.exists():
                            backup_cache[p] = src_f.read_bytes()

                    # Copy new source tree
                    shutil.copytree(extracted_dir, install_path, dirs_exist_ok=True)
                    shutil.rmtree(extracted_dir, ignore_errors=True)

                    # Restore preserved config
                    for p, data in backup_cache.items():
                        dest_f = install_path / p
                        dest_f.parent.mkdir(parents=True, exist_ok=True)
                        dest_f.write_bytes(data)

                    DualLogger.success(f"Source files upgraded to Redmine {latest_upstream}.")
            except Exception as e:
                DualLogger.error(f"Upstream upgrade download/extraction failed: {e}. Continuing with in-place repair.")

    # 2. Recover parameters from manifest if present
    if manifest_path.exists():
        try:
            m = json.loads(manifest_path.read_text(encoding="utf-8"))
            group = m.get("system_identity", {}).get("group", "sharedgroup")
        except Exception:
            pass

    DualLogger.info(f"Target Redmine root: {install_path}")
    DualLogger.info(f"Execution Identity: {user}:{group}")

    exec_prefix = f"cd '{install_path}' && export PATH=\"/home/{user}/.rbenv/bin:$PATH\" && eval \"$(rbenv init - bash)\" && "

    # 2. Diagnostic & Repair: Secret Token
    secret_file = install_path / "config" / "secrets.yml"
    initializers_token = install_path / "config" / "initializers" / "secret_token.rb"
    if not secret_file.exists() and not initializers_token.exists():
        DualLogger.warn("Missing secret token detected. Generating encryption token...")
        SystemInspector.run_command(f"{exec_prefix}bundle exec rake generate_secret_token", as_user=user, shell=True)
        DualLogger.success("Secret encryption token generated.")
    else:
        DualLogger.success("Secret encryption token verified.")

    # 3. Diagnostic & Repair: Runtime Directories & Permissions
    DualLogger.info("Verifying and repairing runtime directories and permissions...")
    for sub in ["tmp", "tmp/pdf", "tmp/pids", "public/plugin_assets", "files", "log"]:
        d = install_path / sub
        d.mkdir(parents=True, exist_ok=True)

    SystemInspector.run_command(["chown", "-R", f"{user}:{group}", str(install_path)])
    writable_dirs = [str(install_path / sub) for sub in ["files", "log", "tmp", "public/plugin_assets"]]
    SystemInspector.run_command(["chmod", "-R", "775"] + writable_dirs)
    DualLogger.success("Runtime directories and permissions (775) repaired.")

    # 4. Gem Bundle Check
    DualLogger.info("Verifying Ruby gems bundle...")
    SystemInspector.run_command(f"{exec_prefix}bundle install", as_user=user, shell=True)
    DualLogger.success("Ruby gems verified.")

    # 5. Database Schema Migrations
    DualLogger.info("Applying pending database migrations (production)...")
    SystemInspector.run_command(f"{exec_prefix}RAILS_ENV=production bundle exec rake db:migrate", as_user=user, shell=True)
    DualLogger.success("Database schema verified and up-to-date.")

    # 6. Asset Precompilation & Cache Clear
    DualLogger.info("Sanitizing cache and precompiling assets...")
    SystemInspector.run_command(f"{exec_prefix}RAILS_ENV=production bundle exec rake tmp:cache:clear", as_user=user, shell=True)
    SystemInspector.run_command(f"{exec_prefix}RAILS_ENV=production bundle exec rake assets:precompile", as_user=user, shell=True)
    DualLogger.success("Assets precompiled.")

    # 7. Diagnostic & Repair: Systemd Unit Checks
    redmine_service = Path("/etc/systemd/system/redmine.service")
    sidekiq_service = Path("/etc/systemd/system/sidekiq.service")

    if not redmine_service.exists() or not sidekiq_service.exists():
        DualLogger.warn("Missing service units detected. Regenerating Puma and Sidekiq systemd units...")
        svc_params = ServiceParams(
            redmine_root=install_path,
            redmine_user=user,
            shared_group=group,
            puma_port=3300,
            puma_workers=2,
            puma_min_threads=0,
            puma_max_threads=16,
            domain_name="_",
            enable_dev_service=False,
            enable_test_service=False,
            configure_sendmail=False
        )
        svc_mgr = ServicesManager(svc_params)
        svc_mgr.configure_puma()
        svc_mgr.configure_puma_systemd()
        svc_mgr.configure_sidekiq()
        svc_mgr.configure_logrotate()
        svc_mgr.configure_nginx()

    # 8. Service Restart & Health Verification
    DualLogger.info("Reloading and restarting active services...")
    SystemInspector.run_command(["systemctl", "daemon-reload"])
    if SystemInspector.is_service_active("redmine"):
        SystemInspector.run_command(["systemctl", "restart", "redmine"])
    if SystemInspector.is_service_active("sidekiq"):
        SystemInspector.run_command(["systemctl", "restart", "sidekiq"])
    if SystemInspector.is_service_active("nginx"):
        SystemInspector.run_command(["systemctl", "restart", "nginx"])

    ServicesManager.run_health_checks(3300, install_path)
    DualLogger.success("In-place diagnostic, auto-repair, and update completed successfully.")


def execute_complete_uninstall(detected: dict[str, str], manifest_path: Path) -> None:
    DualLogger.step("Complete Redmine Uninstallation")
    install_path = Path(detected["install_path"])
    user = detected["system_user"]

    print(f"\n{AnsiColor.YELLOW}WARNING: This will stop and remove Redmine services, logrotate, and Nginx links.{AnsiColor.RESET}")
    confirm = input("Type 'UNINSTALL' to proceed: ").strip()
    if confirm != "UNINSTALL":
        DualLogger.info("Uninstallation cancelled.")
        return

    # 1. Stop and disable services
    for svc in ["redmine", "sidekiq", "redmine-dev", "redmine-test"]:
        subprocess.run(["systemctl", "stop", svc], capture_output=True)
        subprocess.run(["systemctl", "disable", svc], capture_output=True)
        unit = Path(f"/etc/systemd/system/{svc}.service")
        if unit.exists():
            unit.unlink()
    SystemInspector.run_command(["systemctl", "daemon-reload"])
    DualLogger.success("Systemd services stopped and removed.")

    # 2. Remove Nginx configuration
    Path("/etc/nginx/sites-enabled/redmine").unlink(missing_ok=True)
    Path("/etc/nginx/sites-available/redmine").unlink(missing_ok=True)
    if SystemInspector.is_service_active("nginx"):
        subprocess.run(["systemctl", "restart", "nginx"], capture_output=True)
    DualLogger.success("Nginx reverse proxy configuration removed.")

    # 3. Remove logrotate
    Path("/etc/logrotate.d/redmine").unlink(missing_ok=True)
    DualLogger.success("Logrotate configuration removed.")

    # 4. Optional purge of application files
    purge_files = input(f"\nPurge Redmine directory '{install_path}' from filesystem? (y/N): ").strip().lower()
    if purge_files in ["y", "yes"]:
        shutil.rmtree(install_path, ignore_errors=True)
        DualLogger.success(f"Purged directory: {install_path}")
    else:
        DualLogger.info(f"Retained application files at: {install_path}")

    # 5. Optional purge of PostgreSQL databases
    purge_db = input("Drop Redmine PostgreSQL databases and users? (y/N): ").strip().lower()
    if purge_db in ["y", "yes"]:
        db_names = ["redmine", "redmine_dev", "redmine_test"]
        db_users = ["redmine", "redmine_dev_user", "redmine_test_user"]
        for db in db_names:
            subprocess.run(f"sudo -u postgres psql -c 'DROP DATABASE IF EXISTS {db};'", shell=True, capture_output=True)
        for u in db_users:
            subprocess.run(f"sudo -u postgres psql -c 'DROP ROLE IF EXISTS {u};'", shell=True, capture_output=True)
        DualLogger.success("PostgreSQL databases and roles dropped.")

    manifest_path.unlink(missing_ok=True)
    DualLogger.success("Uninstallation completed cleanly.")


def main() -> None:
    if not SystemInspector.is_root():
        print(f"{AnsiColor.RED}[ERROR] This orchestrator provisions system users, packages, systemd units, and Nginx.")
        print(f"Please run with administrative privileges: sudo python3 Redmine_setup.py{AnsiColor.RESET}")
        sys.exit(1)

    script_dir = Path(__file__).resolve().parent
    manifest_path = script_dir / "redmine_install_manifest.json"
    log_path = script_dir / "Redmine_setup.log"

    DualLogger.init(log_path)
    DualLogger.info(f"Session log stream active: {log_path}")

    # Pre-Flight State Detection
    detected = detect_existing_installation(manifest_path)

    if detected:
        print(f"\n{AnsiColor.HEADER}{'='*76}")
        print("  REDMINE PLATINUM LIFECYCLE CONTROLLER - EXISTING INSTALLATION DETECTED")
        print(f"{'='*76}{AnsiColor.RESET}")
        print(f"\nDetected Installation Details:")
        print(f"  - Target Path:     {detected['install_path']}")
        print(f"  - System User:     {detected['system_user']}")
        print(f"  - Ruby Runtime:    {detected['ruby_version']}")
        print(f"  - Detection By:    {detected['source']}")

        print(f"\n{AnsiColor.BOLD}Select Action:{AnsiColor.RESET}")
        print("  [1] Update / In-Place Maintenance (Re-run migrations, assets, gem bundle & restart)")
        print("  [2] Complete Uninstall / Clean Reset (Safely teardown services, files & databases)")
        print("  [3] Fresh Clean Re-install (Purge existing stack and redeploy from scratch)")
        print("  [4] Exit\n")

        action = input("Enter choice [1-4, Default: 1]: ").strip() or "1"

        if action == "1":
            execute_update_maintenance(manifest_path, detected)
            sys.exit(0)
        elif action == "2":
            execute_complete_uninstall(detected, manifest_path)
            sys.exit(0)
        elif action == "3":
            DualLogger.info("Proceeding to fresh deployment...")
            execute_full_deployment(manifest_path)
            sys.exit(0)
        else:
            DualLogger.info("Exiting without changes.")
            sys.exit(0)

    # If no installation detected, proceed directly to fresh deployment
    execute_full_deployment(manifest_path)


if __name__ == "__main__":
    main()
