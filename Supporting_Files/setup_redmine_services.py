#!/usr/bin/env python3
"""
setup_redmine_services.py
=============================================================================
Platinum Enterprise Redmine Production Services Automation Orchestrator
=============================================================================
Architecture & Lifecycle:
  - Dual-Stream Persistent Logging (/data/redmine/log/setup_redmine_services.log)
  - Pre-Flight Installation State Detection (Clean, Configured, Partial)
  - Interactive Lifecycle Menu:
      [1] Fresh Install / Deploy (Sections 11 - 19)
      [2] In-Place Reconfigure (Update Ports, Workers, Domain, Services)
      [3] Stack Status & Live Health Check Matrix
      [4] Safe Clean Uninstall / Reset Services (Leaves DB & files intact)
      [5] Exit
  - Pillar 1: Hardened Systemd Security Sandboxing (ProtectSystem, NoNewPrivileges, PrivateTmp)
  - Pillar 2: Logrotate & Disk Space Protection (/etc/logrotate.d/redmine)
  - Pillar 4: Zero-Downtime Rolling Worker Restarts (Puma Phased Restart on ExecReload)
  - Port Collision & Socket Verification
  - Idempotent Redis & Sendmail Verification
  - Automated Nginx Reverse Proxy & Syntax Validation
=============================================================================
"""

from __future__ import annotations

import datetime
import os
import pty
import select
import shutil
import socket
import subprocess
import sys
from dataclasses import dataclass
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
    """Tee logger printing to stdout with ANSI colors while logging clean text to disk."""

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
            print(f"{AnsiColor.YELLOW}[WARN] Could not initialize log file at {log_path}: {e}{AnsiColor.RESET}")

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
class ServiceConfig:
    """Runtime configuration container for Redmine production services."""
    redmine_root: Path
    redmine_user: str
    shared_group: str
    puma_port: int
    puma_workers: int
    puma_min_threads: int
    puma_max_threads: int
    domain_name: str
    enable_dev_service: bool
    enable_test_service: bool
    configure_sendmail: bool
    enable_ssl: bool = True
    ssl_cert_path: Path = Path("/etc/ssl/redmine/redmine.crt")
    ssl_key_path: Path = Path("/etc/ssl/redmine/redmine.key")


