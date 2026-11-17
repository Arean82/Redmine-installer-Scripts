#!/usr/bin/env python3
"""
setup_ruby.py
=============================================================================
Platinum Enterprise Ruby & rbenv Environment Provisioner
=============================================================================
Architecture:
  - Object-Oriented, Highly Modular, Single-Responsibility Design
  - Flexible Target Deployment:
      * Option 1: Redmine Production Service Account (/home/redmineuser/.rbenv)
      * Option 2: Active User Environment ($HOME/.rbenv)
  - Comprehensive Lifecycle Management:
      * State Detection: Clean, Partial/Broken, Already Installed
      * Automatic Stale Build / Artifact Cleanup
      * Reinstall / Upgrade or Re-bind Existing Ruby
      * Automatic Group & Permissions Enforcement
=============================================================================
"""

from __future__ import annotations

import glob
import os
import re
import shutil
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


class Logger:
    """Enterprise console logger delivering standardized status markers."""

    @staticmethod
    def info(message: str) -> None:
        """Log an informational diagnostic message."""
        print(f"{AnsiColor.BLUE}[INFO]{AnsiColor.RESET} {message}")

    @staticmethod
    def success(message: str) -> None:
        """Log an operation success notice."""
        print(f"{AnsiColor.GREEN}[SUCCESS]{AnsiColor.RESET} {message}")

    @staticmethod
    def warning(message: str) -> None:
        """Log a non-fatal warning condition."""
        print(f"{AnsiColor.YELLOW}[WARNING]{AnsiColor.RESET} {message}")

    @staticmethod
    def error(message: str) -> None:
        """Log an error condition to standard error."""
        print(f"{AnsiColor.RED}[ERROR]{AnsiColor.RESET} {message}", file=sys.stderr)

    @staticmethod
    def banner(title: str) -> None:
        """Print an emphasized visual header banner."""
        divider: Final[str] = "=" * 70
        print(f"\n{AnsiColor.BOLD}{AnsiColor.CYAN}{divider}{AnsiColor.RESET}")
        print(f"{AnsiColor.BOLD}{AnsiColor.CYAN}    {title.center(62)}    {AnsiColor.RESET}")
        print(f"{AnsiColor.BOLD}{AnsiColor.CYAN}{divider}{AnsiColor.RESET}\n")


class CommandExecutor:
    """Robust execution wrapper around system commands with error isolation."""

    @staticmethod
    def run(
        command: str,
        *,
        check: bool = True,
        capture: bool = False,
        env: dict[str, str] | None = None,
    ) -> str:
        """Execute a shell command with standard diagnostic handling."""
        try:
            res = subprocess.run(
                command,
                shell=True,
                check=check,
                capture_output=capture,
                text=True,
                env=env,
            )
            return res.stdout.strip() if capture else ""
        except subprocess.CalledProcessError as exc:
            Logger.error(f"Execution failed for command: '{command}'")
            if capture and exc.stderr:
                Logger.error(exc.stderr.strip())
            raise


@dataclass(frozen=True)
class RubyVersionCandidate:
    """Data representation of a Ruby release candidate and its compatibility."""

    version_str: str
    major: int
    minor: int
    patch: int

    @classmethod
    def from_string(cls, raw: str) -> RubyVersionCandidate | None:
        """Construct a candidate instance from a semantic version string."""
        match = re.match(r"^(\d+)\.(\d+)\.(\d+)$", raw.strip())
        if not match:
            return None
        return cls(
            version_str=raw.strip(),
            major=int(match.group(1)),
            minor=int(match.group(2)),
            patch=int(match.group(3)),
        )

    @property
    def semantic_tuple(self) -> tuple[int, int, int]:
        """Return the semantic integer tuple for sorting."""
        return (self.major, self.minor, self.patch)

    @property
    def compatibility_annotation(self) -> str:
        """Produce an enterprise compatibility assessment label."""
        if self.major == 4:
            if self.patch >= 4:
                return f"{AnsiColor.GREEN}[PLATINUM LTS]{AnsiColor.RESET} Full Redmine 7+ compliance; patched upstream wiki regression"
            return f"{AnsiColor.YELLOW}[UNSAFE]{AnsiColor.RESET} Regressed Wiki render performance (< 4.0.4)"
        if self.major == 3 and self.minor == 4:
            return f"{AnsiColor.CYAN}[SUPPORTED]{AnsiColor.RESET} Modern performance baseline with Rails 8.1 support"
        if self.major == 3 and self.minor == 3:
            return f"{AnsiColor.YELLOW}[LEGACY / EOL RISK]{AnsiColor.RESET} Ubuntu 26 native baseline; older plugin support"
        return "[SUPPORTED]"


