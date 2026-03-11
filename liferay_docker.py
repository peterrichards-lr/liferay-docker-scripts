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
import socket
from datetime import datetime
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import URLError

# --- Constants & Configuration ---
IMAGE_NAME_DXP = "liferay/dxp"
IMAGE_NAME_PORTAL = "liferay/portal"
API_BASE_DXP = "https://hub.docker.com/v2/repositories/liferay/dxp/tags?page_size=200&ordering=name"
API_BASE_PORTAL = "https://hub.docker.com/v2/repositories/liferay/portal/tags?page_size=200&ordering=name"
META_VERSION = "2"
MIN_META_VERSION = 2
PROJECT_META_FILE = ".liferay-docker.meta"
TAG_PATTERN = r'^\d{4}\.q[1-4]\.\d+(-u\d+|-lts)?$'
SCRIPT_DIR = Path(__file__).parent.resolve()

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
        try:
            if default:
                res = input(f"{UI.WHITE}❓ {prompt} [{UI.GREEN}{default}{UI.WHITE}]: {UI.COLOR_OFF}")
                return res if res else default
            return input(f"{UI.WHITE}❓ {prompt}: {UI.COLOR_OFF}")
        except KeyboardInterrupt:
            print("\n")
            UI.info("Interrupted by user. Exiting...")
            sys.exit(130)

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
        if e.returncode == 130:
            raise KeyboardInterrupt()
        if check:
            raise e
        return None
    except KeyboardInterrupt:
        raise

def get_json(url):
    try:
        req = Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urlopen(req) as response:
            return json.loads(response.read().decode())
    except Exception as e:
        UI.error(f"Failed to fetch data: {e}")
        return None

def discover_latest_tag(api_url, release_type="any", year_filter=None, verbose=False, refresh=False):
    cache_path = Path.home() / ".liferay_docker_cache.json"
    cache_key = f"{api_url}_{release_type}_{year_filter}"
    
    if not refresh and cache_path.exists():
        try:
            with open(cache_path, "r") as f:
                cache = json.load(f)
                if cache_key in cache:
                    entry = cache[cache_key]
                    if time.time() - entry.get("timestamp", 0) < 86400:
                        val = entry.get("tag")
                        # Return empty string as signal for "cached but no result"
                        return val if val != "" else ""
        except Exception:
            pass

    print("Initial tag discovery (this may take a few seconds)...")
    start_time = time.time()
    
    url = api_url
    if release_type == "lts": url += "&name=-lts"
    elif release_type == "u": url += "&name=-u"

    if verbose:
        UI.info(f"Discovering latest Docker tag for {release_type}...")

    tags = []
    page = 0
    while url:
        page += 1
        sys.stdout.write(f"\rFetching page {page}...")
        sys.stdout.flush()
        
        data = get_json(url)
        if not data: break
        
        for result in data.get('results', []):
            name = result['name']
            if year_filter and not name.startswith(year_filter):
                continue
            
            is_valid = bool(re.match(TAG_PATTERN, name))
            if is_valid:
                tags.append(name)
        
        url = data.get('next')

    duration = time.time() - start_time
    print(f"\nFetched {page} pages in {duration:.1f}s")

    def natural_sort_key(s):
        return [int(text) if text.isdigit() else text.lower() for text in re.split('([0-9]+)', s)]

    latest_tag = ""
    if tags:
        tags.sort(key=natural_sort_key)
        latest_tag = tags[-1]

    try:
        cache = {}
        if cache_path.exists():
            with open(cache_path, "r") as f:
                cache = json.load(f)
        cache[cache_key] = {"tag": latest_tag, "timestamp": time.time()}
        with open(cache_path, "w") as f:
            json.dump(cache, f)
    except Exception:
        pass
        
    return latest_tag if latest_tag != "" else None

# --- Core Functionality ---

