import os
import sys
import json
import time
import shutil
import argparse
import subprocess
import tarfile
import gzip
from datetime import datetime
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import URLError

# --- Constants & Configuration ---
IMAGE_NAME = "liferay/dxp"
API_BASE = "https://hub.docker.com/v2/repositories/liferay/dxp/tags?page_size=200&ordering=name"

# --- UI Helpers ---
class UI:
    COLOR_OFF = '\033[0m'
    GREEN = '\033[0;32m'
    YELLOW = '\033[0;33m'
    WHITE = '\033[0;37m'
    BYELLOW = '\033[1;33m'
    RED = '\033[0;31m'
    BRED = '\033[1;31m'

    @staticmethod
    def info(msg):
        print(f"{UI.YELLOW}{msg}{UI.COLOR_OFF}")

    @staticmethod
    def success(msg):
        print(f"{UI.GREEN}{msg}{UI.COLOR_OFF}")

    @staticmethod
    def error(msg):
        print(f"{UI.BRED}Error:{UI.COLOR_OFF} {msg}", file=sys.stderr)

    @staticmethod
    def die(msg):
        UI.error(msg)
        sys.exit(1)

    @staticmethod
    def ask(prompt, default=None):
        if default:
            res = input(f"{UI.WHITE}{prompt} [{UI.GREEN}{default}{UI.WHITE}]: {UI.COLOR_OFF}")
            return res if res else default
        return input(f"{UI.WHITE}{prompt}: {UI.COLOR_OFF}")

# --- Utilities ---
def run_command(cmd, shell=False, capture_output=True, check=True):
    try:
        result = subprocess.run(
            cmd, shell=shell, capture_output=capture_output, text=True, check=check
        )
        return result.stdout.strip()
    except subprocess.CalledProcessError as e:
        if check:
            return None
        return e.stdout.strip() if e.stdout else ""

def get_json(url):
    try:
        req = Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urlopen(req) as response:
            return json.loads(response.read().decode())
    except Exception as e:
        UI.error(f"Failed to fetch data: {e}")
        return None

def discover_latest_tag(release_type="any", year_filter=None, verbose=False):
    url = API_BASE
    if release_type == "lts": url += "&name=-lts"
    elif release_type == "u": url += "&name=-u"

    if verbose:
        UI.info(f"Discovering latest Docker tag for {release_type} (Year: {year_filter or 'Any'})...")

    tags = []
    while url:
        data = get_json(url)
        if not data: break
        
        for result in data.get('results', []):
            name = result['name']
            if year_filter and not name.startswith(year_filter):
                continue
            
            # Simple regex-like matching for Liferay tags
            # Format: YYYY.qX.N[-uN|-lts]
            import re
            is_valid = False
            if release_type == "lts":
                is_valid = bool(re.match(r'^\d{4}\.q[1-4]\.\d+-lts$', name))
            elif release_type == "u":
                is_valid = bool(re.match(r'^\d{4}\.q[1-4]\.\d+-u\d+$', name))
            elif release_type == "qr":
                is_valid = bool(re.match(r'^\d{4}\.q[1-4]\.\d+$', name))
            else:
                is_valid = bool(re.match(r'^\d{4}\.q[1-4]\.\d+(-u\d+|-lts)?$', name))
            
            if is_valid:
                tags.append(name)
        
        url = data.get('next')

    if not tags:
        return None
    
    # Sort version strings correctly
    from distutils.version import LooseVersion
    tags.sort(key=LooseVersion)
    return tags[-1]

# --- Core Functionality ---

