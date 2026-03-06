import os
import re
import sys
import json
import time
import shutil
import argparse
import subprocess
import tarfile
import gzip
import lzma
from datetime import datetime
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import URLError

# --- Constants & Configuration ---
IMAGE_NAME = "liferay/dxp"
API_BASE = "https://hub.docker.com/v2/repositories/liferay/dxp/tags?page_size=200&ordering=name"
META_VERSION = "2"
MIN_META_VERSION = 2

# --- UI Helpers ---
class UI:
    COLOR_OFF = '\033[0m'
    GREEN = '\033[0;32m'
    YELLOW = '\033[0;33m'
    WHITE = '\033[0;37m'
    BYELLOW = '\033[1;33m'
    RED = '\033[0;31m'
    BRED = '\033[1;31m'
    CYAN = '\033[0;36m'

    @staticmethod
    def info(msg):
        print(f"{UI.YELLOW}ℹ {msg}{UI.COLOR_OFF}")

    @staticmethod
    def success(msg):
        print(f"{UI.GREEN}✅ {msg}{UI.COLOR_OFF}")

    @staticmethod
    def error(msg):
        print(f"{UI.BRED}❌ Error:{UI.COLOR_OFF} {msg}", file=sys.stderr)

    @staticmethod
    def die(msg):
        UI.error(msg)
        sys.exit(1)

    @staticmethod
    def heading(msg):
        print(f"\n{UI.BYELLOW}=== {msg} ==={UI.COLOR_OFF}")

    @staticmethod
    def ask(prompt, default=None):
        if default:
            res = input(f"{UI.WHITE}❓ {prompt} [{UI.GREEN}{default}{UI.WHITE}]: {UI.COLOR_OFF}")
            return res if res else default
        return input(f"{UI.WHITE}❓ {prompt}: {UI.COLOR_OFF}")

    @staticmethod
    def format_size(size):
        for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
            if size < 1024:
                return f"{size:.1f} {unit}"
            size /= 1024

# --- Utilities ---
def run_command(cmd, shell=False, capture_output=True, check=True, env=None):
    try:
        result = subprocess.run(
            cmd, shell=shell, capture_output=capture_output, text=True, check=check, env=env
        )
        if result.returncode != 0 and not check:
            return None
        return result.stdout.strip() if result.stdout else ""
    except subprocess.CalledProcessError as e:
        if check:
            raise e
        return None

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
        UI.info(f"Discovering latest Docker tag for {release_type}...")

    tags = []
    while url:
        data = get_json(url)
        if not data: break
        
        for result in data.get('results', []):
            name = result['name']
            if year_filter and not name.startswith(year_filter):
                continue
            
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
    
    def natural_sort_key(s):
        return [int(text) if text.isdigit() else text.lower() for text in re.split('([0-9]+)', s)]

    tags.sort(key=natural_sort_key)
    return tags[-1]

# --- Core Functionality ---