class SystemInspector:
    """Validates OS privileges, sockets, processes, and service states."""

    @staticmethod
    def is_root() -> bool:
        return os.geteuid() == 0

    @staticmethod
    def check_port_available(port: int, host: str = "127.0.0.1") -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(1.0)
            result = sock.connect_ex((host, port))
            return result != 0

    @staticmethod
    def run_command(cmd: list[str] | str, check: bool = True, as_user: str | None = None, shell: bool = False, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
        sub_env = (env.copy() if env else os.environ.copy())
        sub_env["TERM"] = "dumb"
        sub_env["CI"] = "true"

        try:
            if as_user and as_user != os.environ.get("USER", ""):
                if isinstance(cmd, list):
                    cmd_str = " ".join(f"'{arg}'" if " " in arg or "$" in arg else arg for arg in cmd)
                else:
                    cmd_str = cmd
                full_cmd = ["sudo", "-u", as_user, "env", "TERM=dumb", "CI=true", "bash", "-c", cmd_str]
                res = subprocess.run(full_cmd, check=check, text=True, env=sub_env)
                DualLogger.write_disk(f"Command (as {as_user}): {cmd_str} -> ReturnCode: {res.returncode}")
                return res

            if isinstance(cmd, str) and not shell:
                import shlex
                cmd = shlex.split(cmd)

            res = subprocess.run(cmd, check=check, text=True, shell=shell, env=sub_env)
            DualLogger.write_disk(f"Command: {cmd} -> ReturnCode: {res.returncode}")
            return res
        except subprocess.CalledProcessError as e:
            if e.returncode in [1, 130]:
                raise KeyboardInterrupt from e
            raise

    @staticmethod
    def is_service_active(service_name: str) -> bool:
        res = subprocess.run(["systemctl", "is-active", "--quiet", service_name])
        return res.returncode == 0

    @staticmethod
    def is_service_installed(service_name: str) -> bool:
        unit_file = Path(f"/etc/systemd/system/{service_name}.service")
        return unit_file.exists()

    @staticmethod
    def is_command_available(command: str) -> bool:
        return shutil.which(command) is not None

    @staticmethod
    def detect_existing_setup(redmine_root: Path) -> dict[str, bool]:
        """Detect presence of Section 11-19 production components."""
        return {
            "puma_config": (redmine_root / "config" / "puma.rb").exists(),
            "redmine_service": Path("/etc/systemd/system/redmine.service").exists(),
            "sidekiq_service": Path("/etc/systemd/system/sidekiq.service").exists(),
            "nginx_vhost": Path("/etc/nginx/sites-available/redmine").exists(),
            "nginx_enabled": Path("/etc/nginx/sites-enabled/redmine").exists(),
            "logrotate_config": Path("/etc/logrotate.d/redmine").exists(),
            "dev_service": Path("/etc/systemd/system/redmine-dev.service").exists(),
            "test_service": Path("/etc/systemd/system/redmine-test.service").exists(),
            "redis_active": SystemInspector.is_service_active("redis-server"),
        }


class ServiceManager:
    """Handles provisioning, reconfiguring, and clean uninstallation of services."""

    def __init__(self, cfg: ServiceConfig) -> None:
        self.cfg = cfg
        self.bundle_bin = self._resolve_bundle_bin()

    def _resolve_bundle_bin(self) -> str:
        """Resolve bundle binary path dynamically across rbenv, local binstubs, or system ruby."""
        rbenv_bundle = Path(f"/home/{self.cfg.redmine_user}/.rbenv/shims/bundle")
        if rbenv_bundle.exists():
            return str(rbenv_bundle)
        binstub = self.cfg.redmine_root / "bin" / "bundle"
        if binstub.exists():
            return str(binstub)
        sys_bundle = shutil.which("bundle")
        if sys_bundle:
            return sys_bundle
        return "/usr/local/bin/bundle"

    def ensure_pid_directory(self) -> None:
        pid_dir = self.cfg.redmine_root / "tmp" / "pids"
        pid_dir.mkdir(parents=True, exist_ok=True)
        SystemInspector.run_command(["chown", "-R", f"{self.cfg.redmine_user}:{self.cfg.shared_group}", str(pid_dir)])
        SystemInspector.run_command(["chmod", "775", str(pid_dir)])
        DualLogger.success(f"PID directory configured: {pid_dir}")

    def configure_puma(self) -> None:
        DualLogger.step("Section 11: Generating Puma Application Server Configuration")

        # Ensure Gemfile.local has enterprise dependencies and run bundle install
        gemfile_local = self.cfg.redmine_root / "Gemfile.local"
        required_gems = ["puma", "sidekiq", "observer", "slim-rails", "xapian-ruby"]
        existing_content = gemfile_local.read_text(encoding="utf-8") if gemfile_local.exists() else ""
        gems_to_add = [g for g in required_gems if f"'{g}'" not in existing_content and f'"{g}"' not in existing_content]
        if gems_to_add:
            DualLogger.info(f"Adding enterprise gems to Gemfile.local: {', '.join(gems_to_add)}")
            addition = "\n".join(f"gem '{g}'" for g in gems_to_add) + "\n"
            gemfile_local.write_text(existing_content + ("\n" if existing_content and not existing_content.endswith("\n") else "") + addition, encoding="utf-8")
            SystemInspector.run_command(["chown", f"{self.cfg.redmine_user}:{self.cfg.shared_group}", str(gemfile_local)])
            SystemInspector.run_command(["chmod", "664", str(gemfile_local)])

        # Check if bundle is already satisfied so we avoid redundant network checks
        rbenv_shims = f"/home/{self.cfg.redmine_user}/.rbenv/shims"
        check_cmd = f"cd '{self.cfg.redmine_root}' && {rbenv_shims}/bundle check"
        res_check = SystemInspector.run_command(check_cmd, check=False, as_user=self.cfg.redmine_user, shell=True)
        if res_check.returncode == 0:
            DualLogger.success("All Ruby gems already satisfied locally. Preserving existing gems.")
        else:
            r_res = subprocess.run(f"sudo -u {self.cfg.redmine_user} env TERM=dumb CI=true bash -c '{rbenv_shims}/ruby -v'", shell=True, capture_output=True, text=True)
            ruby_ver = r_res.stdout.strip() if r_res.stdout else "Unknown"
            DualLogger.info(f"Target User: {self.cfg.redmine_user} | Ruby Runtime: {ruby_ver}")
            DualLogger.info("Resolving and installing enterprise gems via bundle install...")
            bundle_cmd = f"cd '{self.cfg.redmine_root}' && {rbenv_shims}/bundle config set without 'development test' && {rbenv_shims}/bundle install"
            SystemInspector.run_command(bundle_cmd, as_user=self.cfg.redmine_user, shell=True)
            DualLogger.success("Bundler resolved and installed required gems successfully.")

        puma_config_path = self.cfg.redmine_root / "config" / "puma.rb"
        content = f"""# Puma Application Server Configuration (Platinum Orchestrated)
directory '{self.cfg.redmine_root}'
rackup "{self.cfg.redmine_root}/config.ru"
environment 'production'

pidfile "{self.cfg.redmine_root}/tmp/pids/puma.pid"
state_path "{self.cfg.redmine_root}/tmp/pids/puma.state"
stdout_redirect "{self.cfg.redmine_root}/log/puma.stdout.log", "{self.cfg.redmine_root}/log/puma.stderr.log", true

threads {self.cfg.puma_min_threads}, {self.cfg.puma_max_threads}
workers {self.cfg.puma_workers}
bind 'tcp://127.0.0.1:{self.cfg.puma_port}'

# Preload application for memory deduplication & fast worker spawning
preload_app!

# Zero-downtime rolling worker restart hook
prune_bundler

on_worker_boot do
  ActiveSupport.on_load(:active_record) do
    ActiveRecord::Base.establish_connection
  end
end
"""
        puma_config_path.write_text(content, encoding="utf-8")
        SystemInspector.run_command(["chown", f"{self.cfg.redmine_user}:{self.cfg.shared_group}", str(puma_config_path)])
        SystemInspector.run_command(["chmod", "664", str(puma_config_path)])
        DualLogger.success(f"Puma configured to bind on 127.0.0.1:{self.cfg.puma_port} ({puma_config_path})")

    def configure_puma_systemd(self) -> None:
        """Section 12: Generate, sandbox, and enable /etc/systemd/system/redmine.service."""
        DualLogger.step("Section 12: Installing Hardened Puma Systemd Unit Service")
        service_path = Path("/etc/systemd/system/redmine.service")
        
        # Hardened Platinum Unit with Sandboxing & Phased Rolling Restarts
        content = f"""[Unit]
Description=Redmine 7.0.3 Puma Application Server (Platinum Hardened)
After=network.target postgresql.service redis-server.service

[Service]
Type=simple
User={self.cfg.redmine_user}
Group={self.cfg.shared_group}
WorkingDirectory={self.cfg.redmine_root}

# Pre-execution runtime directories & permissions
ExecStartPre=/bin/mkdir -p {self.cfg.redmine_root}/tmp/pids
ExecStartPre=/bin/chown {self.cfg.redmine_user}:{self.cfg.shared_group} {self.cfg.redmine_root}/tmp/pids

# Execution & Rolling Reload Hooks (Zero-Downtime Phased Worker Reload)
ExecStart={self.bundle_bin} exec puma -C {self.cfg.redmine_root}/config/puma.rb
ExecReload={self.bundle_bin} exec pumactl -S {self.cfg.redmine_root}/tmp/pids/puma.state phased-restart
ExecStop={self.bundle_bin} exec pumactl -S {self.cfg.redmine_root}/tmp/pids/puma.state stop

Restart=on-failure
RestartSec=5s
Environment=RAILS_ENV=production

# Platinum Security Sandboxing
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=read-only
ReadWritePaths={self.cfg.redmine_root}/files {self.cfg.redmine_root}/log {self.cfg.redmine_root}/tmp {self.cfg.redmine_root}/public/plugin_assets

[Install]
WantedBy=multi-user.target
"""
        service_path.write_text(content, encoding="utf-8")
        SystemInspector.run_command(["systemctl", "daemon-reload"])
        SystemInspector.run_command(["systemctl", "enable", "redmine"])
        SystemInspector.run_command(["systemctl", "restart", "redmine"])
        DualLogger.success("Hardened Systemd service 'redmine' installed, enabled, and started.")

    def configure_logrotate(self) -> None:
        """Pillar 2: Automated Logrotate & Disk Space Protection."""
        DualLogger.step("Pillar 2: Configuring Logrotate & Disk Space Protection")
        logrotate_path = Path("/etc/logrotate.d/redmine")
        content = f"""# Redmine Platinum Log Rotation Policy
{self.cfg.redmine_root}/log/*.log {{
    weekly
    missingok
    rotate 8
    compress
    delaycompress
    notifempty
    copytruncate
    create 664 {self.cfg.redmine_user} {self.cfg.shared_group}
}}
"""
        logrotate_path.write_text(content, encoding="utf-8")
        SystemInspector.run_command(["chmod", "644", str(logrotate_path)])
        
        # Validate logrotate syntax
        res = subprocess.run(["logrotate", "-d", str(logrotate_path)], capture_output=True, text=True)
        if res.returncode == 0:
            DualLogger.success(f"Logrotate policy verified & installed: {logrotate_path}")
        else:
            DualLogger.warn(f"Logrotate syntax test warning: {res.stderr.strip()[:100]}")

    def configure_redis(self) -> None:
        DualLogger.step("Section 14: Verifying Redis Key-Value Store")
        if not SystemInspector.is_command_available("redis-server"):
            DualLogger.info("Redis is not detected. Installing redis-server via apt...")
            SystemInspector.run_command(["apt", "update", "-y"])
            SystemInspector.run_command(["apt", "install", "-y", "redis-server"])
        else:
            DualLogger.info("Redis binary is already present on the system.")

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
                target_anchor = "Rails.application.configure do"
                if target_anchor in text:
                    new_text = text.replace(target_anchor, f"{target_anchor}\n  config.active_job.queue_adapter = :sidekiq", 1)
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

ExecStart={self.bundle_bin} exec sidekiq -e production
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
                sendmail_block = """
  # ActionMailer Sendmail Delivery Configuration
  config.action_mailer.delivery_method = :sendmail
  config.action_mailer.sendmail_settings = {
    location: '/usr/sbin/sendmail',
    arguments: '-i -t'
  }
"""
                target_anchor = "Rails.application.configure do"
                if target_anchor in text:
                    new_text = text.replace(target_anchor, target_anchor + sendmail_block, 1)
                    prod_rb.write_text(new_text, encoding="utf-8")
                    DualLogger.success("Injected Sendmail delivery settings into production.rb")

    def run_plugin_migrations_and_assets(self) -> None:
        DualLogger.step("Section 16: Migrating Plugins, Publishing Assets, and Sanitizing Cache")
        rbenv_shims = f"/home/{self.cfg.redmine_user}/.rbenv/shims"
        exec_prefix = f"cd '{self.cfg.redmine_root}' && RAILS_ENV=production {rbenv_shims}/bundle exec "

        commands = [
            ("rake redmine:plugins:migrate", "Plugin Database Migrations"),
            ("rake redmine:plugins:assets", "Publish Plugin Assets"),
            ("rake tmp:clear tmp:cache:clear", "Sanitize Temp & Runtime Cache"),
            ("rake assets:precompile", "Precompile Core Assets")
        ]

        for sub_cmd, label in commands:
            DualLogger.info(f"Executing: {label}...")
            try:
                SystemInspector.run_command(exec_prefix + sub_cmd, as_user=self.cfg.redmine_user, shell=True)
                DualLogger.success(f"Completed: {label}\n")
            except subprocess.CalledProcessError as e:
                err_msg = (e.stderr or e.output or "Error occurred").strip().splitlines()[0] if (e.stderr or e.output) else "Execution failed"
                DualLogger.warn(f"{label} notice: {err_msg}\n")

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

ExecStart={self.bundle_bin} exec rails server -e development -b 0.0.0.0 -p 4000
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

ExecStart={self.bundle_bin} exec rails server -e test -b 0.0.0.0 -p 5000
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
        DualLogger.step("Section 19: Configuring Nginx Reverse Proxy with HTTPS Support")
        if not SystemInspector.is_command_available("nginx"):
            DualLogger.info("Nginx not detected. Installing nginx via apt...")
            SystemInspector.run_command(["apt", "install", "-y", "nginx"])

        vhost_path = Path("/etc/nginx/sites-available/redmine")

        if self.cfg.enable_ssl:
            ssl_dir = self.cfg.ssl_cert_path.parent
            ssl_dir.mkdir(parents=True, exist_ok=True)
            if not self.cfg.ssl_cert_path.exists() or not self.cfg.ssl_key_path.exists():
                DualLogger.info("Generating 2048-bit RSA self-signed SSL certificate...")
                gen_cmd = [
                    "openssl", "req", "-x509", "-nodes", "-days", "365",
                    "-newkey", "rsa:2048",
                    "-keyout", str(self.cfg.ssl_key_path),
                    "-out", str(self.cfg.ssl_cert_path),
                    "-subj", f"/C=IN/ST=IT/L=Server/O=Redmine/OU=IT/CN={self.cfg.domain_name}"
                ]
                res_ssl = subprocess.run(gen_cmd, capture_output=True, text=True)
                if res_ssl.returncode == 0:
                    DualLogger.success(f"Self-signed SSL certificate generated: {self.cfg.ssl_cert_path}")
                else:
                    DualLogger.error(f"SSL certificate generation failed: {res_ssl.stderr.strip()}")
                    raise RuntimeError("SSL certificate generation failed.")

            content = f"""# Redmine 7.0.3 Platinum Reverse Proxy Configuration (HTTPS Hardened)
upstream puma_redmine {{
    server 127.0.0.1:{self.cfg.puma_port} fail_timeout=0;
}}

# HTTP -> HTTPS Redirect
server {{
    listen 80;
    server_name {self.cfg.domain_name};
    return 301 https://$host$request_uri;
}}

# HTTPS Server
server {{
    listen 443 ssl;
    server_name {self.cfg.domain_name};

    ssl_certificate {self.cfg.ssl_cert_path};
    ssl_certificate_key {self.cfg.ssl_key_path};
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers HIGH:!aNULL:!MD5;
    ssl_prefer_server_ciphers on;
    ssl_session_cache shared:SSL:10m;
    ssl_session_timeout 1d;

    root {self.cfg.redmine_root}/public;
    access_log /var/log/nginx/redmine_access.log;
    error_log /var/log/nginx/redmine_error.log;

    client_max_body_size 100m;

    location ~ ^/(assets|plugin_assets|themes|javascripts|stylesheets|images)/ {{
        try_files $uri =404;
        gzip_static on;
        expires max;
        add_header Cache-Control public;
    }}

    location / {{
        try_files $uri @puma;
    }}

    location @puma {{
        proxy_pass http://puma_redmine;
        proxy_set_header Host $http_host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
        proxy_redirect off;
        proxy_read_timeout 300;
        proxy_connect_timeout 300;
    }}
}}
"""
        else:
            content = f"""# Redmine 7.0.3 Platinum Reverse Proxy Configuration
upstream puma_redmine {{
    server 127.0.0.1:{self.cfg.puma_port} fail_timeout=0;
}}

server {{
    listen 80;
    server_name {self.cfg.domain_name};

    root {self.cfg.redmine_root}/public;
    access_log /var/log/nginx/redmine_access.log;
    error_log /var/log/nginx/redmine_error.log;

    client_max_body_size 100m;

    location ~ ^/(assets|plugin_assets|themes|javascripts|stylesheets|images)/ {{
        try_files $uri =404;
        gzip_static on;
        expires max;
        add_header Cache-Control public;
    }}

    location / {{
        try_files $uri @puma;
    }}

    location @puma {{
        proxy_pass http://puma_redmine;
        proxy_set_header Host $http_host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_redirect off;
        proxy_read_timeout 300;
        proxy_connect_timeout 300;
    }}
}}
"""
        vhost_path.write_text(content, encoding="utf-8")
        DualLogger.success(f"Nginx virtual host written: {vhost_path}")

        enabled_link = Path("/etc/nginx/sites-enabled/redmine")
        if enabled_link.exists() or enabled_link.is_symlink():
            enabled_link.unlink()
        enabled_link.symlink_to(vhost_path)

        default_link = Path("/etc/nginx/sites-enabled/default")
        if default_link.exists() or default_link.is_symlink():
            default_link.unlink()
            DualLogger.info("Removed default Nginx placeholder symlink.")

        DualLogger.info("Testing Nginx configuration syntax (nginx -t)...")
        res = subprocess.run(["nginx", "-t"], capture_output=True, text=True)
        if res.returncode == 0:
            DualLogger.success("Nginx syntax validation: 100% OK")
            SystemInspector.run_command(["systemctl", "enable", "nginx"])
            SystemInspector.run_command(["systemctl", "reload", "nginx"])
            DualLogger.success("Nginx reverse proxy reloaded and active.")
        else:
            DualLogger.error(f"Nginx syntax error: {res.stderr.strip()}")
            raise RuntimeError("Nginx syntax validation failed.")

    @staticmethod
    def uninstall(redmine_root: Path) -> None:
        """Completely uninstall all Section 11-19 production services, web configs, and injected code."""
        DualLogger.step("Complete Services Uninstall & Revert")
        print(f"{AnsiColor.YELLOW}This operation completely stops and removes Redmine systemd services,")
        print(f"deletes puma.rb, deletes Nginx site configurations, removes logrotate policies,")
        print(f"and reverts modifications made to production.rb.{AnsiColor.RESET}\n")

        confirm = input("Are you sure you want to proceed with complete uninstall? [Y/n, Default: y]: ").strip().lower()
        if confirm in ["n", "no"]:
            DualLogger.info("Uninstall cancelled by user.")
            return

        services_to_stop = ["redmine", "sidekiq", "redmine-dev", "redmine-test"]
        for s in services_to_stop:
            if SystemInspector.is_service_active(s):
                DualLogger.info(f"Stopping service {s}...")
                subprocess.run(["systemctl", "stop", s])
            if SystemInspector.is_service_installed(s):
                DualLogger.info(f"Disabling service {s}...")
                subprocess.run(["systemctl", "disable", s])
            unit_file = Path(f"/etc/systemd/system/{s}.service")
            if unit_file.exists():
                unit_file.unlink()
                DualLogger.success(f"Removed /etc/systemd/system/{s}.service")

        SystemInspector.run_command(["systemctl", "daemon-reload"])

        # Puma Configuration Cleanup
        puma_file = redmine_root / "config" / "puma.rb"
        if puma_file.exists():
            puma_file.unlink()
            DualLogger.success(f"Removed {puma_file}")

        # Logrotate Cleanup
        logrotate_file = Path("/etc/logrotate.d/redmine")
        if logrotate_file.exists():
            logrotate_file.unlink()
            DualLogger.success(f"Removed logrotate policy: {logrotate_file}")

        # Nginx Cleanup (Both enabled symlink and available vhost)
        nginx_enabled = Path("/etc/nginx/sites-enabled/redmine")
        if nginx_enabled.exists() or nginx_enabled.is_symlink():
            nginx_enabled.unlink()
            DualLogger.success("Removed /etc/nginx/sites-enabled/redmine")

        nginx_available = Path("/etc/nginx/sites-available/redmine")
        if nginx_available.exists():
            nginx_available.unlink()
            DualLogger.success("Removed /etc/nginx/sites-available/redmine")

        if SystemInspector.is_command_available("nginx"):
            res = subprocess.run(["nginx", "-t"], capture_output=True, text=True)
            if res.returncode == 0:
                subprocess.run(["systemctl", "reload", "nginx"])
                DualLogger.success("Nginx reloaded cleanly.")

        # Revert modifications in production.rb
        prod_rb = redmine_root / "config" / "environments" / "production.rb"
        if prod_rb.exists():
            text = prod_rb.read_text(encoding="utf-8")
            modified = False
            if "config.active_job.queue_adapter = :sidekiq" in text:
                text = text.replace("  config.active_job.queue_adapter = :sidekiq\n", "")
                text = text.replace("config.active_job.queue_adapter = :sidekiq\n", "")
                modified = True
            if "# ActionMailer Sendmail Delivery Configuration" in text:
                target_block = """
  # ActionMailer Sendmail Delivery Configuration
  config.action_mailer.delivery_method = :sendmail
  config.action_mailer.sendmail_settings = {
    location: '/usr/sbin/sendmail',
    arguments: '-i -t'
  }
"""
                if target_block in text:
                    text = text.replace(target_block, "")
                    modified = True
            if modified:
                prod_rb.write_text(text, encoding="utf-8")
                DualLogger.success("Reverted Sidekiq & Sendmail modifications from production.rb")

        DualLogger.success("Complete services uninstall finished. All components cleanly removed.")

    @staticmethod
    def export_manifest(cfg: ServiceConfig) -> None:
        """Export deployment and service manifest metadata to JSON."""
        manifest_path = cfg.redmine_root / "redmine_services_manifest.json"
        supporting_manifest = Path(__file__).resolve().parent / "redmine_services_manifest.json"
        
        services = {
            "postgresql": "active" if SystemInspector.is_service_active("postgresql") else "inactive",
            "redis-server": "active" if SystemInspector.is_service_active("redis-server") else "inactive",
            "redmine": "active" if SystemInspector.is_service_active("redmine") else "inactive",
            "sidekiq": "active" if SystemInspector.is_service_active("sidekiq") else "inactive",
            "nginx": "active" if SystemInspector.is_service_active("nginx") else "inactive",
        }
        
        manifest_data = {
            "deployment": "Redmine 7.0.3 Platinum Enterprise Stack",
            "user": "redmineuser",
            "ruby_path": "/home/redmineuser/.rbenv/shims/ruby",
            "bundle_path": "/home/redmineuser/.rbenv/shims/bundle",
            "redmine_root": str(cfg.redmine_root),
            "services_status": services,
            "puma": {
                "bind_host": "127.0.0.1",
                "bind_port": cfg.puma_port,
                "workers": cfg.puma_workers,
                "config_file": str(cfg.redmine_root / "config" / "puma.rb"),
                "systemd_service": "redmine.service",
                "status": services["redmine"]
            },
            "sidekiq": {
                "config_file": str(cfg.redmine_root / "config" / "sidekiq.yml"),
                "systemd_service": "sidekiq.service",
                "status": services["sidekiq"]
            },
            "nginx": {
                "domain_name": cfg.domain_name,
                "vhost_config": "/etc/nginx/sites-available/redmine",
                "symlink": "/etc/nginx/sites-enabled/redmine",
                "http_port": 80,
                "https_port": 443 if cfg.enable_ssl else None,
                "ssl_enabled": cfg.enable_ssl,
                "ssl_cert": str(cfg.ssl_cert_path) if cfg.enable_ssl else None,
                "ssl_key": str(cfg.ssl_key_path) if cfg.enable_ssl else None,
                "status": services["nginx"]
            },
            "dns_and_mail_guidelines": {
                "cname_record": f"CNAME | {cfg.domain_name.split('.')[0] if '.' in cfg.domain_name else cfg.domain_name} -> <target-hostname>" if cfg.domain_name not in ["_", "localhost"] else "N/A (Local / Catch-all IP binding)",
                "spf_record": "TXT | @ -> v=spf1 ip4:<SERVER_PUBLIC_IP> ~all (Recommended for outbound Sendmail)",
                "mx_record": f"MX | 10 {cfg.domain_name} (Only needed if receiving incoming mail for issue processing)" if cfg.domain_name not in ["_", "localhost"] else "N/A"
            },
            "redis": {
                "status": services["redis-server"]
            },
            "database": {
                "engine": "postgresql",
                "status": services["postgresql"]
            }
        }
        
        for p in [manifest_path, supporting_manifest]:
            try:
                p.write_text(json.dumps(manifest_data, indent=4), encoding="utf-8")
                DualLogger.success(f"Deployment manifest written: {p}")
            except Exception as e:
                DualLogger.warn(f"Could not write manifest to {p}: {e}")

    @staticmethod
    def run_health_checks(puma_port: int = 3300) -> None:
        """Verify stack connectivity and service status."""
        DualLogger.step("Stack Health Check & Verification Matrix")
        services = ["postgresql", "redis-server", "redmine", "sidekiq", "nginx"]
        header = f"\n{'SERVICE':<20} | {'STATUS':<24} | {'PID / STATE':<30}"
        print(header)
        DualLogger.write_disk(header)
        sep = "-" * 76
        print(sep)
        DualLogger.write_disk(sep)

        for s in services:
            active = SystemInspector.is_service_active(s)
            status_text = f"{AnsiColor.GREEN}ACTIVE (Running){AnsiColor.RESET}" if active else f"{AnsiColor.RED}INACTIVE{AnsiColor.RESET}"
            status_clean = "ACTIVE (Running)" if active else "INACTIVE"

            pid_info = "N/A"
            if active:
                p_res = subprocess.run(["systemctl", "show", s, "--property=MainPID"], capture_output=True, text=True)
                pid_info = p_res.stdout.strip()

            line = f"{s:<20} | {status_text:<33} | {pid_info:<30}"
            clean_line = f"{s:<20} | {status_clean:<24} | {pid_info:<30}"
            print(line)
            DualLogger.write_disk(clean_line)

        print(sep)
        DualLogger.write_disk(sep)

        # Socket Connectivity Probes (Allow up to 10s for Puma to complete Rails boot)
        DualLogger.info(f"Testing Puma internal port http://127.0.0.1:{puma_port}...")
        puma_online = False
        import time
        for _ in range(5):
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                    sock.settimeout(2.0)
                    if sock.connect_ex(("127.0.0.1", puma_port)) == 0:
                        DualLogger.success(f"Puma standalone port {puma_port} is responding.")
                        puma_online = True
                        break
            except Exception:
                pass
            time.sleep(2.0)

        if not puma_online:
            DualLogger.warn(f"Puma port {puma_port} did not accept connections (may still be booting Rails).")

        DualLogger.info("Testing Nginx reverse proxy HTTP port 80...")
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(2.0)
                if sock.connect_ex(("127.0.0.1", 80)) == 0:
                    DualLogger.success("Nginx port 80 is accepting connections.")
                else:
                    DualLogger.warn("Nginx port 80 is not answering.")
        except Exception as e:
            DualLogger.warn(f"Nginx socket check exception: {e}")

        DualLogger.info("Testing Nginx reverse proxy HTTPS port 443...")
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(2.0)
                if sock.connect_ex(("127.0.0.1", 443)) == 0:
                    DualLogger.success("Nginx HTTPS port 443 is accepting connections.")
                else:
                    DualLogger.warn("Nginx HTTPS port 443 is not answering.")
        except Exception as e:
            DualLogger.warn(f"Nginx HTTPS socket check exception: {e}")


def prompt_user_configuration(redmine_root: Path, is_reconfigure: bool = False) -> ServiceConfig:
    action_label = "RECONFIGURE" if is_reconfigure else "DEPLOYMENT"
    print(f"\n{AnsiColor.HEADER}{'='*76}")
    print(f"  REDMINE 7.0.3 PLATINUM SERVICES PARAMETERS ({action_label})")
    print(f"{'='*76}{AnsiColor.RESET}\n")

    default_port = 3300
    puma_conf = redmine_root / "config" / "puma.rb"
    if puma_conf.exists():
        for line in puma_conf.read_text(encoding="utf-8").splitlines():
            line_s = line.strip()
            if line_s.startswith("bind") and "tcp://127.0.0.1:" in line_s:
                port_part = line_s.split("tcp://127.0.0.1:")[1].split("'")[0].split('"')[0]
                if port_part.isdigit():
                    default_port = int(port_part)
                    break

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

    if not SystemInspector.check_port_available(puma_port):
        DualLogger.warn(f"Port {puma_port} is currently bound. (If Puma is already running on this port, it will be restarted cleanly).")

    domain_in = input("Enter Server Domain Name / IP for Nginx [Default: _ (Catch-all)]: ").strip()
    domain_name = domain_in if domain_in else "_"

    workers_in = input("Enter Puma Worker Count [Default: 2]: ").strip()
    puma_workers = int(workers_in) if workers_in.isdigit() else 2

    dev_in = input("Enable background Dev service on port 4000? (y/n) [Default: n]: ").strip().lower()
    enable_dev = dev_in == "y"

    test_in = input("Enable background Test service on port 5000? (y/n) [Default: n]: ").strip().lower()
    enable_test = test_in == "y"

    sendmail_in = input("Configure local Sendmail MTA? (y/n) [Default: y]: ").strip().lower()
    configure_sendmail = sendmail_in != "n"

    ssl_in = input("Enable HTTPS / SSL with self-signed certificate? (y/n) [Default: y]: ").strip().lower()
    enable_ssl = ssl_in != "n"

    return ServiceConfig(
        redmine_root=redmine_root,
        redmine_user="redmineuser",
        shared_group="sharedgroup",
        puma_port=puma_port,
        puma_workers=puma_workers,
        puma_min_threads=0,
        puma_max_threads=16,
        domain_name=domain_name,
        enable_dev_service=enable_dev,
        enable_test_service=enable_test,
        configure_sendmail=configure_sendmail,
        enable_ssl=enable_ssl
    )


def show_interactive_menu(setup_state: dict[str, bool], redmine_root: Path) -> None:
    print(f"\n{AnsiColor.HEADER}{'='*76}")
    print("  REDMINE 7.0.3 PLATINUM SERVICES - STATE DETECTION & LIFECYCLE MENU")
    print(f"{'='*76}{AnsiColor.RESET}")

    print("\nCurrent Components Status:")
    for comp, exists in setup_state.items():
        status = f"{AnsiColor.GREEN}Configured / Active{AnsiColor.RESET}" if exists else f"{AnsiColor.YELLOW}Not Present{AnsiColor.RESET}"
        print(f"  - {comp:<20}: {status}")

    print("\nSelect Action:")
    print("  [1] Reconfigure / Update Stack (Update Ports, Domain, Services in-place)")
    print("  [2] Run Live Health & Verification Matrix")
    print("  [3] Safe Complete Uninstall / Teardown (Removes units, logrotate & Nginx link)")
    print("  [4] Fresh Complete Re-deployment")
    print("  [5] Exit\n")

    choice = input("Enter choice [1-5]: ").strip()

    if choice == "1":
        cfg = prompt_user_configuration(redmine_root, is_reconfigure=True)
        mgr = ServiceManager(cfg)
        mgr.ensure_pid_directory()
        mgr.configure_puma()
        mgr.configure_puma_systemd()
        mgr.configure_logrotate()
        mgr.configure_redis()
        mgr.configure_sidekiq()
        mgr.configure_email()
        mgr.configure_dev_and_test_services()
        mgr.configure_nginx()
        SystemInspector.run_command(["systemctl", "restart", "redmine"])
        SystemInspector.run_command(["systemctl", "restart", "sidekiq"])
        ServiceManager.run_health_checks(cfg.puma_port)
    elif choice == "2":
        puma_port = 3300
        puma_conf = redmine_root / "config" / "puma.rb"
        if puma_conf.exists():
            for line in puma_conf.read_text(encoding="utf-8").splitlines():
                line_s = line.strip()
                if line_s.startswith("bind") and "tcp://127.0.0.1:" in line_s:
                    port_part = line_s.split("tcp://127.0.0.1:")[1].split("'")[0].split('"')[0]
                    if port_part.isdigit():
                        puma_port = int(port_part)
                        break
        ServiceManager.run_health_checks(puma_port)
    elif choice == "3":
        ServiceManager.uninstall(redmine_root)
    elif choice == "4":
        cfg = prompt_user_configuration(redmine_root, is_reconfigure=False)
        mgr = ServiceManager(cfg)
        mgr.ensure_pid_directory()
        mgr.configure_puma()
        mgr.configure_puma_systemd()
        mgr.configure_logrotate()
        mgr.configure_redis()
        mgr.configure_sidekiq()
        mgr.configure_email()
        mgr.run_plugin_migrations_and_assets()
        mgr.configure_dev_and_test_services()
        mgr.configure_nginx()
        SystemInspector.run_command(["systemctl", "restart", "redmine"])
        SystemInspector.run_command(["systemctl", "restart", "sidekiq"])
        ServiceManager.run_health_checks(cfg.puma_port)
    else:
        DualLogger.info("Exiting without making changes.")
        sys.exit(0)


def main() -> None:
    if not SystemInspector.is_root():
        print(f"{AnsiColor.RED}[ERROR] This orchestrator configures systemd units, Nginx, and system daemons.")
        print(f"Please execute with administrative privileges: sudo python3 setup_redmine_services.py{AnsiColor.RESET}")
        sys.exit(1)

    redmine_root = Path("/data/redmine")
    log_path = redmine_root / "log" / "setup_redmine_services.log"
    if not redmine_root.exists():
        log_path = Path("./setup_redmine_services.log")

    DualLogger.init(log_path)
    DualLogger.info(f"Log stream active: {log_path.resolve()}")

    try:
        setup_state = SystemInspector.detect_existing_setup(redmine_root)
        is_already_configured = any([setup_state["puma_config"], setup_state["redmine_service"], setup_state["nginx_vhost"]])

        if is_already_configured:
            show_interactive_menu(setup_state, redmine_root)
        else:
            cfg = prompt_user_configuration(redmine_root, is_reconfigure=False)
            mgr = ServiceManager(cfg)
            mgr.ensure_pid_directory()
            mgr.configure_puma()
            mgr.configure_puma_systemd()
            mgr.configure_logrotate()
            mgr.configure_redis()
            mgr.configure_sidekiq()
            mgr.configure_email()
            mgr.run_plugin_migrations_and_assets()
            mgr.configure_dev_and_test_services()
            mgr.configure_nginx()

            SystemInspector.run_command(["systemctl", "restart", "redmine"])
            SystemInspector.run_command(["systemctl", "restart", "sidekiq"])
            ServiceManager.export_manifest(cfg)
            ServiceManager.run_health_checks(cfg.puma_port)

        print(f"\n{AnsiColor.GREEN}{'='*76}")
        print("  REDMINE 7.0.3 PLATINUM SERVICES OPERATION COMPLETE")
        print(f"{'='*76}{AnsiColor.RESET}\n")

        access_proto = "https" if getattr(locals().get("cfg", None), "enable_ssl", True) else "http"
        domain_display = getattr(locals().get("cfg", None), "domain_name", "localhost")
        if domain_display == "_":
            domain_display = "127.0.0.1"

        print(f"{AnsiColor.BOLD}Post-Deployment Access & Operations Instructions:{AnsiColor.RESET}")
        print(f"  1. Web Access: {access_proto}://{domain_display}/")
        if access_proto == "https":
            print(f"     Note: Since a self-signed SSL certificate is used, your browser will show a warning.")
            print(f"     Click 'Advanced' -> 'Proceed to {domain_display} (unsafe)' to proceed.")
        print(f"  2. Service Controls:")
        print(f"     sudo systemctl status redmine    # Puma Web Server")
        print(f"     sudo systemctl status sidekiq    # Background Worker")
        print(f"     sudo systemctl status nginx      # Reverse Proxy")

        if domain_display not in ["127.0.0.1", "localhost", "_"]:
            subdomain_prefix = domain_display.split(".")[0] if "." in domain_display else domain_display
            print(f"  3. DNS Records Configuration (Action Required for Public Access):")
            print(f"     - CNAME Record: In your DNS manager, point:")
            print(f"         Type: CNAME | Name: {subdomain_prefix} | Target: <your-server-hostname>")
            print(f"         (Or Type: A | Name: {subdomain_prefix} | Value: <your-server-public-ip>)")
            print(f"     - Local Testing: Add '127.0.0.1 {domain_display}' to /etc/hosts for instant testing.")
            print(f"  4. Email Delivery & Mail Records (For Sendmail / ActionMailer):")
            print(f"     - SPF TXT Record: Authorize this server to send notifications without spam classification:")
            print(f"         Type: TXT | Name: @ | Value: v=spf1 ip4:<your-server-public-ip> ~all")
            print(f"     - MX Record: Only required if configuring Redmine to receive incoming email for issues.")
        else:
            print(f"  3. Local Testing:")
            print(f"     - If using a domain like redmine.synorastudio.in later, add CNAME: redmine -> server hostname.")
            print(f"     - Set up an SPF record (v=spf1 ip4:<SERVER_IP> ~all) if using Sendmail notifications.")

        print(f"  4. Deployment Manifest:")
        print(f"     /data/redmine/redmine_services_manifest.json")
        print(f"     {Path(__file__).resolve().parent / 'redmine_services_manifest.json'}\n")

    except Exception as e:
        DualLogger.error(f"Execution terminated with error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print(f"\n{AnsiColor.YELLOW}[NOTICE] Services setup cancelled by user. Exiting cleanly.{AnsiColor.RESET}\n")
        sys.exit(130)
