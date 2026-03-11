# Liferay Docker Scripts - Project Context

## Project Overview
This repository contains automation tools for managing Liferay DXP instances using Docker. It provides a Python-based manager with shell and batch wrappers, along with standalone utility scripts for snapshots and restoration.

## Core Mandates
- **Logic Source of Truth**: `liferay_docker.py` is the primary source of business logic for container management.
- **Platform Parity**: Ensure that changes to `.sh` scripts (for macOS/Linux) have equivalent updates in `.bat` scripts (for Windows) where applicable.
- **Idempotency**: All scripts must handle existing containers, volumes, and network configurations gracefully (e.g., using `docker ps -a` checks).
- **Tag Caching**: Docker Hub API responses must be cached in `~/.liferay_docker_cache.json` for 24 hours to ensure high performance. Empty results for specific filters must also be cached to avoid redundant fetches.
- **Atomic Metadata**: Updates to project metadata (`.liferay-docker.meta`) must be atomic, using temporary files and standard replacement (e.g., `os.replace` or `mv`) to prevent corruption.
- **Container Safety**: Before deleting mapped volumes or clearing state, scripts must verify the container is in an `exited` status. A 2-second safety sleep must be enforced after stopping to allow the host OS to release file handles. If the container fails to stop or remains running, the script must abort the deletion operation and warn the user to prevent volume corruption.
- **Snapshot Integrity**: Snapshot tools must verify the state of `data/document_library` and database connectivity before proceeding. DB dumps must be verified for reachability and authentication using dummy queries (e.g., `SELECT 1`) before archiving files. Success must be confirmed via process return codes and resulting archive integrity (non-zero size).
- **Security Safeguards**:
  - **Zip Slip Protection**: Extractions must validate all member paths against the target root to prevent path traversal.
  - **Property Management**: `portal-ext.properties` must be updated using regex-based in-place replacements to avoid duplicate key entries.
- **Database Support**: Maintain support for Hypersonic (default), PostgreSQL, and MySQL.

## Engineering Standards
- **Liferay Versioning**: Adhere to Liferay 7.4+ tag formats (`YYYY.qX.N`).
- **File System Structure**: Respect the expected directory layout:
  - `deploy/`: Liferay deployment folder.
  - `files/`: Configuration files (e.g., `portal-ext.properties`).
  - `data/`: Persistent Liferay data (document library, etc.).
  - `osgi/`: Client extensions and state.
## UI & Interaction Consistency
- **Background Startup**: Containers start in detached mode by default. Users must use `--follow` or `-f` to attach to logs.
- **UI Consistency**: Use the `UI` helper class in Python and `terminal-colors.txt` in shell scripts for consistent logging.

## Definition of Done
- **Manual Verification**:
  1. Run `liferay-docker.sh run` to ensure container creation and startup.
  2. Verify volume mounting by checking `files/portal-ext.properties` inside the container.
  3. Create a snapshot and restore it to ensure data persistence.
- **Automated Tests**: If logic in `liferay_docker.py` is changed, add unit tests to verify the `LiferayManager` command generation.

## Strategic Deployment Control
- These scripts are local development utilities. Do not attempt to "deploy" them to a production environment unless explicitly requested.
- Before suggesting a `docker pull`, inform the user about the expected image size (~2GB-4GB).
