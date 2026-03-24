# Liferay Docker Scripts (Legacy)

> [!WARNING]
> **Succeeded by Liferay Docker Manager (LDM)**
> This project has evolved into a standalone application with a modular architecture, multi-instance orchestration, and advanced state management.
>
> **Please use the new repository for the latest features:**
> **[https://github.com/peterrichards-lr/liferay-docker-manager](https://github.com/peterrichards-lr/liferay-docker-manager)**

---

A collection of automation tools for managing Liferay Portal and DXP instances using Docker. These scripts simplify container orchestration, configuration persistence, and snapshot/restoration workflows.

---

## Compatibility & Intended Use

The scripts in this repository are designed to work as an integrated suite:

- **Runner Script:** `run-liferay-dxp-docker.sh` creates a standardized project layout (e.g., `files/`, `data/`, `osgi/`, `deploy/`).
- **Management Scripts:** `liferay-docker.sh` (Python wrapper) provides a high-level manager for running, snapshoting, and restoring environments. Features a **24-hour Shared Cache** for Docker Hub tags.
- **Snapshot Scripts:** `create-docker-snapshot.sh` and `restore-docker-snapshot.sh` utilize the standardized layout to perform full database and filesystem backups.

---

## Tag Caching & Discovery

To ensure high performance, the scripts cache Docker Hub API responses in `~/.liferay_docker_cache.json`.

- **TTL:** 24 hours.
- **Refresh:** Use the `--refresh` flag to force a new fetch from Docker Hub.
- **Clear:** Run `./liferay-docker.sh clear-cache` to manually wipe the cache file.

---

## Liferay Home Folder Mapping

The scripts create a project root that mirrors **Liferay Home** and bind-mounts these folders into the container for persistence and easy configuration:

| Host Folder                     | Container Path                        | Purpose                                                 |
| :------------------------------ | :------------------------------------ | :------------------------------------------------------ |
| `<root>/data`                   | `/opt/liferay/data`                   | Persistent data (Document Library, Hypersonic DB, etc.) |
| `<root>/deploy`                 | `/mnt/liferay/deploy`                 | Auto-deploy drop-in for JARs, WARs, and Licenses.       |
| `<root>/files`                  | `/mnt/liferay/files`                  | Configuration overlay (e.g., `portal-ext.properties`).  |
| `<root>/osgi/state`             | `/opt/liferay/osgi/state`             | OSGi runtime cache; persisting this speeds up restarts. |
| `<root>/osgi/client-extensions` | `/opt/liferay/osgi/client-extensions` | Location for zip-deployable Client Extensions.          |
| `<root>/.certs`                 | N/A (Host only)                       | Locally generated SSL certificates and Traefik config.  |
| `<root>/backups`                | N/A (Host only)                       | Local snapshots created by the manager.                 |

---

### Automated Local SSL

When a custom `--host-name` is used (and `mkcert` is installed), the scripts automatically:

1. **Generate Certificates**: Creates locally trusted SSL certificates in a `.certs/` folder.
2. **Launch Traefik**: Starts (or reuses) a singleton **global Traefik proxy** (`liferay-proxy-global`) to handle HTTPS.
    - **Shared Network**: All instances are connected via a shared `liferay-net` Docker bridge.
    - **Port Priority**: The script attempts to bind to host port **443** first.
    - **Fallback**: If port 443 is blocked (e.g., missing permissions), it offers to use port **8443**.
    - **Sudo Hint**: Standard HTTPS (443) requires running the script with `sudo` on most systems.
3. **Secure Routing**: Configures Liferay with the necessary labels for seamless TLS termination.

#### Proxy Visibility

You can inspect the global proxy state by checking its logs:

```bash
docker logs liferay-proxy-global
```

---

## Cross-Platform Networking

The scripts are designed to be fully cross-platform (macOS, Windows, Linux) but have specific networking requirements for virtual hostnames:

### macOS (Intel & Silicon)

To use custom hostnames (e.g., `prospect.demo`), you must alias your loopback interface if you aren't using `127.0.0.1`.

- **Requirement**: Run `sudo ifconfig lo0 alias <your-ip> up` for each unique IP.
- **Docker Socket**: The script automatically detects the socket at `~/.docker/run/docker.sock` to handle M1/M2 permission issues.

### Windows

- **Docker Socket**: Uses Named Pipes (`//./pipe/docker_engine`).
- **Pathing**: The script automatically converts Path objects to POSIX format (`/`) to prevent backslash escapes from breaking Docker volume strings.

### Linux

- **Docker Socket**: Defaults to `/var/run/docker.sock`.
- **Permissions**: Ensure your user is in the `docker` group or run the script with `sudo`.

---

## Automated Health Checks

When starting an instance in background mode (default), the script will:

1. **Monitor Readiness**: Poll the access URL until Liferay responds with a `200 OK` or `302 Found`.
2. **Provide Feedback**: Display a progress loop while waiting (typically 2-5 minutes).
3. **Explicit Readiness**: Notify you with a `READY!` message as soon as the instance is live.

---

## Prerequisites

- **Zsh**: Required for all `.sh` scripts. These scripts utilize advanced Zsh-specific features (like parameter expansion flags and reliable path discovery) for robust operation. On macOS, Zsh is the default shell.
- **Docker**: Docker Desktop or Docker Engine installed and running.
- **Python**: 3.10+ required for the management script.
- **mkcert**: (Optional) Required for automated local SSL termination.
- **Database Clients**: (Optional) If using PostgreSQL or MySQL, ensure the respective client (`psql` or `mysql`) is installed and available in your system PATH for snapshot/restore operations.

---

## License

MIT © Peter Richards