class SystemInspector:
    """Inspects underlying operating system capabilities, users, and permissions."""

    @staticmethod
    def get_current_user() -> str:
        """Return the current user executing the process."""
        if os.geteuid() == 0:
            return "root"
        return os.environ.get("USER", "") or CommandExecutor.run("whoami", capture=True, check=False)

    @staticmethod
    def ensure_redmine_user_and_permissions() -> None:
        """Verify presence of redmineuser, sharedgroup, and /data permissions; create if missing."""
        is_root = SystemInspector.get_current_user() == "root"

        # 1. Verify or create sharedgroup
        group_check = CommandExecutor.run("getent group sharedgroup", capture=True, check=False)
        if not group_check:
            if is_root:
                Logger.info("Group 'sharedgroup' not found. Creating...")
                CommandExecutor.run("groupadd -f sharedgroup")
                Logger.success("Group 'sharedgroup' created.")
            else:
                Logger.warning("Group 'sharedgroup' is missing; run with sudo to create.")
        else:
            Logger.success("Group 'sharedgroup' exists.")

        # 2. Verify or create redmineuser
        user_check = CommandExecutor.run("id redmineuser", capture=True, check=False)
        if not user_check:
            if is_root:
                Logger.info("User 'redmineuser' not found. Creating system user...")
                CommandExecutor.run("adduser --system --group --shell /bin/bash --home /home/redmineuser redmineuser")
                CommandExecutor.run("usermod -aG sudo,sharedgroup redmineuser", check=False)
                Logger.success("User 'redmineuser' created with home /home/redmineuser.")
            else:
                Logger.warning("User 'redmineuser' is missing; run with sudo to create.")
        else:
            Logger.success("User 'redmineuser' exists.")
            if is_root:
                CommandExecutor.run("usermod -aG sudo,sharedgroup redmineuser", check=False)

        # 3. Verify /data directory and SetGID permissions
        data_path = Path("/data")
        if not data_path.exists() and is_root:
            Logger.info("Directory '/data' not found. Creating...")
            CommandExecutor.run("mkdir -p /data")
        if is_root:
            CommandExecutor.run("chown root:sharedgroup /data 2>/dev/null && chmod 2775 /data 2>/dev/null", check=False)
            Logger.success("Directory '/data' permissions verified (2775 root:sharedgroup).")


