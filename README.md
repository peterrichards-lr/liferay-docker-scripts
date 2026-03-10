# Liferay Docker Scripts

A collection of professional automation tools for managing Liferay Portal and DXP instances using Docker. These scripts simplify container orchestration, configuration persistence, and snapshot/restoration workflows.

---

## Compatibility & Intended Use

The scripts in this repository are designed to work as an integrated suite:

- **Runner Script:** `run-liferay-dxp-docker.sh` creates a standardized project layout (e.g., `files/`, `data/`, `osgi/`, `deploy/`).
- **Management Scripts:** `liferay-docker.sh` (Python wrapper) provides a high-level manager for running, snapshoting, and restoring environments.
- **Snapshot Scripts:** `create-docker-snapshot.sh` and `restore-docker-snapshot.sh` utilize the standardized layout to perform full database and filesystem backups.

---

## Liferay Home Folder Mapping

The scripts create a project root that mirrors **Liferay Home** and bind-mounts these folders into the container for persistence and easy configuration:

| Host Folder | Container Path | Purpose |
| :--- | :--- | :--- |
| `<root>/data` | `/opt/liferay/data` | Persistent data (Document Library, Hypersonic DB, etc.) |
| `<root>/deploy` | `/mnt/liferay/deploy` | Auto-deploy drop-in for JARs, WARs, and Licenses. |
| `<root>/files` | `/mnt/liferay/files` | Configuration overlay (e.g., `portal-ext.properties`). |
| `<root>/osgi/state` | `/opt/liferay/osgi/state` | OSGi runtime cache; persisting this speeds up restarts. |
| `<root>/osgi/client-extensions` | `/opt/liferay/osgi/client-extensions` | Location for zip-deployable Client Extensions. |
| `<root>/backups` | N/A (Host only) | Local snapshots created by the manager. |

---

## Quick Start Examples

### 1. Run a Standard DXP Instance
Launch a Liferay DXP instance on the default port (8080) with Hypersonic:
```bash
./liferay-docker.sh run --tag 2025.q4.11
```
*Note: The container starts in the background. Use `--follow` to attach to the logs.*

### 2. Run a Liferay Portal Instance
Use the `--portal` flag to switch to the open-source Liferay Portal image:
```bash
./liferay-docker.sh run --portal
```

### 3. Running Multiple Isolated Instances
Use unique virtual hostnames and loopback IPs to run instances side-by-side on the **same port**:
```bash
# Instance A (on 127.0.0.1:8080)
./liferay-docker.sh run --host-name portal-a.local -p 8080

# Instance B (on 127.0.0.74:8080)
./liferay-docker.sh run --host-name portal-b.local -p 8080
```
*Note: Ensure your `hosts` file maps these domains to the respective loopback IPs. See [hosts.example](hosts.example).*

### 4. Create and Restore Snapshots
```bash
# Create a standard snapshot
./liferay-docker.sh snapshot --name "Pre-Upgrade Backup"

# View available snapshots
./liferay-docker.sh snapshots

# Restore the latest snapshot
./liferay-docker.sh restore
```

---

## Command Reference

### `run` command options

| Option | Description | Default |
| :--- | :--- | :--- |
| `-t, --tag <tag>` | Docker image tag (e.g., `2024.q1.5`). | Latest available |
| `--portal` | Use Liferay Portal (`liferay/portal`) instead of DXP. | DXP |
| `-r, --root <path>` | Project root directory. | `./<tag>` |
| `--host-name <host>` | Virtual hostname (e.g., `liferay.local`). Enables session isolation. | `localhost` |
| `-p, --port <port>` | Local HTTP port mapping. | `8080` |
| `--es-port <port>` | Elasticsearch sidecar HTTP port. | `9200` |
| `--db <type>` | Database type: `postgresql`, `mysql`, or `hypersonic`. | `hypersonic` |
| `--disable-zip64` | Disable JVM Zip64 extra field validation. | Enabled |
| `--select` | Browse and select from existing managed folders. | N/A |
| `-f, --follow` | Start container and automatically follow logs. | Background only |
| `--remove-after` | Automatically remove the container after it stops. | Off |

---

## Advanced Features

### Virtual Host & Session Isolation
When using `--host-name`, the scripts automatically:
1.  **Rename Cookies:** Sets a unique `SESSION_COOKIE_NAME` (e.g., `LFR_SESSION_ID_portal_a_local`) to prevent cross-instance logouts.
2.  **Domain Scoping:** Configures Liferay to scope cookies to the specific virtual domain.
3.  **Security Whitelisting:** Adds the hostname and resolved IP to `virtual.hosts.valid.hosts` to prevent 403 Forbidden errors.

### Proactive Collision Detection
The scripts perform "Fast-Fail" checks before starting a container to prevent environment corruption:
- **DNS/Binding Verification:** Ensures custom hostnames resolve to a bindable `127.x.x.x` loopback address.
- **Port Conflict Detection:** Detects if the requested HTTP or Elasticsearch port is already occupied on the **same loopback IP** by another running Liferay container.
- **Database Collision Detection:** Parses and normalizes JDBC URLs from `portal-ext.properties` to warn you if two instances are attempting to use the **exact same database schema** on the same server.

---

## License
MIT © Peter Richards