class LiferayManager:
    def __init__(self, args):
        self.args = args
        self.verbose = getattr(args, 'verbose', False)
        self.non_interactive = getattr(args, 'non_interactive', False)

    def setup_paths(self, root_path):
        root = Path(root_path).resolve()
        paths = {
            "root": root,
            "deploy": root / "deploy",
            "data": root / "data",
            "scripts": root / "scripts",
            "files": root / "files",
            "cx": root / "osgi" / "client-extensions",
            "state": root / "osgi" / "state",
            "modules": root / "modules",
            "backups": root / "backups"
        }
        
        # Auto-fix for the 'osgi/modules' mistake in old scripts
        legacy_modules = root / "osgi" / "modules"
        if legacy_modules.exists() and legacy_modules.is_dir():
            UI.info(f"Detected legacy 'osgi/modules' folder. Moving contents to root 'modules'...")
            paths["modules"].mkdir(parents=True, exist_ok=True)
            for item in legacy_modules.iterdir():
                dest = paths["modules"] / item.name
                if not dest.exists():
                    shutil.move(str(item), str(dest))
            shutil.rmtree(legacy_modules)
            
        return paths

    def get_jdbc_params(self, files_dir):
        portal_ext = Path(files_dir) / "portal-ext.properties"
        params = {}
        if portal_ext.exists():
            with open(portal_ext, 'r') as f:
                for line in f:
                    if '=' in line:
                        key, val = line.strip().split('=', 1)
                        params[key.strip()] = val.strip()
        return params

    def cmd_run(self):
        # 1. Tag discovery
        tag = self.args.tag
        if not tag:
            release_type = self.args.release_type or (UI.ask("Release type (any|u|lts|qr)", "any") if not self.non_interactive else "any")
            year = datetime.now().strftime("%Y")
            tag = discover_latest_tag(release_type, year, self.verbose)
            if not tag:
                tag = discover_latest_tag(release_type, None, self.verbose)
            
            if not self.non_interactive:
                tag = UI.ask("Enter Liferay Docker Tag", tag)
            elif not tag:
                UI.die("Could not auto-detect tag. Please provide --tag.")

        # 2. Root path
        root_default = f"./{tag}"
        root_path = self.args.root or (UI.ask("Liferay Root", root_default) if not self.non_interactive else root_default)
        paths = self.setup_paths(root_path)

        # 3. Container name
        container_name = self.args.container or root_path.replace("./", "").replace("/", "-").replace(".", "-")
        
        # Check container existence
        inspect = run_command(["docker", "container", "inspect", container_name], check=False)
        
        if inspect is None:
            UI.info(f"{container_name} does not exist. Creating...")
            
            # Create dirs
            for p in paths.values():
                p.mkdir(parents=True, exist_ok=True)
            
            # Common files
            common_dir = Path("7.4-common")
            if common_dir.exists():
                for f in common_dir.glob("*activationkeys.xml"):
                    shutil.copy(f, paths["deploy"])
                for f in common_dir.glob("*.properties"):
                    shutil.copy(f, paths["files"])

            # DB Setup Logic
            db_kind = getattr(self.args, 'db', None)
            if not db_kind and not self.non_interactive:
                db_kind = UI.ask("Database (postgresql|mysql|hypersonic)", "hypersonic")
            elif not db_kind:
                db_kind = "hypersonic"

            jdbc_lines = []
            if db_kind in ["postgresql", "mysql"]:
                db_name = container_name.replace("-", "")
                user = self.args.jdbc_username or UI.ask("DB Username", "liferay")
                pw = self.args.jdbc_password or (UI.ask("DB Password") if db_kind == "mysql" else None)
                
                if db_kind == "postgresql":
                    UI.info(f"Setting up PostgreSQL database: {db_name}")
                    run_command(["createdb", "-h", "localhost", "-U", user, db_name], check=False)
                    jdbc_lines = [
                        f"jdbc.default.driverClassName=org.postgresql.Driver",
                        f"jdbc.default.url=jdbc:postgresql://host.docker.internal:5432/{db_name}",
                        f"jdbc.default.username={user}"
                    ]
                else:
                    UI.info(f"Setting up MySQL database: {db_name}")
                    pw_arg = f"-p{pw}" if pw else ""
                    run_command(["mysql", "-u", user, pw_arg, "-e", f"CREATE DATABASE IF NOT EXISTS {db_name} CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"], check=False)
                    jdbc_lines = [
                        f"jdbc.default.driverClassName=com.mysql.cj.jdbc.Driver",
                        f"jdbc.default.url=jdbc:mysql://host.docker.internal:3306/{db_name}",
                        f"jdbc.default.username={user}",
                        f"jdbc.default.password={pw}" if pw else ""
                    ]

                portal_ext = paths["files"] / "portal-ext.properties"
                with open(portal_ext, "a") as f:
                    f.write("\n" + "\n".join(filter(None, jdbc_lines)) + "\n")

            # JVM Opts (Zip64)
            env_args = []
            if getattr(self.args, 'disable_zip64', False):
                env_args += ["-e", "LIFERAY_JVM_OPTS=-Djdk.util.zip.disableZip64ExtraFieldValidation=true"]

            # Network
            net_args = ["--network", "host"] if getattr(self.args, 'host_network', False) else ["-p", f"{self.args.port or 8080}:8080"]

            # Build docker command
            docker_cmd = [
                "docker", "create", "-it", 
                "--name", container_name] + net_args + env_args + [
                "-v", f"{paths['files']}:/mnt/liferay/files",
                "-v", f"{paths['scripts']}:/mnt/liferay/scripts",
                "-v", f"{paths['state']}:/opt/liferay/osgi/state",
                "-v", f"{paths['modules']}:/opt/liferay/modules",
                "-v", f"{paths['data']}:/opt/liferay/data",
                "-v", f"{paths['deploy']}:/mnt/liferay/deploy",
                "-v", f"{paths['cx']}:/opt/liferay/osgi/client-extensions",
                f"{IMAGE_NAME}:{tag}"
            ]
            run_command(docker_cmd)
            run_command(["docker", "start", "-i", "-a", container_name], capture_output=False)
        else:
            UI.info(f"{container_name} already exists. Starting...")
            if getattr(self.args, 'delete_state', False):
                UI.info("Deleting OSGi state folder...")
                shutil.rmtree(paths["state"], ignore_errors=True)
                paths["state"].mkdir(parents=True)
            
            run_command(["docker", "start", "-i", "-a", container_name], capture_output=False)

    def cmd_snapshot(self):
        root_path = self.args.root or UI.ask("Liferay Root", os.getcwd())
        paths = self.setup_paths(root_path)
        container_name = self.args.container or Path(root_path).name.replace(".", "-")
        
        is_running = run_command(["docker", "ps", "-q", "-f", f"name={container_name}"])
        stop_needed = (getattr(self.args, 'stop', True) and is_running)
        
        if stop_needed:
            UI.info(f"Stopping {container_name}...")
            run_command(["docker", "stop", container_name])

        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        prefix = self.args.prefix + "-" if self.args.prefix else ""
        snap_dir = paths["backups"] / f"{prefix}{timestamp}"
        snap_dir.mkdir(parents=True, exist_ok=True)

        format_choice = self.args.format or "standard"
        comp = self.args.compression or "gzip"
        meta = {"meta_version": "2", "timestamp": timestamp, "name": self.args.name or "", "format": format_choice, "compression": comp}

        # Compression setup
        comp_ext = ".gz" if comp == "gzip" else (".xz" if comp == "xz" else "")
        tar_mode = "w:gz" if comp == "gzip" else ("w:xz" if comp == "xz" else "w")

        # Files archive
        if not getattr(self.args, 'db_only', False):
            if format_choice == "liferay-cloud":
                arc_path = snap_dir / "volume.tgz"
                UI.info("Archiving document_library (Liferay Cloud format)...")
                with tarfile.open(arc_path, "w:gz") as tar:
                    doclib = paths["data"] / "document_library"
                    if doclib.exists(): tar.add(doclib, arcname="document_library")
            else:
                arc_path = snap_dir / f"files.tar{comp_ext}"
                UI.info(f"Archiving volumes ({comp})...")
                with tarfile.open(arc_path, tar_mode) as tar:
                    for folder in ["files", "scripts", "osgi", "data", "deploy", "modules"]:
                        folder_path = paths["root"] / folder
                        if folder_path.exists(): tar.add(folder_path, arcname=folder)
            meta["files_archive"] = arc_path.name

        # DB Dump
        jdbc = self.get_jdbc_params(paths["files"])
        if jdbc.get("jdbc.default.url") and not getattr(self.args, 'files_only', False):
            url = jdbc["jdbc.default.url"]
            user = jdbc.get("jdbc.default.username", "")
            pw = jdbc.get("jdbc.default.password", "")
            
            dump_file = snap_dir / ("database.gz" if format_choice == "liferay-cloud" else f"db-dump.sql{comp_ext}")
            
            if "postgresql" in url.lower():
                dbname = url.split("/")[-1].split("?")[0]
                UI.info(f"Dumping PostgreSQL {dbname}...")
                env = os.environ.copy()
                if pw: env["PGPASSWORD"] = pw
                
                dump_cmd = ["pg_dump", "-h", "localhost", "-U", user, dbname]
                if format_choice == "liferay-cloud":
                    dump_cmd += ["--no-owner", "--no-privileges"]
                
                with open(dump_file, "wb") as f:
                    p1 = subprocess.Popen(dump_cmd, env=env, stdout=subprocess.PIPE)
                    comp_cmd = ["gzip", "-c"] if comp == "gzip" or format_choice == "liferay-cloud" else (["xz", "-c"] if comp == "xz" else ["cat"])
                    p2 = subprocess.Popen(comp_cmd, stdin=p1.stdout, stdout=f)
                    p2.communicate()
                meta["type"] = "postgresql"
            elif "mysql" in url.lower():
                dbname = url.split("/")[-1].split("?")[0]
                UI.info(f"Dumping MySQL {dbname}...")
                pw_arg = f"-p{pw}" if pw else ""
                with open(dump_file, "wb") as f:
                    p1 = subprocess.Popen(["mysqldump", "-h", "localhost", "-u", user, pw_arg, dbname], stdout=subprocess.PIPE)
                    comp_cmd = ["gzip", "-c"] if comp == "gzip" or format_choice == "liferay-cloud" else (["xz", "-c"] if comp == "xz" else ["cat"])
                    p2 = subprocess.Popen(comp_cmd, stdin=p1.stdout, stdout=f)
                    p2.communicate()
                meta["type"] = "mysql"
            
            meta["db_dump"] = dump_file.name

        with open(snap_dir / "meta.json", "w") as f:
            json.dump(meta, f, indent=2)

        if stop_needed: run_command(["docker", "start", container_name])
        
        # Retention
        if self.args.retention:
            UI.info(f"Pruning backups (keeping newest {self.args.retention})...")
            all_bks = sorted([d for d in paths["backups"].iterdir() if d.is_dir()], reverse=True)
            for old_bk in all_bks[self.args.retention:]:
                UI.info(f"Deleting old backup: {old_bk.name}")
                shutil.rmtree(old_bk)

        UI.success(f"Snapshot created at {snap_dir}")


    def cmd_restore(self):
        root_path = self.args.root or UI.ask("Liferay Root", os.getcwd())
        paths = self.setup_paths(root_path)
        
        backups = sorted([d for d in paths["backups"].iterdir() if d.is_dir()], reverse=True)
        if not backups: UI.die("No backups found.")

        if self.args.index:
            choice = backups[self.args.index - 1]
        else:
            UI.info("Available backups:")
            for i, b in enumerate(backups):
                meta_file = b / "meta.json"
                name = "(unnamed)"
                if meta_file.exists():
                    with open(meta_file) as f: name = json.load(f).get("name", name)
                print(f"  [{i+1}] {name} - {b.name}")
            choice = backups[int(UI.ask("Select backup", "1")) - 1]

        container_name = self.args.container or Path(root_path).name.replace(".", "-")
        is_running = run_command(["docker", "ps", "-q", "-f", f"name={container_name}"])
        if is_running:
            UI.info(f"Stopping {container_name}...")
            run_command(["docker", "stop", container_name])

        UI.info(f"Restoring from {choice.name}...")
        
        # Cleanup state/ES if requested
        if getattr(self.args, 'delete_state', False):
            shutil.rmtree(paths["state"], ignore_errors=True)
            paths["state"].mkdir(parents=True)
        if getattr(self.args, 'delete_es', False):
            shutil.rmtree(paths["data"] / "elasticsearch7", ignore_errors=True)

        meta_file = choice / "meta.json"
        if meta_file.exists():
            with open(meta_file) as f: meta = json.load(f)
            
            # Restore Files
            if "files_archive" in meta:
                arc = choice / meta["files_archive"]
                UI.info(f"Extracting {arc.name}...")
                mode = "r:gz" if arc.name.endswith(".gz") else ("r:xz" if arc.name.endswith(".xz") else "r")
                if meta.get("format") == "liferay-cloud":
                    with tarfile.open(arc, "r:gz") as tar:
                        tar.extractall(path=paths["data"])
                else:
                    with tarfile.open(arc, mode) as tar:
                        tar.extractall(path=paths["root"])

            # Restore DB
            if "db_dump" in meta:
                dump = choice / meta["db_dump"]
                jdbc = self.get_jdbc_params(paths["files"])
                url, user, pw = jdbc.get("jdbc.default.url"), jdbc.get("jdbc.default.username"), jdbc.get("jdbc.default.password")
                
                if "postgresql" in url.lower():
                    dbname = url.split("/")[-1].split("?")[0]
                    UI.info(f"Restoring PostgreSQL {dbname}...")
                    env = os.environ.copy()
                    if pw: env["PGPASSWORD"] = pw
                    # Reset DB
                    run_command(["dropdb", "-h", "localhost", "-U", user, dbname], check=False)
                    run_command(["createdb", "-h", "localhost", "-U", user, dbname])
                    # Import
                    with open(dump, "rb") as f_in:
                        comp_cmd = ["gunzip", "-c"] if dump.name.endswith(".gz") else (["xz", "-dc"] if dump.name.endswith(".xz") else ["cat"])
                        p1 = subprocess.Popen(comp_cmd, stdin=f_in, stdout=subprocess.PIPE)
                        p2 = subprocess.Popen(["psql", "-h", "localhost", "-U", user, "-d", dbname], env=env, stdin=p1.stdout)
                        p2.communicate()
                elif "mysql" in url.lower():
                    dbname = url.split("/")[-1].split("?")[0]
                    UI.info(f"Restoring MySQL {dbname}...")
                    pw_arg = f"-p{pw}" if pw else ""
                    # Reset DB
                    run_command(["mysql", "-u", user, pw_arg, "-e", f"DROP DATABASE IF EXISTS {dbname}; CREATE DATABASE {dbname};"])
                    # Import
                    with open(dump, "rb") as f_in:
                        comp_cmd = ["gunzip", "-c"] if dump.name.endswith(".gz") else (["xz", "-dc"] if dump.name.endswith(".xz") else ["cat"])
                        p1 = subprocess.Popen(comp_cmd, stdin=f_in, stdout=subprocess.PIPE)
                        p2 = subprocess.Popen(["mysql", "-u", user, pw_arg, dbname], stdin=p1.stdout)
                        p2.communicate()

        if is_running: run_command(["docker", "start", container_name])
        UI.success("Restore complete.")