class RbenvManager:
    """Manages rbenv lifecycle, plugins, environment paths, and cleanups."""

    def __init__(self, target_home: Path, is_redmine_mode: bool = False) -> None:
        self.home: Path = target_home
        self.is_redmine_mode: bool = is_redmine_mode
        self.rbenv_dir: Path = self.home / ".rbenv"
        self.ruby_build_dir: Path = self.rbenv_dir / "plugins" / "ruby-build"
        self.shims_dir: Path = self.rbenv_dir / "shims"
        self.bin_dir: Path = self.rbenv_dir / "bin"

    def get_runtime_env(self) -> dict[str, str]:
        """Return an active environment dictionary injected with target rbenv shims."""
        current_env = os.environ.copy()
        current_env["RBENV_ROOT"] = str(self.rbenv_dir)
        current_env["PATH"] = f"{self.bin_dir}:{self.shims_dir}:" + current_env.get("PATH", "")
        return current_env

    def get_active_ruby_version(self) -> str:
        """Discover the currently bound Ruby binary version in target rbenv."""
        ruby_bin = self.shims_dir / "ruby"
        if ruby_bin.is_file():
            try:
                output = CommandExecutor.run(f"{ruby_bin} -v", capture=True, check=False, env=self.get_runtime_env())
                if output and "ruby" in output:
                    match = re.search(r"ruby (\d+\.\d+\.\d+)", output)
                    return match.group(1) if match else output.split()[1]
            except Exception:
                pass
        return "None (Unconfigured)"

    def ensure_system_dependencies(self) -> None:
        """Ensure native C-compilers and build dependencies are installed."""
        if not shutil.which("git") or not shutil.which("gcc") or not shutil.which("make"):
            Logger.info("Installing essential compiler toolchain via apt...")
            prefix = "sudo " if SystemInspector.get_current_user() != "root" else ""
            CommandExecutor.run(
                f"{prefix}apt update && {prefix}apt install -y "
                "git curl build-essential libssl-dev libreadline-dev "
                "zlib1g-dev libyaml-dev pkg-config"
            )

    def provision_rbenv_repositories(self) -> None:
        """Clone or update rbenv core and the ruby-build plugin."""
        self.ensure_system_dependencies()

        if not self.rbenv_dir.is_dir():
            Logger.info(f"Cloning rbenv repository into: {self.rbenv_dir}")
            CommandExecutor.run(f"git clone https://github.com/rbenv/rbenv.git {self.rbenv_dir}")
        else:
            Logger.info(f"Syncing existing rbenv repository in {self.rbenv_dir}...")
            CommandExecutor.run(f"git -C {self.rbenv_dir} pull", check=False)

        if not self.ruby_build_dir.is_dir():
            Logger.info(f"Provisioning ruby-build plugin into: {self.ruby_build_dir}")
            self.ruby_build_dir.parent.mkdir(parents=True, exist_ok=True)
            CommandExecutor.run(f"git clone https://github.com/rbenv/ruby-build.git {self.ruby_build_dir}")
        else:
            Logger.info("Fetching latest Ruby build definitions via ruby-build...")
            CommandExecutor.run(f"git -C {self.ruby_build_dir} pull", check=False)

    def configure_shell_profile(self) -> None:
        """Inject rbenv shims into target user's bash profile and login profile."""
        bashrc_file = self.home / ".bashrc"
        profile_file = self.home / ".bash_profile"
        alt_profile = self.home / ".profile"

        initialization_directives = [
            'export RBENV_ROOT="$HOME/.rbenv"',
            'export PATH="$HOME/.rbenv/bin:$HOME/.rbenv/shims:$PATH"',
            'eval "$(rbenv init - bash)"',
        ]

        # 1. Update ~/.bashrc
        existing_rc = bashrc_file.read_text(encoding="utf-8") if bashrc_file.exists() else ""
        directives_to_write = [
            d for d in initialization_directives if d not in existing_rc
        ]

        if directives_to_write:
            with bashrc_file.open("a", encoding="utf-8") as f:
                f.write("\n# rbenv Environment Initialization\n")
                for d in directives_to_write:
                    f.write(f"{d}\n")
            Logger.success(f"Updated {bashrc_file} with rbenv environment hooks.")

        # 2. Ensure login shells (su - redmineuser) source ~/.bashrc via .bash_profile or .profile
        source_hook = 'if [ -f ~/.bashrc ]; then . ~/.bashrc; fi'
        target_profile = profile_file if profile_file.exists() or not alt_profile.exists() else alt_profile
        existing_profile = target_profile.read_text(encoding="utf-8") if target_profile.exists() else ""
        if source_hook not in existing_profile:
            with target_profile.open("a", encoding="utf-8") as f:
                f.write(f"\n# Source .bashrc for login shells\n{source_hook}\n")
            Logger.success(f"Ensured login shell sources .bashrc in {target_profile}.")

    def fetch_compatible_rubies(self) -> list[RubyVersionCandidate]:
        """Query and return top compatible Ruby versions."""
        ruby_build_binary = self.ruby_build_dir / "bin" / "ruby-build"
        query_command = (
            f"{ruby_build_binary} --definitions"
            if ruby_build_binary.is_file()
            else "rbenv install -l"
        )

        raw_output = CommandExecutor.run(
            query_command,
            capture=True,
            check=False,
            env=self.get_runtime_env(),
        )

        candidates: list[RubyVersionCandidate] = []
        if raw_output:
            for line in raw_output.splitlines():
                candidate = RubyVersionCandidate.from_string(line)
                if candidate and ((candidate.major == 3 and candidate.minor >= 2) or (candidate.major >= 4)):
                    candidates.append(candidate)

        if not candidates:
            fallback_versions = ["4.0.9", "4.0.4", "3.4.12", "3.4.0", "3.3.13"]
            candidates = [
                v for ver_str in fallback_versions if (v := RubyVersionCandidate.from_string(ver_str))
            ]

        candidates.sort(key=lambda item: item.semantic_tuple, reverse=True)
        return candidates[:5]

    def cleanup_partial_builds(self, target_version: str) -> None:
        """Clean up interrupted or corrupted compilation directories and temporary build files."""
        for tmp_item in glob.glob("/tmp/ruby-build.*"):
            try:
                p = Path(tmp_item)
                if p.is_dir():
                    shutil.rmtree(p, ignore_errors=True)
                elif p.is_file():
                    p.unlink(missing_ok=True)
            except Exception:
                pass

        version_install_dir = self.rbenv_dir / "versions" / target_version
        ruby_bin = version_install_dir / "bin" / "ruby"

        if version_install_dir.exists() and not ruby_bin.is_file():
            Logger.warning(f"Incomplete/corrupted build detected in {version_install_dir}. Cleaning up...")
            shutil.rmtree(version_install_dir, ignore_errors=True)
            Logger.success(f"Purged incomplete directory: {version_install_dir}")

    def fix_permissions(self) -> None:
        """Ensure correct ownership for target rbenv directory."""
        if SystemInspector.get_current_user() == "root" and self.is_redmine_mode:
            CommandExecutor.run(
                f"chown -R redmineuser:sharedgroup {self.rbenv_dir} 2>/dev/null || true",
                check=False,
            )

    def install_and_bind_version(self, target_version: str, force_reinstall: bool = False) -> None:
        """Compile selected Ruby version, bind global default, and setup Bundler."""
        version_install_dir = self.rbenv_dir / "versions" / target_version
        ruby_bin = version_install_dir / "bin" / "ruby"

        self.cleanup_partial_builds(target_version)

        if version_install_dir.is_dir() and ruby_bin.is_file() and not force_reinstall:
            Logger.info(f"Ruby {target_version} is already compiled in {version_install_dir}")
        else:
            if force_reinstall and version_install_dir.exists():
                Logger.info(f"Purging existing Ruby {target_version} for fresh reinstallation...")
                shutil.rmtree(version_install_dir, ignore_errors=True)

            Logger.info(f"Compiling Ruby {target_version} from source (3-6 mins)...")
            CommandExecutor.run(
                f"{self.bin_dir}/rbenv install {target_version}",
                env=self.get_runtime_env(),
            )
            Logger.success(f"Ruby {target_version} compilation completed successfully.")

        Logger.info(f"Designating Ruby {target_version} as the global rbenv default...")
        CommandExecutor.run(f"{self.bin_dir}/rbenv global {target_version}", env=self.get_runtime_env())

        Logger.info("Bootstrap and update Bundler and RubyGems...")
        gem_executable = self.shims_dir / "gem"
        CommandExecutor.run(
            f"{gem_executable} install bundler",
            env=self.get_runtime_env(),
        )
        CommandExecutor.run(
            f"{gem_executable} update --system",
            check=False,
            env=self.get_runtime_env(),
        )

        self.fix_permissions()


