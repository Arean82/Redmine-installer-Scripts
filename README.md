# Redmine Enterprise Automated Deployment & Lifecycle Orchestrator

Production-grade automated installation and lifecycle management scripts for **Redmine** on Debian/Ubuntu systems with PostgreSQL, rbenv/Ruby, Puma, Redis/Sidekiq, Systemd, and Nginx.

---

## Features

- **Automated Dependency Provisioning:** System packages, native build headers, and PostgreSQL with PGroonga.
- **Ruby Runtime Management:** Clean automated installation of `rbenv` and Ruby builds or binding to existing environments.
- **Redmine Core Installation:** Archive retrieval, database role and schema creation, `database.yml` templating, Bundler execution, database migrations, and asset precompilation.
- **Production Services Architecture:**
  - Puma web application server with systemd integration and phased zero-downtime reloads.
  - Hardened systemd sandboxing (`ProtectSystem`, `PrivateTmp`, `NoNewPrivileges`).
  - Redis and Sidekiq background job processing.
  - Automated Nginx reverse proxy configuration with syntax verification.
  - Automated log rotation rules via `logrotate`.
- **Full Lifecycle Management:** Interactive menu providing fresh deployment, modular stage execution, stack health checks, in-place reconfigurations, and safe clean teardown.

---

## Directory Structure

```text
.
├── LICENSE
├── README.md
├── Redmine.docx
├── Redmine_7_0_2_Installation_Guide.docx
├── Redmine_7_0_3_Installation_Guide.docx
└── Supporting_Files/
    ├── Redmine_setup.py            # Combined installer (end-to-end master deployment)
    ├── setup_ruby.py               # Ruby installer (rbenv & Ruby environment provisioner)
    ├── setup_redmine_install.py    # Modular core Redmine installer (DB, gems, migrations)
    ├── setup_redmine_services.py   # Modular production services manager (Puma, systemd, Nginx)
    └── format_appendix_native.py   # Documentation formatting utility
```

---

## Requirements

- **Operating System:** Ubuntu 22.04 / 24.04 / 26.04 LTS or Debian 11 / 12
- **Python:** Python 3.8+
- **Privileges:** Root or `sudo` access
- **Target Components:** PostgreSQL, Nginx, Redis, Systemd

---

## Quick Start

### 1. Combined Installer (End-to-End)

To run the combined full-stack installer:

```bash
cd Supporting_Files
sudo python3 Redmine_setup.py
```

The interactive menu will prompt for the desired deployment option:

```text
[1] Complete End-to-End Platinum Deployment (Sections 0 - 19)
[2] Core Installation Only (Sections 0 - 10)
[3] Services & Web Stack Only (Sections 11 - 19)
[4] Stack Status & Live Health Verification Matrix
[5] Safe Clean Services Teardown (Leaves DB & Redmine files intact)
[6] Exit
```

### 2. Standalone Ruby Installer

To provision only `rbenv` and Ruby:

```bash
sudo python3 Supporting_Files/setup_ruby.py
```

### 3. Other Modular Scripts

- **Core Redmine Setup (DB, Gems, Migrations):**
  ```bash
  sudo python3 Supporting_Files/setup_redmine_install.py
  ```

- **Production Services (Puma, Nginx, Systemd, Redis):**
  ```bash
  sudo python3 Supporting_Files/setup_redmine_services.py
  ```

---

## Verification & Health Check

You can run health verification at any time using Option `[4]` in `Redmine_setup.py` or Option `[3]` in `setup_redmine_services.py` to inspect:
- Systemd service states (`redmine-puma`, `redmine-sidekiq`, `nginx`, `postgresql`, `redis`)
- Open sockets and active HTTP listener responses
- Database connectivity and worker logs

---

## License

This project is licensed under the [MIT License](LICENSE) © 2026 Arean Narrayan (arean@synorastudio.in).