# --- Main CLI ---
def main():
    parser = argparse.ArgumentParser(description="Liferay DXP Docker Manager (Python)")
    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # Run command
    run_parser = subparsers.add_parser("run", help="Run Liferay DXP container")
    run_parser.add_argument("-t", "--tag", help="Liferay Docker image tag")
    run_parser.add_argument("-r", "--root", help="Project root path")
    run_parser.add_argument("-c", "--container", help="Container name")
    run_parser.add_argument("--release-type", choices=["any", "u", "lts", "qr"], help="Tag discovery mode")
    run_parser.add_argument("--db", choices=["postgresql", "mysql", "hypersonic"], help="Database choice")
    run_parser.add_argument("--jdbc-username", help="Username for external DB")
    run_parser.add_argument("--jdbc-password", help="Password for external DB")
    run_parser.add_argument("-p", "--port", type=int, help="Local HTTP port")
    run_parser.add_argument("--host-network", action="store_true", help="Use host networking")
    run_parser.add_argument("--disable-zip64", action="store_true", help="Disable JVM Zip64 validation")
    run_parser.add_argument("--delete-state", action="store_true", help="Delete OSGi state before start")
    run_parser.add_argument("--non-interactive", action="store_true")
    run_parser.add_argument("--verbose", action="store_true")

    # Snapshot command
    snap_parser = subparsers.add_parser("snapshot", help="Create a snapshot")
    snap_parser.add_argument("-r", "--root", help="Project root path")
    snap_parser.add_argument("-c", "--container", help="Container name")
    snap_parser.add_argument("-n", "--name", help="Snapshot name")
    snap_parser.add_argument("--prefix", help="Folder name prefix")
    snap_parser.add_argument("--db-only", action="store_true")
    snap_parser.add_argument("--files-only", action="store_true")
    snap_parser.add_argument("--format", choices=["standard", "liferay-cloud"], help="Backup layout")
    snap_parser.add_argument("--compression", choices=["gzip", "xz", "none"], help="Compression format")
    snap_parser.add_argument("--retention", type=int, help="Number of backups to keep")

    # Restore command
    rest_parser = subparsers.add_parser("restore", help="Restore a snapshot")
    rest_parser.add_argument("-r", "--root", help="Project root path")
    rest_parser.add_argument("-c", "--container", help="Container name")
    rest_parser.add_argument("-i", "--index", type=int, help="Backup index to restore")
    rest_parser.add_argument("--delete-state", action="store_true", help="Delete OSGi state before restore")
    rest_parser.add_argument("--delete-es", action="store_true", help="Delete Elasticsearch data before restore")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return

    manager = LiferayManager(args)
    if args.command == "run": manager.cmd_run()
    elif args.command == "snapshot": manager.cmd_snapshot()
    elif args.command == "restore": manager.cmd_restore()

if __name__ == "__main__":
    main()