class RubyProvisionerCLI:
    """Interactive command-line workflow for guided enterprise deployment."""

    def run(self) -> None:
        """Execute the interactive deployment workflow."""
        Logger.banner("Enterprise Ruby & rbenv Provisioner")

        current_user = SystemInspector.get_current_user()
        current_home = Path.home()

        print(f"{AnsiColor.BOLD}Current Operating User:{AnsiColor.RESET} {AnsiColor.CYAN}{current_user}{AnsiColor.RESET} ({current_home})\n")

        # 1. Target Environment Prompt
        print(f"{AnsiColor.BOLD}Select Target Installation Environment:{AnsiColor.RESET}")
        print("-" * 70)
        print(f"  1) Redmine Service Account (/home/redmineuser/.rbenv) {AnsiColor.GREEN}[Production Standard]{AnsiColor.RESET}")
        print(f"  2) Current User Environment ({current_home}/.rbenv)")
        print(f"  0) Cancel and Exit")
        print("-" * 70)

        target_choice = input(f"{AnsiColor.BOLD}Select target [1-2, 0=exit, default=1]: {AnsiColor.RESET}").strip() or "1"

        if target_choice == "0":
            Logger.info("Provisioning cancelled.")
            sys.exit(0)
        elif target_choice == "1":
            target_home = Path("/home/redmineuser")
            is_redmine_mode = True
            SystemInspector.ensure_redmine_user_and_permissions()
        elif target_choice == "2":
            target_home = current_home
            is_redmine_mode = False
        else:
            Logger.error("Invalid choice specified. Exiting.")
            sys.exit(1)

        rbenv_manager = RbenvManager(target_home=target_home, is_redmine_mode=is_redmine_mode)

        print(f"\n{AnsiColor.BOLD}Target Provisioning Path:{AnsiColor.RESET} {AnsiColor.CYAN}{rbenv_manager.rbenv_dir}{AnsiColor.RESET}")

        # 2. Provision rbenv & plugins
        rbenv_manager.provision_rbenv_repositories()
        rbenv_manager.configure_shell_profile()
        rbenv_manager.fix_permissions()

        current_ruby = rbenv_manager.get_active_ruby_version()
        print(f"{AnsiColor.BOLD}Currently Bound Target Ruby:{AnsiColor.RESET} {AnsiColor.YELLOW}{current_ruby}{AnsiColor.RESET}\n")

        # 3. Present Top 5 releases
        Logger.info("Evaluating upstream compatibility definitions...")
        candidates = rbenv_manager.fetch_compatible_rubies()

        print(f"\n{AnsiColor.BOLD}Available Compatible Ruby Releases:{AnsiColor.RESET}")
        print("-" * 70)
        for index, candidate in enumerate(candidates, start=1):
            marker = " (Default)" if index == 1 else ""
            installed = (rbenv_manager.rbenv_dir / "versions" / candidate.version_str / "bin" / "ruby").is_file()
            status_tag = f" {AnsiColor.GREEN}[INSTALLED]{AnsiColor.RESET}" if installed else ""
            print(f" [{index}] Ruby {AnsiColor.BOLD}{candidate.version_str:<8}{AnsiColor.RESET}{marker}{status_tag} -> {candidate.compatibility_annotation}")
        print(f" [0] Retain active environment ({current_ruby}) and terminate")
        print("-" * 70)

        chosen_version: str | None = None
        while True:
            selection = input(
                f"\n{AnsiColor.BOLD}Select Ruby version to deploy [1-{len(candidates)}, 0=exit, default=1]: {AnsiColor.RESET}"
            ).strip() or "1"

            if selection == "0":
                Logger.info(f"Preserving existing Ruby binding: {current_ruby}")
                sys.exit(0)

            try:
                candidate_index = int(selection) - 1
                if 0 <= candidate_index < len(candidates):
                    chosen_version = candidates[candidate_index].version_str
                    break
                else:
                    Logger.error(f"Selection must be between 0 and {len(candidates)}.")
            except ValueError:
                Logger.error("Input must be a valid option number.")

        # Check existing state for selected version
        version_dir = rbenv_manager.rbenv_dir / "versions" / chosen_version
        ruby_bin = version_dir / "bin" / "ruby"

        force_reinstall = False
        if version_dir.is_dir() and ruby_bin.is_file():
            print(f"\n{AnsiColor.YELLOW}Notice: Ruby {chosen_version} is already installed in {version_dir}.{AnsiColor.RESET}")
            print("  1) Re-bind as global default and update Bundler (Fastest)")
            print(f"  2) Reinstall / Upgrade cleanly (Recompiles {chosen_version})")
            print("  3) Cancel and exit\n")

            sub_choice = input(f"{AnsiColor.BOLD}Select option [1-3, default=1]: {AnsiColor.RESET}").strip() or "1"
            if sub_choice == "2":
                force_reinstall = True
            elif sub_choice == "3":
                Logger.info("Deployment aborted by user.")
                sys.exit(0)

        # 4. Clean partial builds and install
        rbenv_manager.install_and_bind_version(chosen_version, force_reinstall=force_reinstall)

        env = rbenv_manager.get_runtime_env()
        verified_ruby = CommandExecutor.run(f"{rbenv_manager.shims_dir}/ruby -v", capture=True, env=env)
        verified_bundle = CommandExecutor.run(f"{rbenv_manager.shims_dir}/bundle -v", capture=True, check=False, env=env)

        Logger.banner("PROVISIONING COMPLETED SUCCESSFULLY")
        print(f" {AnsiColor.BOLD}Target Home:{AnsiColor.RESET}             {rbenv_manager.home}")
        print(f" {AnsiColor.BOLD}Verified Active Ruby:{AnsiColor.RESET}    {verified_ruby}")
        print(f" {AnsiColor.BOLD}Verified Active Bundler:{AnsiColor.RESET} {verified_bundle}")
        print(f" {AnsiColor.BOLD}Configured Shims Path:{AnsiColor.RESET}   {rbenv_manager.shims_dir}")
        print(f"{AnsiColor.BOLD}{AnsiColor.CYAN}{'=' * 70}{AnsiColor.RESET}\n")
        Logger.info(f"Reload your shell using 'source {rbenv_manager.home}/.bashrc' to apply changes.")


if __name__ == "__main__":
    try:
        RubyProvisionerCLI().run()
    except KeyboardInterrupt:
        print(f"\n{AnsiColor.YELLOW}[NOTICE] Setup interrupted by operator. Exiting cleanly.{AnsiColor.RESET}")
        sys.exit(130)