class LiferayManager:
    def __init__(self, args):
        self.args = args
        self.verbose = getattr(args, 'verbose', False)
        self.non_interactive = getattr(args, 'non_interactive', False)

    def detect_root(self):
        # 1. Explicit arg
        if getattr(self.args, 'root', None):
            return Path(self.args.root).resolve()
        
        # 2. Smart detection (is current dir a Liferay root?)
        cwd = Path.cwd()
        if (cwd / "files" / "portal-ext.properties").exists() or (cwd / "deploy").exists():
            return cwd
        
        return None

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
                    line = line.strip()
                    if line and not line.startswith('#') and '=' in line:
                        key, val = line.split('=', 1)
                        params[key.strip()] = val.strip()
        return params

    def read_meta(self, path):
        meta = {}
        if not path.exists(): return meta
        with open(path, 'r') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    k, v = line.split('=', 1)
                    meta[k.strip()] = v.strip()
        return meta

    def write_meta(self, path, meta):
        with open(path, 'w') as f:
            f.write(f"# Generated by Liferay Docker Manager ({datetime.now().isoformat()})\n")
            for k, v in sorted(meta.items()):
                if v is not None:
                    f.write(f"{k}={v}\n")

    def verify_archive(self, file_path):
        UI.info(f"Verifying {file_path.name} integrity...")
        try:
            if file_path.suffix == ".gz":
                with gzip.open(file_path, 'rb') as f:
                    while f.read(1024*1024): pass
            elif file_path.suffix == ".xz":
                with lzma.open(file_path, 'rb') as f:
                    while f.read(1024*1024): pass
            
            if ".tar" in file_path.name or file_path.suffix in [".tgz", ".tar"]:
                with tarfile.open(file_path, 'r:*') as tar:
                    tar.getmembers()
            return True
        except Exception as e:
            UI.error(f"Integrity check failed: {e}")
            return False

    def cmd_run(self):
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

        root_default = self.detect_root() or f"./{tag}"
        root_path = self.args.root or (UI.ask("Liferay Root", root_default) if not self.non_interactive else root_default)
        paths = self.setup_paths(root_path)

        container_name = self.args.container or Path(root_path).name.replace(".", "-")
        
        inspect = run_command(["docker", "ps", "-a", "--filter", f"name=^{container_name}$", "--format", "{{.Names}}"], check=False)
        
        if not inspect or container_name not in inspect.split("\n"):
            UI.heading(f"Initializing {container_name}")
            
            for p in paths.values():
                p.mkdir(parents=True, exist_ok=True)
            
            common_dir = Path("7.4-common")
            if common_dir.exists():
                for f in common_dir.glob("*activationkeys.xml"):
                    shutil.copy(f, paths["deploy"])
                for f in common_dir.glob("*.properties"):
                    shutil.copy(f, paths["files"])

            rm_container = getattr(self.args, 'remove_after', False)
            if not rm_container and not self.non_interactive:
                rm_container = UI.ask("Remove container afterwards?", "Y") == "Y"
            rm_arg = ["--rm"] if rm_container else []

            db_kind = getattr(self.args, 'db', None)
            if not db_kind and not self.non_interactive:
                db_kind = UI.ask("Use Hypersonic database?", "Y")
                db_kind = "hypersonic" if db_kind.upper() == "Y" else None
                if not db_kind:
                    db_kind = UI.ask("Liferay Root - postgresql or mysql", "postgresql")
            elif not db_kind:
                db_kind = "hypersonic"

            jdbc_lines = []
            if db_kind in ["postgresql", "mysql"]:
                db_name = container_name.replace("-", "")
                user = self.args.jdbc_username or UI.ask("Username", "liferay")
                pw = self.args.jdbc_password or (UI.ask("Password") if db_kind == "mysql" else None)
                
                recreate = getattr(self.args, 'recreate_db', False)
                if db_kind == "postgresql":
                    UI.info(f"Database: {db_name}")
                    exists = run_command(["psql", "-lqt", "-c", f"SELECT 1 FROM pg_database WHERE datname='{db_name}'"], check=False)
                    if exists and not recreate and not self.non_interactive:
                        recreate = UI.ask("Recreate database?", "N") == "Y"
                    
                    if recreate or not exists:
                        if recreate: run_command(["dropdb", "-f", "-h", "localhost", "-U", user, db_name], check=False)
                        UI.info(f"Creating PostgreSQL database: {db_name}")
                        run_command(["createdb", "-h", "localhost", "-p", "5432", "-U", user, "-O", user, db_name])
                    
                    jdbc_lines = [
                        f"jdbc.default.driverClassName=org.postgresql.Driver",
                        f"jdbc.default.url=jdbc:postgresql://host.docker.internal:5432/{db_name}",
                        f"jdbc.default.username={user}"
                    ]
                else:
                    UI.info(f"Database: {db_name}")
                    pw_arg = f"-p{pw}" if pw else ""
                    exists = run_command(["mysql", "-u", user, pw_arg, "-e", f"use {db_name}"], check=False)
                    if exists is not None and not recreate and not self.non_interactive:
                        recreate = UI.ask("Recreate database?", "N") == "Y"
                    
                    if recreate or exists is None:
                        if recreate: run_command(["mysql", "-u", user, pw_arg, "-e", f"DROP DATABASE {db_name};"], check=False)
                        UI.info(f"Creating MySQL database: {db_name}")
                        run_command(["mysql", "-u", user, pw_arg, "-e", f"CREATE DATABASE {db_name} CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"])
                    
                    jdbc_lines = [
                        f"jdbc.default.driverClassName=com.mysql.cj.jdbc.Driver",
                        f"jdbc.default.url=jdbc:mysql://host.docker.internal:3306/{db_name}",
                        f"jdbc.default.username={user}",
                        f"jdbc.default.password={pw}" if pw else ""
                    ]

                portal_ext = paths["files"] / "portal-ext.properties"
                with open(portal_ext, "a") as f:
                    f.write("\n" + "\n".join(filter(None, jdbc_lines)) + "\n")

            use_host_net = getattr(self.args, 'host_network', False)
            if not use_host_net and not self.non_interactive:
                use_host_net = UI.ask("Use host network?", "N") == "Y"

            net_args = []
            if use_host_net:
                net_args = ["--network", "host"]
            else:
                port = self.args.port or (int(UI.ask("Local Port", "8080")) if not self.non_interactive else 8080)
                net_args = ["-p", f"{port}:8080"]

            disable_zip64 = getattr(self.args, 'disable_zip64', False)
            if not disable_zip64 and not self.non_interactive:
                disable_zip64 = UI.ask("Disable ZIP64 Extra Field Validation?", "N") == "Y"
            
            env_args = []
            if disable_zip64:
                env_args += ["-e", "LIFERAY_JVM_OPTS=-Djdk.util.zip.disableZip64ExtraFieldValidation=true"]

            image_tag = f"{IMAGE_NAME}:{tag}"
            UI.info(f"Pulling {image_tag}...")
            run_command(["docker", "pull", image_tag], capture_output=False)
            
            docker_cmd = [
                "docker", "create", "-it", 
                "--name", container_name] + rm_arg + net_args + env_args + [
                "-v", f"{paths['files']}:/mnt/liferay/files",
                "-v", f"{paths['scripts']}:/mnt/liferay/scripts",
                "-v", f"{paths['state']}:/opt/liferay/osgi/state",
                "-v", f"{paths['modules']}:/opt/liferay/modules",
                "-v", f"{paths['data']}:/opt/liferay/data",
                "-v", f"{paths['deploy']}:/mnt/liferay/deploy",
                "-v", f"{paths['cx']}:/opt/liferay/osgi/client-extensions",
                image_tag
            ]
            run_command(docker_cmd)
            UI.success(f"Container created: {container_name}")
            run_command(["docker", "start", "-i", "-a", container_name], capture_output=False)
            
            if not rm_container:
                UI.info(f"Stopping {container_name}")
                run_command(["docker", "stop", container_name], check=False)
        else:
            UI.heading(f"Resuming {container_name}")
            
            rm_container = getattr(self.args, 'remove_after', False)
            if not rm_container and not self.non_interactive:
                rm_container = UI.ask("Remove container afterwards?", "N") == "Y"
            
            delete_state = getattr(self.args, 'delete_state', False)
            if not delete_state and not self.non_interactive:
                delete_state = UI.ask("Delete OSGi state folder?", "Y") == "Y"
                
            if delete_state:
                UI.info("Clearing OSGi state...")
                shutil.rmtree(paths["state"], ignore_errors=True)
                paths["state"].mkdir(parents=True)
            
            if getattr(self.args, 'follow', False):
                subprocess.Popen(["docker", "start", container_name])
                UI.info("Following logs (Ctrl+C to stop container)...")
                try:
                    run_command(["docker", "logs", "-f", container_name], capture_output=False)
                except KeyboardInterrupt:
                    pass
            else:
                run_command(["docker", "start", "-i", "-a", container_name], capture_output=False)
            
            if rm_container:
                UI.info(f"Deleting {container_name}")
                run_command(["docker", "rm", "--force", container_name], check=False)
            else:
                UI.info(f"Stopping {container_name}")
                run_command(["docker", "stop", container_name], check=False)

    def cmd_list(self):
        root_path = self.detect_root() or os.getcwd()
        paths = self.setup_paths(root_path)
        
        if not paths["backups"].exists():
            UI.info(f"No backups directory found at {paths['backups']}")
            return

        backups = sorted([d for d in paths["backups"].iterdir() if d.is_dir()], key=lambda x: x.name, reverse=True)
        if not backups:
            UI.info("No snapshots found.")
            return

        UI.heading(f"Snapshots in {paths['backups']}")
        print(f"{'Index':<6} {'Name':<20} {'Date':<20} {'Format':<15} {'Size':<10}")
        print("-" * 75)
        
        for i, b in enumerate(backups):
            meta = self.read_meta(b / "meta")
            name = meta.get("name", "(unnamed)")[:18]
            date = b.name.replace("-", " ")[:19]
            fmt = meta.get("format", "standard")
            
            # Calculate total size
            total_size = sum(f.stat().st_size for f in b.glob('*') if f.is_file())
            size_str = UI.format_size(total_size)
            
            print(f"[{i+1:<3}] {name:<20} {date:<20} {fmt:<15} {size_str:<10}")

    def cmd_snapshot(self):
        root_path = self.detect_root() or UI.ask("Liferay Root", os.getcwd())
        paths = self.setup_paths(root_path)
        container_name = self.args.container or Path(root_path).name.replace(".", "-")
        
        is_running = run_command(["docker", "ps", "-q", "-f", f"name={container_name}"])
        stop_needed = (not getattr(self.args, 'no_stop', False) and is_running)
        
        if stop_needed and not self.non_interactive:
            stop_needed = UI.ask("Stop container during backup?", "Y") == "Y"

        if stop_needed and is_running:
            UI.info(f"Stopping {container_name}...")
            run_command(["docker", "stop", container_name])

        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        prefix = self.args.prefix + "-" if self.args.prefix else ""
        snap_dir = paths["backups"] / f"{prefix}{timestamp}"
        
        # Disk Space check (crude)
        total, used, free = shutil.disk_usage(paths["root"])
        if free < 1024 * 1024 * 500: # 500MB warning
            UI.info("⚠️ Low disk space detected. Backup might fail.")

        snap_dir.mkdir(parents=True, exist_ok=True)

        format_choice = self.args.format or (UI.ask("Backup format (standard|liferay-cloud)", "standard") if not self.non_interactive else "standard")
        comp = self.args.compression or "gzip"
        
        snap_name = self.args.name or (UI.ask("Snapshot Name (optional)", "") if not self.non_interactive else "")
        meta = {
            "meta_version": META_VERSION,
            "timestamp": timestamp,
            "name": snap_name,
            "format": format_choice,
            "compression": comp
        }
        
        if self.args.tag:
            for t in self.args.tag:
                if '=' in t:
                    k, v = t.split('=', 1)
                    meta[f"tag.{k}"] = v

        comp_ext = ".gz" if comp == "gzip" else (".xz" if comp == "xz" else "")
        tar_mode = "w:gz" if comp == "gzip" else ("w:xz" if comp == "xz" else "w")

        # Files archive
        if not getattr(self.args, 'db_only', False):
            UI.heading("Archiving Files")
            if format_choice == "liferay-cloud":
                arc_path = snap_dir / "volume.tgz"
                UI.info("Capturing document_library...")
                with tarfile.open(arc_path, "w:gz") as tar:
                    doclib = paths["data"] / "document_library"
                    if doclib.exists(): tar.add(doclib, arcname="document_library")
            else:
                arc_path = snap_dir / f"files.tar{comp_ext}"
                UI.info(f"Capturing volumes ({comp})...")
                with tarfile.open(arc_path, tar_mode) as tar:
                    for folder in ["files", "scripts", "osgi", "data", "deploy", "modules"]:
                        folder_path = paths["root"] / folder
                        if folder_path.exists(): tar.add(folder_path, arcname=folder)
            meta["files_archive"] = arc_path.name

        # DB Dump
        jdbc = self.get_jdbc_params(paths["files"])
        snap_type = "hypersonic"
        if jdbc.get("jdbc.default.url") and not getattr(self.args, 'files_only', False):
            UI.heading("Dumping Database")
            url = jdbc["jdbc.default.url"]
            user = jdbc.get("jdbc.default.username", "")
            pw = jdbc.get("jdbc.default.password", "")
            
            dump_file = snap_dir / ("database.gz" if format_choice == "liferay-cloud" else f"db-dump.sql{comp_ext}")
            
            if "postgresql" in url.lower():
                dbname = url.split("/")[-1].split("?")[0]
                host = self.args.pg_host or "localhost"
                port = self.args.pg_port or "5432"
                UI.info(f"PostgreSQL: {dbname}")
                env = os.environ.copy()
                if pw: env["PGPASSWORD"] = pw
                
                dump_cmd = ["pg_dump", "-h", host, "-p", port, "-U", user, dbname]
                if format_choice == "liferay-cloud":
                    dump_cmd += ["--no-owner", "--no-privileges"]
                
                with open(dump_file, "wb") as f:
                    p1 = subprocess.Popen(dump_cmd, env=env, stdout=subprocess.PIPE)
                    comp_cmd = ["gzip", "-c"] if comp == "gzip" or format_choice == "liferay-cloud" else (["xz", "-c"] if comp == "xz" else ["cat"])
                    p2 = subprocess.Popen(comp_cmd, stdin=p1.stdout, stdout=f)
                    p2.communicate()
                snap_type = "postgresql"
            elif "mysql" in url.lower():
                dbname = url.split("/")[-1].split("?")[0]
                host = self.args.my_host or "localhost"
                port = self.args.my_port or "3306"
                UI.info(f"MySQL: {dbname}")
                pw_arg = f"-p{pw}" if pw else ""
                with open(dump_file, "wb") as f:
                    p1 = subprocess.Popen(["mysqldump", "-h", host, "-P", port, "-u", user, pw_arg, dbname], stdout=subprocess.PIPE)
                    comp_cmd = ["gzip", "-c"] if comp == "gzip" or format_choice == "liferay-cloud" else (["xz", "-c"] if comp == "xz" else ["cat"])
                    p2 = subprocess.Popen(comp_cmd, stdin=p1.stdout, stdout=f)
                    p2.communicate()
                snap_type = "mysql"
            
            meta["db_dump"] = dump_file.name
        
        meta["type"] = snap_type
        self.write_meta(snap_dir / "meta", meta)

        # Verification
        if getattr(self.args, 'verify', False):
            UI.heading("Verification")
            success = True
            if "files_archive" in meta: success &= self.verify_archive(snap_dir / meta["files_archive"])
            if "db_dump" in meta: success &= self.verify_archive(snap_dir / meta["db_dump"])
            if not success: UI.die("Verification failed.")

        if stop_needed: run_command(["docker", "start", container_name])
        
        # Retention
        if self.args.retention:
            UI.info(f"Pruning backups (keeping {self.args.retention})...")
            all_bks = sorted([d for d in paths["backups"].iterdir() if d.is_dir() and (not prefix or d.name.startswith(prefix))], key=lambda x: x.name, reverse=True)
            for old_bk in all_bks[self.args.retention:]:
                UI.info(f"Pruning: {old_bk.name}")
                shutil.rmtree(old_bk)

        UI.success(f"Snapshot saved: {snap_dir}")


    def cmd_restore(self):
        root_path = self.detect_root() or UI.ask("Liferay Root", os.getcwd())
        paths = self.setup_paths(root_path)
        
        backups = sorted([d for d in paths["backups"].iterdir() if d.is_dir()], key=lambda x: x.name, reverse=True)
        if not backups: UI.die("No snapshots available.")

        if self.args.index:
            choice = backups[self.args.index - 1]
        elif getattr(self.args, 'checkpoint', None):
            choice = paths["backups"] / self.args.checkpoint
            if not choice.exists(): UI.die(f"Snapshot {self.args.checkpoint} not found.")
        else:
            self.cmd_list()
            choice = backups[int(UI.ask("Select snapshot index", "1")) - 1]

        container_name = self.args.container or Path(root_path).name.replace(".", "-")
        is_running = run_command(["docker", "ps", "-q", "-f", f"name={container_name}"])
        
        stop_needed = True if is_running else False
        if stop_needed and not self.non_interactive:
            stop_needed = UI.ask("Stop container during restore?", "Y") == "Y"
            
        if stop_needed and is_running:
            UI.info(f"Stopping {container_name}...")
            run_command(["docker", "stop", container_name])

        UI.heading(f"Restoring {choice.name}")
        
        delete_state = getattr(self.args, 'delete_state', False)
        if not delete_state and not self.non_interactive:
            delete_state = UI.ask("Delete OSGi state?", "Y") == "Y"
        if delete_state:
            shutil.rmtree(paths["state"], ignore_errors=True)
            paths["state"].mkdir(parents=True)
            
        delete_es = getattr(self.args, 'delete_es', False)
        if not delete_es and not self.non_interactive:
            delete_es = UI.ask("Delete Elasticsearch?", "Y") == "Y"
        if delete_es:
            shutil.rmtree(paths["data"] / "elasticsearch7", ignore_errors=True)

        meta = self.read_meta(choice / "meta")
        if not meta:
            UI.info("Missing meta file. Using heuristics...")
            meta["format"] = "liferay-cloud" if (choice / "database.gz").exists() or (choice / "volume.tgz").exists() else "standard"
            sqls = list(choice.glob("*.sql*"))
            if sqls: meta["db_dump"] = sqls[0].name
            tars = list(choice.glob("files.tar*"))
            if tars: meta["files_archive"] = tars[0].name
        
        # Version check
        meta_ver = int(meta.get("meta_version", 1))
        if meta_ver < MIN_META_VERSION and not getattr(self.args, 'allow_legacy', False):
            UI.die(f"Backup version {meta_ver} is unsupported. Use --allow-legacy.")

        # Restore Files
        arc_name = meta.get("files_archive") or ( "volume.tgz" if meta.get("format") == "liferay-cloud" else None )
        if arc_name:
            arc = choice / arc_name
            if arc.exists():
                UI.info(f"Restoring Files: {arc.name}")
                if meta.get("format") == "liferay-cloud":
                    with tarfile.open(arc, "r:gz") as tar:
                        tar.extractall(path=paths["data"])
                else:
                    with tarfile.open(arc, "r:*") as tar:
                        tar.extractall(path=paths["root"])

        # Restore DB
        dump_name = meta.get("db_dump") or ( "database.gz" if meta.get("format") == "liferay-cloud" else None )
        if dump_name:
            dump = choice / dump_name
            if dump.exists():
                jdbc = self.get_jdbc_params(paths["files"])
                url, user, pw = jdbc.get("jdbc.default.url"), jdbc.get("jdbc.default.username"), jdbc.get("jdbc.default.password")
                
                if url and "postgresql" in url.lower():
                    dbname = url.split("/")[-1].split("?")[0]
                    host = self.args.pg_host or "localhost"
                    port = self.args.pg_port or "5432"
                    UI.info(f"Restoring PostgreSQL: {dbname}")
                    env = os.environ.copy()
                    if pw: env["PGPASSWORD"] = pw
                    
                    term_sql = f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '{dbname}' AND pid <> pg_backend_pid();"
                    run_command(["psql", "-h", host, "-p", port, "-U", user, "-d", "postgres", "-c", term_sql], env=env, check=False)
                    run_command(["psql", "-h", host, "-p", port, "-U", user, "-d", "postgres", "-c", f"DROP DATABASE IF EXISTS \"{dbname}\";"], env=env)
                    run_command(["psql", "-h", host, "-p", port, "-U", user, "-d", "postgres", "-c", f"CREATE DATABASE \"{dbname}\" WITH TEMPLATE template0 ENCODING 'UTF8';"], env=env)
                    
                    with open(dump, "rb") as f_in:
                        comp_cmd = ["gunzip", "-c"] if dump.name.endswith(".gz") else (["xz", "-dc"] if dump.name.endswith(".xz") else ["cat"])
                        p1 = subprocess.Popen(comp_cmd, stdin=f_in, stdout=subprocess.PIPE)
                        p2 = subprocess.Popen(["psql", "-h", host, "-p", port, "-U", user, "-d", dbname], env=env, stdin=p1.stdout)
                        p2.communicate()
                elif url and "mysql" in url.lower():
                    dbname = url.split("/")[-1].split("?")[0]
                    host = self.args.my_host or "localhost"
                    port = self.args.my_port or "3306"
                    UI.info(f"Restoring MySQL: {dbname}")
                    pw_arg = f"-p{pw}" if pw else ""
                    run_command(["mysql", "-h", host, "-P", port, "-u", user, pw_arg, "-e", f"DROP DATABASE IF EXISTS `{dbname}`; CREATE DATABASE `{dbname}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"])
                    with open(dump, "rb") as f_in:
                        comp_cmd = ["gunzip", "-c"] if dump.name.endswith(".gz") else (["xz", "-dc"] if dump.name.endswith(".xz") else ["cat"])
                        p1 = subprocess.Popen(comp_cmd, stdin=f_in, stdout=subprocess.PIPE)
                        p2 = subprocess.Popen(["mysql", "-h", host, "-P", port, "-u", user, pw_arg, dbname], stdin=p1.stdout)
                        p2.communicate()

        delete_bk = getattr(self.args, 'delete_after', False)
        if not delete_bk and not self.non_interactive:
            delete_bk = UI.ask("Delete snapshot after restore?", "N") == "Y"
        if delete_bk:
            UI.info(f"Cleaning up snapshot...")
            shutil.rmtree(choice)

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
    run_parser.add_argument("-f", "--follow", action="store_true", help="Follow logs after start")
    run_parser.add_argument("--release-type", choices=["any", "u", "lts", "qr"], help="Tag discovery mode")
    run_parser.add_argument("--db", choices=["postgresql", "mysql", "hypersonic"], help="Database choice")
    run_parser.add_argument("--jdbc-username", help="Username for external DB")
    run_parser.add_argument("--jdbc-password", help="Password for external DB")
    run_parser.add_argument("--recreate-db", action="store_true", help="Recreate DB if it exists")
    run_parser.add_argument("-p", "--port", type=int, help="Local HTTP port")
    run_parser.add_argument("--host-network", action="store_true", help="Use host networking")
    run_parser.add_argument("--disable-zip64", action="store_true", help="Disable JVM Zip64 validation")
    run_parser.add_argument("--delete-state", action="store_true", help="Delete OSGi state before start")
    run_parser.add_argument("--remove-after", action="store_true", help="Remove container after stop")
    run_parser.add_argument("--non-interactive", action="store_true")
    run_parser.add_argument("--verbose", action="store_true")

    # List command
    subparsers.add_parser("list", help="List available snapshots")

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
    snap_parser.add_argument("--tag", action="append", help="Add custom tag (key=value)")
    snap_parser.add_argument("--verify", action="store_true", help="Verify archive integrity")
    snap_parser.add_argument("--no-stop", action="store_true", help="Do not stop container")
    snap_parser.add_argument("--pg-host", help="PostgreSQL host override")
    snap_parser.add_argument("--pg-port", help="PostgreSQL port override")
    snap_parser.add_argument("--my-host", help="MySQL host override")
    snap_parser.add_argument("--my-port", help="MySQL port override")
    snap_parser.add_argument("--non-interactive", action="store_true")
    snap_parser.add_argument("--verbose", action="store_true")

    # Restore command
    rest_parser = subparsers.add_parser("restore", help="Restore a snapshot")
    rest_parser.add_argument("-r", "--root", help="Project root path")
    rest_parser.add_argument("-c", "--container", help="Container name")
    rest_parser.add_argument("-i", "--index", type=int, help="Backup index to restore")
    rest_parser.add_argument("--checkpoint", help="Exact checkpoint folder name")
    rest_parser.add_argument("--delete-state", action="store_true", help="Delete OSGi state before restore")
    rest_parser.add_argument("--delete-es", action="store_true", help="Delete Elasticsearch data before restore")
    rest_parser.add_argument("--delete-after", action="store_true", help="Delete snapshot after restore")
    rest_parser.add_argument("--allow-legacy", action="store_true", help="Allow older meta_version")
    rest_parser.add_argument("--pg-host", help="PostgreSQL host override")
    rest_parser.add_argument("--pg-port", help="PostgreSQL port override")
    rest_parser.add_argument("--my-host", help="MySQL host override")
    rest_parser.add_argument("--my-port", help="MySQL port override")
    rest_parser.add_argument("--non-interactive", action="store_true")
    rest_parser.add_argument("--verbose", action="store_true")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return

    manager = LiferayManager(args)
    if args.command == "run": manager.cmd_run()
    elif args.command == "list": manager.cmd_list()
    elif args.command == "snapshot": manager.cmd_snapshot()
    elif args.command == "restore": manager.cmd_restore()

if __name__ == "__main__":
    main()