class LiferayManager:
    def __init__(self, args):
        self.args = args
        self.verbose = getattr(args, 'verbose', False)
        self.non_interactive = getattr(args, 'non_interactive', False)
        
        # Ensure all 'run' command attributes are present to avoid AttributeError
        # when forcing command='run' via top-level --select
        run_attrs = [
            'tag', 'root', 'container', 'follow', 'release_type', 'db', 
            'jdbc_username', 'jdbc_password', 'recreate_db', 'port', 
            'host_network', 'host_name', 'es_port', 'disable_zip64', 'delete_state', 'remove_after', 'portal', 'refresh'
        ]
        for attr in run_attrs:
            if not hasattr(self.args, attr):
                setattr(self.args, attr, None)

    def detect_root(self):
        # 1. Explicit arg
        if getattr(self.args, 'root', None):
            return Path(self.args.root).resolve()
        
        # 2. Smart detection (is current dir a Liferay root?)
        cwd = Path.cwd()
        if (cwd / "files" / "portal-ext.properties").exists() or (cwd / "deploy").exists():
            return cwd
        
        return None

    def find_dxp_roots(self, search_dir=None):
        search_dir = Path(search_dir or Path.cwd())
        roots = []
        if not search_dir.exists(): return roots
        for item in search_dir.iterdir():
            if item.is_dir() and not item.name.startswith('.'):
                # Heuristic: must have files/ or deploy/ or .meta
                if (item / "files").exists() or (item / "deploy").exists() or (item / PROJECT_META_FILE).exists():
                    meta = self.read_meta(item / PROJECT_META_FILE)
                    version = meta.get("tag")
                    if not version:
                        if re.match(TAG_PATTERN, item.name):
                            version = item.name
                        else:
                            version = "unknown"
                    roots.append({"path": item, "version": version})
        return sorted(roots, key=lambda x: x["path"].name)

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
            self.safe_rmtree(legacy_modules, root=root)
            
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
        path = Path(path)
        tmp_path = path.with_suffix(".tmp")
        with open(tmp_path, 'w') as f:
            f.write(f"# Generated by Liferay Docker Manager ({datetime.now().isoformat()})\n")
            for k, v in sorted(meta.items()):
                if v is not None:
                    f.write(f"{k}={v}\n")
        os.replace(tmp_path, path)

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

    def check_docker(self):
        try:
            # Check if server is reachable
            run_command(["docker", "version", "--format", "{{.Server.Version}}"], capture_output=True)
            return True
        except Exception:
            return False

    def get_resolved_ip(self, host_name):
        if not host_name or host_name == "localhost":
            return "127.0.0.1"
        try:
            return socket.gethostbyname(host_name)
        except socket.gaierror:
            return None

    def is_bindable(self, ip):
        """Checks if the IP address is actually bound to an interface and ready for listening."""
        try:
            # Try to create a dummy socket and bind to it on an ephemeral port
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind((ip, 0))
            return True
        except Exception as e:
            if self.verbose:
                UI.info(f"Bind check for {ip} failed: {e}")
            return False

    def is_port_available(self, port, ip="127.0.0.1"):
        """Checks if a specific port is available on the host OS."""
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(0.5)
                s.bind((ip, int(port)))
            return True
        except Exception:
            return False

    def check_hostname(self, host_name):
        if not host_name or host_name == "localhost":
            return True
        
        UI.info(f"Verifying resolution for '{host_name}'...")
        ip = self.get_resolved_ip(host_name)
        
        if not ip:
            UI.error(f"Hostname '{host_name}' could not be resolved.")
            print(f"\n{UI.BRED}IMPORTANT:{UI.COLOR_OFF} You must map '{host_name}' to a loopback address (e.g. 127.0.0.1) in your OS hosts file.")
            print(f"Edit {UI.CYAN}/etc/hosts{UI.COLOR_OFF} (macOS/Linux) or {UI.CYAN}C:\\Windows\\System32\\drivers\\etc\\hosts{UI.COLOR_OFF} (Windows).")
            print(f"Add the following line:\n{UI.GREEN}127.0.0.1  {host_name}{UI.COLOR_OFF}")
            if self.non_interactive: sys.exit(1)
            return UI.ask("Continue anyway?", "N").upper() == "Y"

        if not (ip.startswith("127.") or ip in ["::1", "0:0:0:0:0:0:0:1"]):
            UI.error(f"Hostname '{host_name}' resolves to {ip}, which is not a local loopback address.")
            if self.non_interactive: sys.exit(1)
            return UI.ask("Continue anyway?", "N").upper() == "Y"

        UI.success(f"Resolved '{host_name}' to loopback address {ip}")
        
        # Verify if IP is actually bindable (especially important for macOS)
        if not self.is_bindable(ip):
            UI.error(f"IP address {ip} is not available for binding on this host.")
            self.print_macos_alias_advice(ip)
            
            if self.non_interactive:
                sys.exit(1)
            
            if not UI.ask("Continue anyway?", "N").upper() == "Y":
                sys.exit(1)

        return True

    def print_macos_alias_advice(self, ip):
        if sys.platform == "darwin":
            print(f"\n{UI.BRED}OSX DETECTED:{UI.COLOR_OFF} You must alias this IP to your loopback interface.")
            print(f"Run the following command in your terminal:")
            print(f"{UI.CYAN}sudo ifconfig lo0 alias {ip} up{UI.COLOR_OFF}\n")
        else:
            print(f"\nEnsure your network interface is configured to handle {ip}.")

    def normalize_jdbc_url(self, url):
        """Extracts host, port, and database name for comparison."""
        if not url: return None
        # Pattern for standard PostgreSQL/MySQL JDBC URLs
        pattern = r'^jdbc:(postgresql|mysql|mariadb)://([^:/]+)(?::(\d+))?/([^/?]+)'
        match = re.match(pattern, url)
        if match:
            driver, host, port, db_name = match.groups()
            # Normalize host
            if host in ['localhost', 'host.docker.internal']: host = '127.0.0.1'
            # Default ports
            if not port:
                port = '5432' if driver == 'postgresql' else '3306'
            return (driver, host, port, db_name)
        return url

    def update_portal_ext(self, path, updates):
        """Updates or appends properties in portal-ext.properties using regex to avoid duplicates."""
        content = ""
        if path.exists():
            with open(path, 'r') as f:
                content = f.read()
        
        for key, value in updates.items():
            # Match key=value, ignoring leading/trailing whitespace on key
            # Pattern: ^\s*key\s*=.*$
            pattern = re.compile(rf"^\s*{re.escape(key)}\s*=.*$", re.MULTILINE)
            if pattern.search(content):
                content = pattern.sub(f"{key}={value}", content)
            else:
                if content and not content.endswith('\n'):
                    content += '\n'
                content += f"{key}={value}\n"
                
        with open(path, 'w') as f:
            f.write(content)

    def is_within_root(self, path, root):
        try:
            path = Path(path).resolve()
            root = Path(root).resolve()
            return root in path.parents or path == root
        except Exception:
            return False

    def wait_for_container_stop(self, container_name, timeout=30):
        """Verify the container is fully stopped and unmounted."""
        start_time = time.time()
        while time.time() - start_time < timeout:
            inspect_raw = run_command(["docker", "inspect", "-f", "{{.State.Status}} {{.State.Running}}", container_name], check=False)
            if not inspect_raw:
                return True # Container no longer exists
            
            parts = inspect_raw.split()
            status = parts[0]
            running = parts[1].lower() == "true"
            
            # Target status must be exited and running must be false
            if status == "exited" and not running:
                return True
            time.sleep(1)
        return False

    def safe_rmtree(self, path, root=None):
        path = Path(path).resolve()
        if not path.exists():
            return True
            
        if root:
            root = Path(root).resolve()
            if not self.is_within_root(path, root):
                UI.error(f"Safety violation: Attempted to delete {path} which is outside root {root}")
                return False
        
        # Simple retry logic for Windows/macOS file handles
        for i in range(5):
            try:
                shutil.rmtree(path)
                return True
            except Exception as e:
                if i == 4:
                    UI.error(f"Failed to delete {path}: {e}")
                    return False
                if self.verbose:
                    UI.info(f"Retry deleting {path.name} ({i+1}/5)...")
                time.sleep(2)
        return False

    def safe_extract(self, tar, path):
        """Zip Slip protection: ensures all extracted members stay within the target path."""
        target_path = Path(path).resolve()
        for member in tar.getmembers():
            member_path = (target_path / member.name).resolve()
            # Path.resolve() handles .. and symlinks. We check if target_path is a parent.
            try:
                member_path.relative_to(target_path)
            except ValueError:
                UI.die(f"Security Alert: Attempted path traversal in archive: {member.name}")
        
        # Use 'data' filter for Python 3.12+ security standards
        if sys.version_info >= (3, 12):
            tar.extractall(path=target_path, filter='data')
        else:
            tar.extractall(path=target_path)

    def cmd_run(self):
        if not self.check_docker():
            UI.die("Docker is not running or not accessible. Please start Docker and try again.")

        if getattr(self.args, 'select', False):
            roots = self.find_dxp_roots()
            UI.heading("Available DXP Folders")
            for i, root in enumerate(roots):
                print(f"[{i+1}] {root['path'].name} [{UI.CYAN}{root['version']}{UI.COLOR_OFF}]")
            print(f"[{len(roots)+1}] Create New...")
            
            choice = UI.ask("Select folder index", "1")
            try:
                idx = int(choice) - 1
                if idx == len(roots):
                    self.args.root = None
                    self.args.tag = None
                elif 0 <= idx < len(roots):
                    self.args.root = str(roots[idx]['path'])
                    if not self.args.tag and roots[idx]['version'] != "unknown":
                        self.args.tag = roots[idx]['version']
                else:
                    UI.die("Invalid selection.")
            except ValueError:
                UI.die("Invalid input. Please enter a number.")

        root_path = self.args.root or self.detect_root()
        project_meta = {}
        if root_path and Path(root_path).exists():
            project_meta = self.read_meta(Path(root_path) / PROJECT_META_FILE)

        # Early Conflict & Hostname Check
        port = self.args.port or project_meta.get("port")
        host_name = self.args.host_name or project_meta.get("host_name")
        es_port = self.args.es_port or project_meta.get("es_port")
        container_name = self.args.container or project_meta.get("container_name") or (Path(root_path).name.replace(".", "-") if root_path else None)
        db_kind = getattr(self.args, 'db', None) or project_meta.get("db_type")

        if host_name and not self.check_hostname(host_name):
            sys.exit(1)

        if not self.non_interactive or self.args.port or self.args.host_name or self.args.es_port:
            search_dir = Path(root_path).parent if root_path else Path.cwd()
            other_projects = self.find_dxp_roots(search_dir)
            
            curr_jdbc = {}
            if root_path:
                curr_jdbc = self.get_jdbc_params(Path(root_path) / "files")
            
            curr_normalized = self.normalize_jdbc_url(curr_jdbc.get("jdbc.default.url"))
            
            # Predict JDBC URL for new projects to detect collisions early
            if not curr_normalized and db_kind in ["postgresql", "mysql"] and container_name:
                db_name = container_name.replace("-", "")
                if db_kind == "postgresql":
                    curr_normalized = self.normalize_jdbc_url(f"jdbc:postgresql://host.docker.internal:5432/{db_name}")
                else:
                    curr_normalized = self.normalize_jdbc_url(f"jdbc:mysql://host.docker.internal:3306/{db_name}")
            
            for proj in other_projects:
                if root_path and proj["path"].resolve() == Path(root_path).resolve():
                    continue
                
                meta = self.read_meta(proj["path"] / PROJECT_META_FILE)

                # Database collision check (scan all projects, running or not)
                # Check meta file first, then fallback to properties
                proj_jdbc_url = meta.get("jdbc_url")
                if not proj_jdbc_url:
                    proj_jdbc_params = self.get_jdbc_params(proj["path"] / "files")
                    proj_jdbc_url = proj_jdbc_params.get("jdbc.default.url")
                
                proj_normalized = self.normalize_jdbc_url(proj_jdbc_url)
                
                if curr_normalized and proj_normalized and curr_normalized == proj_normalized:
                    UI.error(f"Database collision! This project uses the same database as project '{proj['path'].name}':")
                    if isinstance(curr_normalized, tuple):
                        driver, host, db_port, db_name = curr_normalized
                        print(f" {UI.WHITE}Database: {db_name} on {host}:{db_port} ({driver}){UI.COLOR_OFF}")
                    else:
                        print(f" {UI.WHITE}{curr_normalized}{UI.COLOR_OFF}")
                    
                    if self.non_interactive: sys.exit(1)
                    if UI.ask("Continue anyway?", "N").upper() != "Y": sys.exit(1)

                # Check if project is likely running (container exists)
                check_cmd = ["docker", "ps", "--filter", f"name=^{meta.get('container_name')}$", "--format", "{{.Names}}"]
                if run_command(check_cmd, check=False):
                    m_ip = self.get_resolved_ip(meta.get("host_name")) or "127.0.0.1"
                    r_ip = self.get_resolved_ip(host_name) or "127.0.0.1"
                    
                    if port and str(port) == meta.get("port") and m_ip == r_ip:
                        UI.error(f"Port {port} on {r_ip} is already in use by running container '{meta.get('container_name')}'")
                        if self.non_interactive: sys.exit(1)
                        port = int(UI.ask("Enter a different Local Port", int(port) + 1))
                    
                    if host_name and host_name != "localhost" and host_name == meta.get("host_name"):
                        UI.error(f"Hostname '{host_name}' is already in use by running container '{meta.get('container_name')}'")
                        if self.non_interactive: sys.exit(1)
                        host_name = UI.ask("Enter a different Hostname", f"alt-{host_name}")

                    if es_port and str(es_port) == meta.get("es_port") and m_ip == r_ip:
                        UI.error(f"Elasticsearch port {es_port} on {r_ip} is already in use by running container '{meta.get('container_name')}'")
                        if self.non_interactive: sys.exit(1)
                        es_port = int(UI.ask("Enter a different Elasticsearch Port", int(es_port) + 1))

        tag = self.args.tag or project_meta.get("tag")
        use_portal = self.args.portal or (project_meta.get("image_type") == "portal")
        image_name = IMAGE_NAME_PORTAL if use_portal else IMAGE_NAME_DXP
        api_base = API_BASE_PORTAL if use_portal else API_BASE_DXP

        if not tag:
            if root_path and re.match(TAG_PATTERN, Path(root_path).name):
                tag = Path(root_path).name
            
            if not tag:
                ans = self.args.release_type or (UI.ask("Release type (any|u|lts|qr) or Enter Tag", "any") if not self.non_interactive else "any")
                
                if re.match(TAG_PATTERN, ans):
                    tag = ans
                else:
                    release_type = ans
                    year = datetime.now().strftime("%Y")
                    tag = discover_latest_tag(api_base, release_type, year, self.verbose, getattr(self.args, 'refresh', False))
                    if not tag:
                        tag = discover_latest_tag(api_base, release_type, None, self.verbose, getattr(self.args, 'refresh', False))
                    
                    if not self.non_interactive:
                        tag = UI.ask("Enter Liferay Docker Tag", tag)
                    elif not tag:
                        UI.die("Could not auto-detect tag. Please provide --tag.")

        root_default = root_path or f"./{tag}"
        root_path = self.args.root or (UI.ask("Liferay Root", root_default) if not self.non_interactive else root_default)
        paths = self.setup_paths(root_path)

        if not container_name:
            container_name = self.args.container or project_meta.get("container_name") or Path(root_path).name.replace(".", "-")
        
        inspect = run_command(["docker", "ps", "-a", "--filter", f"name=^{container_name}$", "--format", "{{.Names}}"], check=False)
        
        db_kind = getattr(self.args, 'db', None) or project_meta.get("db_type")
        use_host_net = getattr(self.args, 'host_network', None)
        if use_host_net is None:
            host_net_meta = project_meta.get("host_network")
            if host_net_meta is not None: use_host_net = (host_net_meta == "True")

        disable_zip64 = getattr(self.args, 'disable_zip64', False)

        resolved_ip = self.get_resolved_ip(host_name) or "127.0.0.1"

        if not inspect or container_name not in inspect.split("\n"):
            UI.heading(f"Initializing {container_name}")
            
            for p in paths.values():
                p.mkdir(parents=True, exist_ok=True)
            
            common_dir = SCRIPT_DIR / "common"
            if common_dir.exists():
                for f in common_dir.glob("*activationkeys.xml"):
                    if not use_portal: shutil.copy(f, paths["deploy"])
                for f in common_dir.glob("*.properties"):
                    shutil.copy(f, paths["files"])

            rm_container = getattr(self.args, 'remove_after', False)
            if not rm_container and not self.non_interactive:
                rm_container = UI.ask("Remove container afterwards?", "N") == "Y"
            rm_arg = ["--rm"] if rm_container else []

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
                    
                    jdbc_updates = {
                        "jdbc.default.driverClassName": "org.postgresql.Driver",
                        "jdbc.default.url": f"jdbc:postgresql://host.docker.internal:5432/{db_name}",
                        "jdbc.default.username": user
                    }
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
                    
                    jdbc_updates = {
                        "jdbc.default.driverClassName": "com.mysql.cj.jdbc.Driver",
                        "jdbc.default.url": f"jdbc:mysql://host.docker.internal:3306/{db_name}",
                        "jdbc.default.username": user
                    }
                    if pw:
                        jdbc_updates["jdbc.default.password"] = pw

                self.update_portal_ext(paths["files"] / "portal-ext.properties", jdbc_updates)

            net_args = []
            if use_host_net is None and not self.non_interactive:
                use_host_net = UI.ask("Use host network?", "N") == "Y"
            elif use_host_net is None:
                use_host_net = False

            if use_host_net:
                net_args = ["--network", "host"]
            else:
                port = port or (int(UI.ask("Local Port", "8080")) if not self.non_interactive else 8080)
                if not self.is_port_available(port, resolved_ip):
                    UI.die(f"Host port {port} is already in use on {resolved_ip}. Please choose a different port.")
                net_args = ["-p", f"{resolved_ip}:{port}:8080"]

            if not host_name and not self.non_interactive:
                host_name = UI.ask("Virtual Hostname (e.g. liferay.local)", "localhost")
            elif not host_name:
                host_name = "localhost"

            if not es_port and not self.non_interactive:
                es_port = UI.ask("Elasticsearch Sidecar Port", "9200")
            elif not es_port:
                es_port = "9200"

            # Always expose ES port to the resolved IP for developer convenience
            if not use_host_net:
                if not self.is_port_available(es_port, resolved_ip):
                    UI.die(f"Host port {es_port} (Elasticsearch) is already in use on {resolved_ip}.")
                net_args += ["-p", f"{resolved_ip}:{es_port}:9200"]

            env_args = []
            if disable_zip64:
                env_args += ["-e", "LIFERAY_JVM_OPTS=-Djdk.util.zip.disableZip64ExtraFieldValidation=true"]
            
            host_updates = {}
            if host_name and host_name != "localhost":
                safe_host = host_name.replace(".", "_").replace("-", "_")
                cookie_name = f"LFR_SESSION_ID_{safe_host}"
                jvm_opts = f"-Dorg.apache.catalina.SESSION_COOKIE_NAME={cookie_name}"
                
                # Append to existing opts if any
                found = False
                for i, arg in enumerate(env_args):
                    if "LIFERAY_JVM_OPTS=" in arg:
                        env_args[i] = arg.replace("LIFERAY_JVM_OPTS=", f"LIFERAY_JVM_OPTS={jvm_opts} ")
                        found = True
                        break
                if not found:
                    env_args += ["-e", f"LIFERAY_JVM_OPTS={jvm_opts}"]
                
                host_updates["session.cookie.domain"] = host_name
                host_updates["session.cookie.use.full.hostname"] = "true"
                # Add virtual host to valid list (LPS-184385)
                host_updates["virtual.hosts.valid.hosts"] = f"localhost,127.0.0.1,[::1],{host_name},{resolved_ip}"

            if es_port and str(es_port) != "9200":
                es_transport = str(int(es_port) + 100)
                host_updates["module.framework.properties.com.liferay.portal.search.elasticsearch7.configuration.ElasticsearchConfiguration.sidecarHttpPort"] = str(es_port)
                host_updates["module.framework.properties.com.liferay.portal.search.elasticsearch7.configuration.ElasticsearchConfiguration.transportTcpPort"] = es_transport

            if host_updates:
                self.update_portal_ext(paths["files"] / "portal-ext.properties", host_updates)

            image_tag = f"{image_name}:{tag}"
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
        else:
            UI.heading(f"Resuming {container_name}")
            
            rm_container = getattr(self.args, 'remove_after', False)
            if not rm_container and not self.non_interactive:
                rm_container = UI.ask("Remove container afterwards?", "N") == "Y"
            
            delete_state = getattr(self.args, 'delete_state', False)
            if not delete_state and not self.non_interactive:
                delete_state = UI.ask("Delete OSGi state folder?", "Y") == "Y"
                
            if delete_state:
                is_running = run_command(["docker", "ps", "-q", "-f", f"name={container_name}"])
                if is_running:
                    UI.info(f"Stopping {container_name} to clear state...")
                    run_command(["docker", "stop", container_name])
                    if not self.wait_for_container_stop(container_name):
                        UI.die(f"Container {container_name} failed to stop within timeout. Aborting state deletion.")

                UI.info("Clearing OSGi state...")
                if self.safe_rmtree(paths["state"], root=paths["root"]):
                    paths["state"].mkdir(parents=True)

        # Capture current JDBC URL for collision checks
        jdbc_params = self.get_jdbc_params(paths["files"])
        jdbc_url = jdbc_params.get("jdbc.default.url")

        project_meta.update({
            "tag": tag,
            "image_type": "portal" if use_portal else "dxp",
            "container_name": container_name,
            "db_type": db_kind or "hypersonic",
            "jdbc_url": jdbc_url,
            "port": str(port) if port else "8080",
            "host_network": str(use_host_net),
            "host_name": host_name or "localhost",
            "es_port": str(es_port) if es_port else "9200",
            "disable_zip64": str(disable_zip64),
            "last_run": datetime.now().isoformat()
        })
        self.write_meta(paths["root"] / PROJECT_META_FILE, project_meta)

        try:
            if getattr(self.args, 'follow', False):
                UI.info(f"Starting {container_name} and following logs...")
                # Start detached first, then attach to logs
                run_command(["docker", "start", container_name], capture_output=False)
                try:
                    run_command(["docker", "logs", "-f", container_name], capture_output=False)
                except KeyboardInterrupt:
                    # Clean stop if follow is interrupted
                    UI.info(f"Stopping {container_name}...")
                    run_command(["docker", "stop", container_name], check=False)
            else:
                UI.info(f"Starting {container_name} in background...")
                run_command(["docker", "start", container_name], capture_output=False)
                
                access_url = f"http://{host_name or 'localhost'}:{port or 8080}"
                UI.success(f"Container {container_name} started.\n"
                           f"  Logs: {UI.CYAN}docker logs -f {container_name}{UI.COLOR_OFF}\n"
                           f"  URL:  {UI.CYAN}{access_url}{UI.COLOR_OFF}")
        except (subprocess.CalledProcessError, KeyboardInterrupt) as e:
            if isinstance(e, subprocess.CalledProcessError):
                if "can't assign requested address" in str(e.stderr or "") or "bind: can't assign requested address" in (e.stdout or ""):
                    UI.error(f"Failed to start container: Port binding error.")
                    self.print_macos_alias_advice(resolved_ip)
                    sys.exit(1)
                if e.returncode == 130:
                    # Cleanly handled by main loop
                    raise KeyboardInterrupt()
                raise e
            else:
                # Top-level handler in main() will catch this
                raise
        
        if rm_container:
            UI.info(f"Deleting {container_name}")
            run_command(["docker", "rm", "--force", container_name], check=False)

    def cmd_snapshots(self, paths=None):
        if not paths:
            root_path = self.detect_root() or os.getcwd()
            paths = self.setup_paths(root_path)
        
        if not paths["backups"].exists():
            UI.info(f"No backups directory found at {paths['backups']}")
            return []

        backups = sorted([d for d in paths["backups"].iterdir() if d.is_dir()], key=lambda x: x.name, reverse=True)
        if not backups:
            UI.info("No snapshots found.")
            return []

        UI.heading(f"Snapshots in {paths['backups']}")
        print(f"{'Index':<6} {'Name':<20} {'Date':<20} {'Format':<15} {'Size':<10}")
        print("-" * 75)
        
        for i, b in enumerate(backups):
            meta = self.read_meta(b / "meta")
            name = meta.get("name", "(unnamed)")[:18]
            date = b.name.replace("-", " ")[:19]
            fmt = meta.get("format", "standard")
            
            total_size = sum(f.stat().st_size for f in b.glob('*') if f.is_file())
            size_str = UI.format_size(total_size)
            
            print(f"[{i+1:<3}] {name:<20} {date:<20} {fmt:<15} {size_str:<10}")
        return backups

    def cmd_snapshot(self):
        root_path = self.detect_root()
        if not root_path:
            roots = self.find_dxp_roots()
            if roots and not self.non_interactive:
                UI.heading("Select Managed Folder for Snapshot")
                for i, root in enumerate(roots):
                    print(f"[{i+1}] {root['path'].name} [{UI.CYAN}{root['version']}{UI.COLOR_OFF}]")
                
                choice = UI.ask("Select folder index", "1")
                try:
                    idx = int(choice) - 1
                    if 0 <= idx < len(roots):
                        root_path = roots[idx]['path']
                except ValueError:
                    pass
            
            if not root_path:
                root_path = UI.ask("Liferay Root path")
                if not root_path: UI.die("Liferay Root is required.")

        paths = self.setup_paths(root_path)
        project_meta = self.read_meta(root_path / PROJECT_META_FILE)
        container_name = self.args.container or project_meta.get("container_name")
        if not container_name:
            container_name = Path(root_path).name.replace(".", "-")
        
        is_running = run_command(["docker", "ps", "-q", "-f", f"name={container_name}"])
        stop_needed = (not getattr(self.args, 'no_stop', False) and is_running)
        
        if stop_needed and not self.non_interactive:
            stop_needed = UI.ask("Stop container during backup?", "Y") == "Y"

        if stop_needed and is_running:
            UI.info(f"Stopping {container_name}...")
            run_command(["docker", "stop", container_name])
            if not self.wait_for_container_stop(container_name):
                UI.die(f"Container {container_name} failed to stop within timeout. Aborting snapshot.")

        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        prefix = self.args.prefix + "-" if self.args.prefix else ""
        snap_dir = paths["backups"] / f"{prefix}{timestamp}"
        
        total, used, free = shutil.disk_usage(paths["root"])
        if free < 1024 * 1024 * 500:
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

        if not getattr(self.args, 'db_only', False):
            UI.heading("Archiving Files")

            def snapshot_filter(tarinfo):
                # Skip non-regular files like sockets, pipes, etc.
                if not (tarinfo.isfile() or tarinfo.isdir() or tarinfo.issym() or tarinfo.islnk()):
                    if self.verbose:
                        UI.info(f"Skipping special file: {tarinfo.name}")
                    return None
                
                # Exclude generated/re-indexable directories
                exclude_patterns = [
                    "osgi/state",
                    "data/elasticsearch7",
                    "data/elasticsearch"
                ]
                for pattern in exclude_patterns:
                    if tarinfo.name == pattern or tarinfo.name.startswith(pattern + "/"):
                        if self.verbose:
                            UI.info(f"Skipping generated path: {tarinfo.name}")
                        return None
                return tarinfo

            if format_choice == "liferay-cloud":
                arc_path = snap_dir / "volume.tgz"
                UI.info("Capturing document_library...")
                with tarfile.open(arc_path, "w:gz") as tar:
                    doclib = paths["data"] / "document_library"
                    if doclib.exists(): 
                        tar.add(doclib, arcname="document_library", filter=snapshot_filter)
            else:
                arc_path = snap_dir / f"files.tar{comp_ext}"
                UI.info(f"Capturing volumes ({comp})...")
                with tarfile.open(arc_path, tar_mode) as tar:
                    for folder in ["files", "scripts", "osgi", "data", "deploy", "modules"]:
                        folder_path = paths["root"] / folder
                        if folder_path.exists(): 
                            tar.add(folder_path, arcname=folder, filter=snapshot_filter)
            meta["files_archive"] = arc_path.name

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
                    p1 = subprocess.Popen(dump_cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    comp_cmd = ["gzip", "-c"] if comp == "gzip" or format_choice == "liferay-cloud" else (["xz", "-c"] if comp == "xz" else ["cat"])
                    p2 = subprocess.Popen(comp_cmd, stdin=p1.stdout, stdout=f)
                    p1.stdout.close() # Allow p1 to receive a SIGPIPE if p2 exits
                    _, stderr = p1.communicate()
                    p2.wait()
                    
                    if p1.returncode != 0 or p2.returncode != 0 or not dump_file.exists() or dump_file.stat().st_size == 0:
                        UI.error(f"Database export failed (PostgreSQL): {stderr.decode().strip() if stderr else 'Stream error'}")
                        if dump_file.exists(): dump_file.unlink()
                        self.safe_rmtree(snap_dir, root=paths["backups"])
                        if stop_needed: run_command(["docker", "start", container_name])
                        sys.exit(1)
                snap_type = "postgresql"
            elif "mysql" in url.lower():
                dbname = url.split("/")[-1].split("?")[0]
                host = self.args.my_host or "localhost"
                port = self.args.my_port or "3306"
                UI.info(f"MySQL: {dbname}")
                pw_arg = f"-p{pw}" if pw else ""
                with open(dump_file, "wb") as f:
                    dump_cmd = ["mysqldump", "-h", host, "-P", port, "-u", user, pw_arg, dbname]
                    p1 = subprocess.Popen(dump_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    comp_cmd = ["gzip", "-c"] if comp == "gzip" or format_choice == "liferay-cloud" else (["xz", "-c"] if comp == "xz" else ["cat"])
                    p2 = subprocess.Popen(comp_cmd, stdin=p1.stdout, stdout=f)
                    p1.stdout.close()
                    _, stderr = p1.communicate()
                    p2.wait()
                    
                    if p1.returncode != 0 or p2.returncode != 0 or not dump_file.exists() or dump_file.stat().st_size == 0:
                        UI.error(f"Database export failed (MySQL): {stderr.decode().strip() if stderr else 'Stream error'}")
                        if dump_file.exists(): dump_file.unlink()
                        self.safe_rmtree(snap_dir, root=paths["backups"])
                        if stop_needed: run_command(["docker", "start", container_name])
                        sys.exit(1)
                snap_type = "mysql"
            
            meta["db_dump"] = dump_file.name
        
        meta["type"] = snap_type
        self.write_meta(snap_dir / "meta", meta)

        if getattr(self.args, 'verify', False):
            UI.heading("Verification")
            success = True
            if "files_archive" in meta: success &= self.verify_archive(snap_dir / meta["files_archive"])
            if "db_dump" in meta: success &= self.verify_archive(snap_dir / meta["db_dump"])
            if not success: UI.die("Verification failed.")

        if stop_needed: run_command(["docker", "start", container_name])
        
        if self.args.retention:
            UI.info(f"Pruning backups (keeping {self.args.retention})...")
            all_bks = sorted([d for d in paths["backups"].iterdir() if d.is_dir() and (not prefix or d.name.startswith(prefix))], key=lambda x: x.name, reverse=True)
            for old_bk in all_bks[self.args.retention:]:
                UI.info(f"Pruning: {old_bk.name}")
                self.safe_rmtree(old_bk, root=paths["backups"])

        UI.success(f"Snapshot saved: {snap_dir}")


    def cmd_restore(self):
        root_path = self.detect_root()
        if not root_path:
            roots = self.find_dxp_roots()
            if roots and not self.non_interactive:
                UI.heading("Select Managed Folder for Restore")
                for i, root in enumerate(roots):
                    print(f"[{i+1}] {root['path'].name} [{UI.CYAN}{root['version']}{UI.COLOR_OFF}]")
                
                choice = UI.ask("Select folder index", "1")
                try:
                    idx = int(choice) - 1
                    if 0 <= idx < len(roots):
                        root_path = roots[idx]['path']
                except ValueError:
                    pass
            
            if not root_path:
                root_path = UI.ask("Liferay Root path")
                if not root_path: UI.die("Liferay Root is required.")

        paths = self.setup_paths(root_path)
        project_meta = self.read_meta(root_path / PROJECT_META_FILE)
        container_name = self.args.container or project_meta.get("container_name")
        if not container_name:
            container_name = Path(root_path).name.replace(".", "-")
        
        if self.args.index:
            backups = sorted([d for d in paths["backups"].iterdir() if d.is_dir()], key=lambda x: x.name, reverse=True)
            if not backups: UI.die("No snapshots available.")
            choice = backups[self.args.index - 1]
        elif getattr(self.args, 'checkpoint', None):
            choice = paths["backups"] / self.args.checkpoint
            if not choice.exists(): UI.die(f"Snapshot {self.args.checkpoint} not found.")
        else:
            backups = self.cmd_snapshots(paths)
            if not backups: UI.die("No snapshots available.")
            choice = backups[int(UI.ask("Select snapshot index", "1")) - 1]

        is_running = run_command(["docker", "ps", "-q", "-f", f"name={container_name}"])
        
        stop_needed = True if is_running else False
        if stop_needed and not self.non_interactive:
            stop_needed = UI.ask("Stop container during restore?", "Y") == "Y"
            
        if stop_needed and is_running:
            UI.info(f"Stopping {container_name}...")
            try:
                subprocess.run(["docker", "stop", container_name], check=True, capture_output=True)
            except subprocess.CalledProcessError as e:
                UI.die(f"Failed to stop container: {e.stderr.decode().strip()}")
            
            UI.info("Waiting for container to release volumes...")
            if not self.wait_for_container_stop(container_name):
                UI.die(f"Container {container_name} failed to stop within timeout. Aborting restore.")
            
            # Host-side safety sleep for file handle release (macOS/Windows)
            time.sleep(2)

        UI.heading(f"Restoring {choice.name}")
        
        delete_state = getattr(self.args, 'delete_state', False)
        if not delete_state and not self.non_interactive:
            delete_state = UI.ask("Delete OSGi state?", "Y") == "Y"
        if delete_state:
            if self.safe_rmtree(paths["state"], root=paths["root"]):
                paths["state"].mkdir(parents=True)
            
        delete_es = getattr(self.args, 'delete_es', False)
        if not delete_es and not self.non_interactive:
            delete_es = UI.ask("Delete Elasticsearch?", "Y") == "Y"
        if delete_es:
            self.safe_rmtree(paths["data"] / "elasticsearch7", root=paths["root"])

        meta = self.read_meta(choice / "meta")
        if not meta:
            UI.info("Missing meta file. Using heuristics...")
            meta["format"] = "liferay-cloud" if (choice / "database.gz").exists() or (choice / "volume.tgz").exists() else "standard"
            sqls = list(choice.glob("*.sql*"))
            if sqls: meta["db_dump"] = sqls[0].name
            tars = list(choice.glob("files.tar*"))
            if tars: meta["files_archive"] = tars[0].name
        
        meta_ver = int(meta.get("meta_version", 1))
        if meta_ver < MIN_META_VERSION and not getattr(self.args, 'allow_legacy', False):
            UI.die(f"Backup version {meta_ver} is unsupported. Use --allow-legacy.")

        arc_name = meta.get("files_archive") or ( "volume.tgz" if meta.get("format") == "liferay-cloud" else None )
        if arc_name:
            arc = choice / arc_name
            if arc.exists():
                UI.info(f"Restoring Files: {arc.name}")
                if meta.get("format") == "liferay-cloud":
                    with tarfile.open(arc, "r:gz") as tar:
                        self.safe_extract(tar, paths["data"])
                else:
                    with tarfile.open(arc, "r:*") as tar:
                        self.safe_extract(tar, paths["root"])

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
                    UI.info(f"MySQL: {dbname}")
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
            self.safe_rmtree(choice, root=paths["backups"])

        if is_running: run_command(["docker", "start", container_name])
        UI.success("Restore complete.")



# --- Main CLI ---
def main():
    parser = argparse.ArgumentParser(description="Liferay DXP Docker Manager (Python)")
    parser.add_argument("--select", action="store_true", help="Browse and select managed DXP folders")
    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # Run command
    run_parser = subparsers.add_parser("run", help="Run Liferay DXP container")
    run_parser.add_argument("--select", action="store_true", help="Browse and select managed DXP folders")
    run_parser.add_argument("-t", "--tag", help="Liferay Docker image tag")
    run_parser.add_argument("-r", "--root", help="Project root path")
    run_parser.add_argument("-c", "--container", help="Container name")
    run_parser.add_argument("-f", "--follow", action="store_true", help="Follow logs after start")
    run_parser.add_argument("--portal", action="store_true", help="Use Liferay Portal instead of DXP")
    run_parser.add_argument("--release-type", choices=["any", "u", "lts", "qr"], help="Tag discovery mode")
    run_parser.add_argument("--db", choices=["postgresql", "mysql", "hypersonic"], help="Database choice")
    run_parser.add_argument("--jdbc-username", help="Username for external DB")
    run_parser.add_argument("--jdbc-password", help="Password for external DB")
    run_parser.add_argument("--recreate-db", action="store_true", help="Recreate DB if it exists")
    run_parser.add_argument("-p", "--port", type=int, help="Local HTTP port")
    run_parser.add_argument("--host-network", action="store_true", help="Use host networking")
    run_parser.add_argument("--host-name", help="Virtual hostname for the instance")
    run_parser.add_argument("--es-port", type=int, help="Elasticsearch sidecar HTTP port")
    run_parser.add_argument("--disable-zip64", action="store_true", help="Disable JVM Zip64 validation")
    run_parser.add_argument("--delete-state", action="store_true", help="Delete OSGi state before start")
    run_parser.add_argument("--remove-after", action="store_true", help="Remove container after stop")
    run_parser.add_argument("--refresh", action="store_true", help="Force refresh of Docker Hub tag cache")
    run_parser.add_argument("--non-interactive", action="store_true")
    run_parser.add_argument("--verbose", action="store_true")

    # Snapshots command
    subparsers.add_parser("snapshots", help="List available snapshots")

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
    rest_parser.add_argument("--my-port", help="MySQL_port override")
    rest_parser.add_argument("--non-interactive", action="store_true")
    rest_parser.add_argument("--verbose", action="store_true")

    args = parser.parse_args()
    
    if getattr(args, 'select', False) and not args.command:
        args.command = "run"

    if not args.command:
        parser.print_help()
        return

    manager = LiferayManager(args)
    if args.command == "run": manager.cmd_run()
    elif args.command == "snapshots": manager.cmd_snapshots()
    elif args.command == "snapshot": manager.cmd_snapshot()
    elif args.command == "restore": manager.cmd_restore()

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n")
        UI.info("Interrupted by user. Exiting...")
        sys.exit(130)
