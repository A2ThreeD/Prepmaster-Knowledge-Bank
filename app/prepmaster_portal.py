#!/usr/bin/env python3

import argparse
import json
import os
import re
import shutil
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib import error, parse, request


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Prepmaster portal API service.")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8081)
    parser.add_argument("--repo-root", required=True)
    parser.add_argument("--data-dir", required=True)
    return parser.parse_args()


def read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key] = value
    return values


def update_env_file(path: Path, updates: dict[str, str]) -> None:
    lines = path.read_text().splitlines() if path.exists() else []
    seen: set[str] = set()
    result: list[str] = []

    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key, _ = stripped.split("=", 1)
            if key in updates:
                result.append(f"{key}={updates[key]}")
                seen.add(key)
                continue
        result.append(line)

    for key, value in updates.items():
        if key not in seen:
            result.append(f"{key}={value}")

    path.write_text("\n".join(result).rstrip() + "\n")


def read_json(path: Path, default: dict) -> dict:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return default


def inspect_pmtiles_file(path: Path) -> dict[str, object]:
    info: dict[str, object] = {
        "exists": path.exists(),
        "size_bytes": path.stat().st_size if path.exists() else 0,
        "valid": False,
        "error": "File not found",
    }
    if not path.exists() or not path.is_file():
        return info

    try:
        header = path.read_bytes()[:64]
    except OSError as exc:
        info["error"] = str(exc)
        return info

    if header.startswith(b"version https://git-lfs.github.com/spec/"):
        info["error"] = "Git LFS pointer file downloaded instead of PMTiles archive"
        return info

    if header.startswith(b"PMTiles") or header[:2] == b"PM":
        info["valid"] = True
        info["error"] = None
        return info

    info["error"] = "Wrong magic number for PMTiles archive"
    return info


class PortalState:
    def __init__(self, repo_root: Path, data_dir: Path) -> None:
        self.repo_root = repo_root
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.prepmaster_env = self.repo_root / "config" / "prepmaster.env"
        self.install_profile_env = self.repo_root / "config" / "install-profile.env"
        self.state_file = self.data_dir / "portal-state.json"
        self.apply_state_file = self.data_dir / "apply-state.json"
        self.apply_log_file = self.data_dir / "apply.log"
        self.map_sync_state_file = self.data_dir / "map-sync-state.json"
        self.map_sync_log_file = self.data_dir / "map-sync.log"
        self.maps_catalog_cache_file = self.data_dir / "maps-catalog-cache.json"
        self.apply_lock = threading.Lock()
        self.apply_thread: threading.Thread | None = None
        self.map_sync_lock = threading.Lock()
        self.map_sync_thread: threading.Thread | None = None
        self.write_maps_runtime_config()
        self.recover_interrupted_apply()

    def load_state(self) -> dict:
        prepmaster = read_env_file(self.prepmaster_env)
        profile = read_env_file(self.install_profile_env)
        state = read_json(
            self.state_file,
            {"setup_complete": False, "last_saved_at": None},
        )
        return {
            "setup_complete": bool(state.get("setup_complete", False)),
            "last_saved_at": state.get("last_saved_at"),
            "profile": {
                "install_kolibri": profile.get("INSTALL_KOLIBRI", "0") == "1",
                "install_ka_lite": profile.get("INSTALL_KA_LITE", "0") == "1",
                "wikipedia_option": prepmaster.get(
                    "PREPMASTER_WIKIPEDIA_OPTION", "top-mini"
                ),
                "ap_enabled": prepmaster.get("PREPMASTER_AP_ENABLED", "0") == "1",
                "zim_mode": prepmaster.get("PREPMASTER_ZIM_MODE", "full"),
            },
        }

    def maps_env(self) -> dict[str, str]:
        return read_env_file(self.prepmaster_env)

    def maps_root(self) -> Path:
        env = self.maps_env()
        return Path(env.get("PREPMASTER_MAP_PMTILES_ROOT", "/srv/prepmaster/maps/pmtiles"))

    def maps_web_root(self) -> Path:
        env = self.maps_env()
        return Path(env.get("PREPMASTER_MAPS_ROOT", "/srv/prepmaster/www/maps"))

    def active_pmtiles_file(self) -> str:
        env = self.maps_env()
        return env.get("PREPMASTER_MAP_PMTILES_FILE", "basemap.pmtiles")

    def list_pmtiles_packages(self, valid_only: bool = False) -> list[str]:
        root = self.maps_root()
        if not root.exists():
            return []
        packages: list[str] = []
        for path in sorted(root.iterdir()):
            if not path.is_file() or path.suffix.lower() != ".pmtiles":
                continue
            if valid_only and not inspect_pmtiles_file(path).get("valid"):
                continue
            packages.append(path.name)
        return packages

    def pmtiles_inventory(self) -> list[dict[str, object]]:
        root = self.maps_root()
        if not root.exists():
            return []
        inventory: list[dict[str, object]] = []
        for path in sorted(root.iterdir()):
            if not path.is_file() or path.suffix.lower() != ".pmtiles":
                continue
            details = inspect_pmtiles_file(path)
            inventory.append(
                {
                    "name": path.name,
                    "size_bytes": details["size_bytes"],
                    "valid": details["valid"],
                    "error": details["error"],
                }
            )
        return inventory

    def write_maps_runtime_config(self) -> None:
        env = self.maps_env()
        maps_web_root = self.maps_web_root()
        maps_web_root.mkdir(parents=True, exist_ok=True)
        status = self.maps_status()
        config = {
            "pmtilesUrl": f"/pmtiles/{env.get('PREPMASTER_MAP_PMTILES_FILE', 'basemap.pmtiles')}",
            "pmtilesFile": env.get("PREPMASTER_MAP_PMTILES_FILE", "basemap.pmtiles"),
            "flavor": env.get("PREPMASTER_MAP_STYLE_FLAVOR", "dark"),
            "language": env.get("PREPMASTER_MAP_LANGUAGE", "en"),
            "defaultLat": float(env.get("PREPMASTER_MAP_DEFAULT_LAT", "39.8283")),
            "defaultLon": float(env.get("PREPMASTER_MAP_DEFAULT_LON", "-98.5795")),
            "defaultZoom": int(env.get("PREPMASTER_MAP_DEFAULT_ZOOM", "4")),
            "minZoom": int(env.get("PREPMASTER_MAP_MIN_ZOOM", "2")),
            "maxZoom": int(env.get("PREPMASTER_MAP_MAX_ZOOM", "14")),
        }
        (maps_web_root / "config.json").write_text(json.dumps(config, indent=2) + "\n")
        (maps_web_root / "state.json").write_text(json.dumps(status, indent=2) + "\n")

    def maps_status(self) -> dict:
        env = self.maps_env()
        root = self.maps_root()
        active_file = env.get("PREPMASTER_MAP_PMTILES_FILE", "basemap.pmtiles")
        active_path = root / active_file
        inventory = self.pmtiles_inventory()
        valid_packages = [item["name"] for item in inventory if item["valid"]]
        invalid_packages = [
            {"name": item["name"], "size_bytes": item["size_bytes"], "error": item["error"]}
            for item in inventory
            if not item["valid"]
        ]
        active_details = inspect_pmtiles_file(active_path)
        return {
            "root": str(root),
            "active_file": active_file,
            "active_url": f"/pmtiles/{active_file}",
            "active_exists": bool(active_details["exists"]),
            "active_valid": bool(active_details["valid"]),
            "active_error": active_details["error"],
            "active_size_bytes": active_details["size_bytes"],
            "available_files": valid_packages,
            "invalid_files": invalid_packages,
            "flavor": env.get("PREPMASTER_MAP_STYLE_FLAVOR", "dark"),
            "language": env.get("PREPMASTER_MAP_LANGUAGE", "en"),
        }

    def maps_catalog_source(self) -> dict[str, str]:
        env = self.maps_env()
        return {
            "owner": env.get("PREPMASTER_MAP_REPO_OWNER", "Crosstalk-Solutions"),
            "repo": env.get("PREPMASTER_MAP_REPO_NAME", "project-nomad-maps"),
            "branch": env.get("PREPMASTER_MAP_REPO_BRANCH", "master"),
            "subdir": env.get("PREPMASTER_MAP_REPO_SUBDIR", "pmtiles"),
        }

    def nomad_repo_cache_root(self) -> Path:
        return self.data_dir / "nomad-maps-repo"

    def ensure_git_lfs_available(self) -> None:
        result = subprocess.run(
            ["git", "lfs", "version"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError("git-lfs is not installed on the Pi")

    def ensure_nomad_repo_checkout(self, log_handle) -> Path:
        self.ensure_git_lfs_available()
        source = self.maps_catalog_source()
        repo_url = f"https://github.com/{source['owner']}/{source['repo']}.git"
        repo_root = self.nomad_repo_cache_root()
        git_env = os.environ.copy()
        git_env["GIT_LFS_SKIP_SMUDGE"] = "1"

        if not repo_root.exists():
            log_handle.write(f"Cloning {repo_url} ({source['branch']})...\n")
            log_handle.flush()
            result = subprocess.run(
                ["git", "clone", "--depth", "1", "--branch", source["branch"], repo_url, str(repo_root)],
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                text=True,
                check=False,
                env=git_env,
            )
            if result.returncode != 0:
                raise RuntimeError("Unable to clone Project NOMAD maps repository")
        else:
            log_handle.write(f"Refreshing {repo_url} ({source['branch']})...\n")
            log_handle.flush()
            commands = [
                ["git", "remote", "set-url", "origin", repo_url],
                ["git", "fetch", "origin", source["branch"], "--depth", "1"],
                ["git", "checkout", "-f", source["branch"]],
                ["git", "reset", "--hard", f"origin/{source['branch']}"],
                ["git", "clean", "-fd"],
            ]
            for command in commands:
                result = subprocess.run(
                    command,
                    cwd=repo_root,
                    stdout=log_handle,
                    stderr=subprocess.STDOUT,
                    text=True,
                    check=False,
                    env=git_env,
                )
                if result.returncode != 0:
                    raise RuntimeError("Unable to refresh Project NOMAD maps repository")

        return repo_root

    def fetch_nomad_maps_catalog(self) -> dict:
        return self.fetch_nomad_maps_catalog_cached(force_refresh=False)

    def fetch_nomad_maps_catalog_cached(self, force_refresh: bool = False) -> dict:
        cache_ttl_seconds = 600
        if not force_refresh and self.maps_catalog_cache_file.exists():
            try:
                cached = json.loads(self.maps_catalog_cache_file.read_text())
            except json.JSONDecodeError:
                cached = None
            if cached:
                fetched_at = float(cached.get("fetched_at", 0))
                if time.time() - fetched_at < cache_ttl_seconds and "payload" in cached:
                    return cached["payload"]

        source = self.maps_catalog_source()
        api_url = (
            f"https://api.github.com/repos/{source['owner']}/{source['repo']}"
            f"/contents/{source['subdir']}?ref={source['branch']}"
        )
        req = request.Request(
            api_url,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "SOPR-Portal",
            },
        )
        try:
            with request.urlopen(req, timeout=30) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except error.URLError as exc:
            raise RuntimeError(f"Unable to fetch NOMAD maps catalog: {exc}") from exc

        installed_info = {item["name"]: item for item in self.pmtiles_inventory()}
        active = self.active_pmtiles_file()
        items = []
        for entry in payload:
            if entry.get("type") != "file":
                continue
            name = entry.get("name", "")
            if not name.endswith(".pmtiles"):
                continue
            items.append(
                {
                    "name": name,
                    "size_bytes": entry.get("size", 0),
                    "lfs_backed": int(entry.get("size", 0) or 0) < 2048,
                    "download_url": entry.get("download_url"),
                    "html_url": entry.get("html_url"),
                    "installed": name in installed_info,
                    "installed_valid": bool(installed_info.get(name, {}).get("valid")),
                    "installed_size_bytes": int(installed_info.get(name, {}).get("size_bytes", 0)),
                    "installed_error": installed_info.get(name, {}).get("error"),
                    "active": name == active,
                }
            )

        payload = {
            "source": source,
            "items": sorted(items, key=lambda item: item["name"]),
        }
        self.maps_catalog_cache_file.write_text(
            json.dumps({"fetched_at": time.time(), "payload": payload}, indent=2) + "\n"
        )
        return payload

    def select_map_package(self, filename: str) -> dict:
        if not filename or Path(filename).name != filename or not filename.endswith(".pmtiles"):
            raise ValueError("Invalid PMTiles filename")

        packages = self.list_pmtiles_packages(valid_only=True)
        if filename not in packages:
            raise ValueError("Valid PMTiles package not found in map root")

        update_env_file(
            self.prepmaster_env,
            {
                "PREPMASTER_MAP_PMTILES_FILE": filename,
            },
        )
        self.write_maps_runtime_config()
        return self.maps_status()

    def update_map_settings(self, payload: dict) -> dict:
        updates: dict[str, str] = {}

        filename = payload.get("filename")
        if filename is not None:
            if not filename or Path(filename).name != filename or not filename.endswith(".pmtiles"):
                raise ValueError("Invalid PMTiles filename")
            packages = self.list_pmtiles_packages(valid_only=True)
            if filename not in packages:
                raise ValueError("Valid PMTiles package not found in map root")
            updates["PREPMASTER_MAP_PMTILES_FILE"] = filename

        flavor = payload.get("flavor")
        if flavor is not None:
            if flavor not in {"light", "dark"}:
                raise ValueError("Invalid map flavor")
            updates["PREPMASTER_MAP_STYLE_FLAVOR"] = flavor

        if updates:
            update_env_file(self.prepmaster_env, updates)
            self.write_maps_runtime_config()

        return self.maps_status()

    def read_map_sync_log_tail(self, lines: int = 30) -> list[str]:
        if not self.map_sync_log_file.exists():
            return []
        return self.map_sync_log_file.read_text().splitlines()[-lines:]

    def load_map_sync_state(self) -> dict:
        state = read_json(
            self.map_sync_state_file,
            {
                "status": "idle",
                "started_at": None,
                "finished_at": None,
                "current_file": None,
                "current_index": None,
                "total_files": None,
                "progress_percent": 0,
                "error": None,
                "selected_files": [],
            },
        )
        state["log_tail"] = self.read_map_sync_log_tail()
        return state

    def save_map_sync_state(self, payload: dict) -> dict:
        current = read_json(self.map_sync_state_file, {})
        current.update(payload)
        self.map_sync_state_file.write_text(json.dumps(current, indent=2) + "\n")
        return self.load_map_sync_state()

    def start_map_sync(self, selected_files: list[str]) -> dict:
        cleaned = []
        for value in selected_files:
            name = Path(value).name
            if name != value or not name.endswith(".pmtiles"):
                raise ValueError("Invalid PMTiles filename in selection")
            cleaned.append(name)

        cleaned = sorted(set(cleaned))

        with self.map_sync_lock:
            current = self.load_map_sync_state()
            if current.get("status") == "running":
                raise RuntimeError("Map sync is already running")

            catalog = self.fetch_nomad_maps_catalog()
            remote_names = {item["name"] for item in catalog["items"]}
            missing = [name for name in cleaned if name not in remote_names]
            if missing:
                raise ValueError(f"Selected PMTiles package is not in catalog: {missing[0]}")

            self.map_sync_log_file.write_text("")
            self.save_map_sync_state(
                {
                    "status": "running",
                    "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "finished_at": None,
                    "current_file": None,
                    "current_index": 0,
                    "total_files": len(cleaned),
                    "progress_percent": 0,
                    "error": None,
                    "selected_files": cleaned,
                }
            )
            self.map_sync_thread = threading.Thread(
                target=self.run_map_sync,
                args=(cleaned,),
                daemon=True,
            )
            self.map_sync_thread.start()
            return self.load_map_sync_state()

    def run_map_sync(self, selected_files: list[str]) -> None:
        exit_error = None
        root = self.maps_root()
        root.mkdir(parents=True, exist_ok=True)

        try:
            catalog = self.fetch_nomad_maps_catalog()
            remote_by_name = {item["name"]: item for item in catalog["items"]}
            managed_remote_names = set(remote_by_name.keys())

            with self.map_sync_log_file.open("a", encoding="utf-8") as log_handle:
                log_handle.write("== Syncing Project NOMAD PMTiles packages ==\n")
                log_handle.flush()
                repo_root = self.ensure_nomad_repo_checkout(log_handle)
                source = self.maps_catalog_source()
                subdir = source["subdir"]

                total_files = len(selected_files)
                for index, name in enumerate(selected_files, start=1):
                    destination = root / name
                    self.save_map_sync_state(
                        {
                            "current_file": name,
                            "current_index": index,
                            "total_files": total_files,
                            "progress_percent": int(((index - 1) / max(total_files, 1)) * 100),
                        }
                    )

                    log_handle.write(f"\n== Syncing map {index}/{total_files}: {name} ==\n")
                    log_handle.flush()

                    existing_details = inspect_pmtiles_file(destination)
                    if existing_details["valid"]:
                        log_handle.write(f"Already current: {name}\n")
                        log_handle.flush()
                    else:
                        include_path = f"{subdir}/{name}"
                        log_handle.write(f"Fetching via git lfs pull: {include_path}\n")
                        log_handle.flush()
                        result = subprocess.run(
                            [
                                "git",
                                "lfs",
                                "pull",
                                "--include",
                                include_path,
                                "--exclude",
                                "",
                            ],
                            cwd=repo_root,
                            stdout=log_handle,
                            stderr=subprocess.STDOUT,
                            text=True,
                            check=False,
                        )
                        log_handle.flush()
                        if result.returncode != 0:
                            raise RuntimeError(f"git lfs pull failed for {name}")

                        source_path = repo_root / subdir / name
                        if not source_path.exists():
                            raise RuntimeError(f"Downloaded map is missing from repo checkout: {name}")

                        shutil.copy2(source_path, destination)

                    downloaded_details = inspect_pmtiles_file(destination)
                    if not downloaded_details["valid"]:
                        try:
                            destination.unlink()
                        except FileNotFoundError:
                            pass
                        raise RuntimeError(
                            f"{name} is not a valid PMTiles archive: {downloaded_details['error']}"
                        )

                    self.save_map_sync_state(
                        {
                            "current_file": name,
                            "current_index": index,
                            "total_files": total_files,
                            "progress_percent": int((index / max(total_files, 1)) * 100),
                        }
                    )

                installed_now = set(self.list_pmtiles_packages())
                for name in sorted(installed_now):
                    if name in managed_remote_names and name not in set(selected_files):
                        target = root / name
                        log_handle.write(f"Removing unchecked map: {name}\n")
                        log_handle.flush()
                        try:
                            target.unlink()
                        except FileNotFoundError:
                            pass

            installed_after = self.list_pmtiles_packages(valid_only=True)
            active = self.active_pmtiles_file()
            if active not in installed_after:
                replacement = installed_after[0] if installed_after else "basemap.pmtiles"
                update_env_file(self.prepmaster_env, {"PREPMASTER_MAP_PMTILES_FILE": replacement})

            self.write_maps_runtime_config()
        except Exception as exc:  # noqa: BLE001
            exit_error = str(exc)

        self.save_map_sync_state(
            {
                "status": "failed" if exit_error else "succeeded",
                "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "error": exit_error,
                "current_file": None,
                "progress_percent": 100 if not exit_error else read_json(self.map_sync_state_file, {}).get("progress_percent", 0),
            }
        )

    def save_setup(self, payload: dict) -> dict:
        wikipedia_option = payload.get("wikipedia_option", "top-mini")
        if wikipedia_option not in {"top-mini", "mini", "maxi"}:
            raise ValueError("Invalid wikipedia_option")
        zim_mode = payload.get("zim_mode", "full")
        if zim_mode not in {"full", "quick-test"}:
            raise ValueError("Invalid zim_mode")

        install_kolibri = bool(payload.get("install_kolibri", False))
        install_ka_lite = bool(payload.get("install_ka_lite", False))
        ap_enabled = bool(payload.get("ap_enabled", False))
        setup_complete = bool(payload.get("setup_complete", True))

        update_env_file(
            self.install_profile_env,
            {
                "INSTALL_BASE_STACK": "1",
                "INSTALL_OPENSTREETMAPS": "1",
                "INSTALL_KIWIX": "1",
                "INSTALL_KOLIBRI": "1" if install_kolibri else "0",
                "INSTALL_KA_LITE": "1" if install_ka_lite else "0",
            },
        )
        update_env_file(
            self.prepmaster_env,
            {
                "PREPMASTER_WIKIPEDIA_OPTION": wikipedia_option,
                "PREPMASTER_AP_ENABLED": "1" if ap_enabled else "0",
                "PREPMASTER_ZIM_MODE": zim_mode,
            },
        )
        self.write_maps_runtime_config()

        state = {
            "setup_complete": setup_complete,
            "last_saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        self.state_file.write_text(json.dumps(state, indent=2) + "\n")
        return self.load_state()

    def status(self) -> dict:
        total, used, free = shutil.disk_usage("/")
        services = self.read_service_statuses(
            {
                "portal": "prepmaster-portal.service",
                "kiwix": "prepmaster-kiwix.service",
                "hostapd": "hostapd.service",
                "dnsmasq": "dnsmasq.service",
                "nginx": "nginx.service",
            }
        )
        return {
            "disk": {
                "total_gb": round(total / (1024 ** 3), 1),
                "used_gb": round(used / (1024 ** 3), 1),
                "free_gb": round(free / (1024 ** 3), 1),
            },
            "temperature_c": self.read_temperature(),
            "cpu": self.read_cpu_load(),
            "uptime": self.read_uptime(),
            "kiwix_url": f"http://{self.detect_primary_host()}:{read_env_file(self.prepmaster_env).get('KIWIX_PORT', '8080')}/",
            "content_mode": read_env_file(self.prepmaster_env).get(
                "PREPMASTER_ZIM_MODE", "full"
            ),
            "services": services,
        }

    def load_apply_state(self) -> dict:
        state = read_json(
            self.apply_state_file,
            {
                "status": "idle",
                "action": "full",
                "total_steps": None,
                "current_step_index": None,
                "started_at": None,
                "finished_at": None,
                "step": None,
                "exit_code": None,
                "error": None,
            },
        )
        state["log_tail"] = self.read_log_tail()
        state.update(self.parse_apply_progress(state))
        return state

    def save_apply_state(self, payload: dict) -> dict:
        current = read_json(self.apply_state_file, {})
        current.update(payload)
        self.apply_state_file.write_text(json.dumps(current, indent=2) + "\n")
        return self.load_apply_state()

    def read_log_tail(self, lines: int = 30) -> list[str]:
        if not self.apply_log_file.exists():
            return []
        content = self.apply_log_file.read_text().splitlines()
        return content[-lines:]

    def launch_apply(
        self,
        action: str,
        *,
        clear_log: bool,
        started_at: str | None = None,
        resumed: bool = False,
    ) -> dict:
        if clear_log:
            self.apply_log_file.write_text("")
        else:
            with self.apply_log_file.open("a", encoding="utf-8") as log_handle:
                log_handle.write(
                    "\n== Resuming interrupted apply workflow after restart ==\n"
                )

        self.save_apply_state(
            {
                "status": "running",
                "action": action,
                "total_steps": len(self.commands_for_action(action)),
                "current_step_index": 0,
                "started_at": started_at
                or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "finished_at": None,
                "step": "Resuming interrupted apply workflow"
                if resumed
                else "Starting apply workflow",
                "exit_code": None,
                "error": None,
                "resumed_from_interruption": resumed,
            }
        )
        self.apply_thread = threading.Thread(
            target=self.run_apply_workflow,
            args=(action,),
            daemon=True,
        )
        self.apply_thread.start()
        return self.load_apply_state()

    def start_apply(self, action: str = "full") -> dict:
        if action not in {"full", "refresh-content", "rebuild-library"}:
            raise ValueError("Invalid apply action")

        with self.apply_lock:
            state = self.load_apply_state()
            if state.get("status") == "running":
                raise RuntimeError("Configuration apply is already running")

            return self.launch_apply(action, clear_log=True)

    def recover_interrupted_apply(self) -> None:
        state = read_json(self.apply_state_file, {})
        if state.get("status") != "running":
            return

        action = state.get("action", "full")
        if action not in {"full", "refresh-content", "rebuild-library"}:
            return

        self.launch_apply(
            action,
            clear_log=False,
            started_at=state.get("started_at"),
            resumed=True,
        )

    def commands_for_action(self, action: str) -> list[tuple[str, list[str]]]:
        if action == "refresh-content":
            return [
                (
                    "Downloading selected Kiwix content",
                    [str(self.repo_root / "scripts" / "download_kiwix_zims.sh")],
                ),
                (
                    "Restarting Kiwix service",
                    ["systemctl", "restart", "prepmaster-kiwix.service"],
                ),
            ]

        if action == "rebuild-library":
            return [
                (
                    "Rebuilding Kiwix library",
                    [str(self.repo_root / "scripts" / "rebuild_kiwix_library.sh")],
                ),
                (
                    "Restarting Kiwix service",
                    ["systemctl", "restart", "prepmaster-kiwix.service"],
                ),
            ]

        return [
            (
                "Downloading selected Kiwix content",
                [str(self.repo_root / "scripts" / "download_kiwix_zims.sh")],
            ),
            (
                "Installing optional components",
                [str(self.repo_root / "scripts" / "install_optional_components.sh")],
            ),
            (
                "Applying wireless AP settings",
                [str(self.repo_root / "scripts" / "configure_access_point.sh")],
            ),
            (
                "Rebuilding Kiwix library",
                [str(self.repo_root / "scripts" / "rebuild_kiwix_library.sh")],
            ),
            (
                "Restarting core services",
                ["systemctl", "restart", "prepmaster-kiwix.service"],
            ),
            (
                "Reloading Nginx",
                ["systemctl", "reload", "nginx"],
            ),
        ]

    def run_apply_workflow(self, action: str) -> None:
        commands = self.commands_for_action(action)

        env = dict(os.environ)
        env.update(read_env_file(self.prepmaster_env))
        env.update(
            {
                "PREPMASTER_ENV_FILE": str(self.prepmaster_env),
                "PREPMASTER_PROFILE_FILE": str(self.install_profile_env),
                "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            }
        )

        exit_code = 0
        error_message = None

        with self.apply_log_file.open("a", encoding="utf-8") as log_handle:
            for index, (step, command) in enumerate(commands, start=1):
                self.save_apply_state(
                    {
                        "step": step,
                        "current_step_index": index,
                        "total_steps": len(commands),
                    }
                )
                log_handle.write(f"\n== {step} ==\n")
                log_handle.flush()
                result = subprocess.run(
                    command,
                    cwd=self.repo_root,
                    env=env,
                    stdout=log_handle,
                    stderr=subprocess.STDOUT,
                    text=True,
                    check=False,
                )
                log_handle.flush()
                if result.returncode != 0:
                    exit_code = result.returncode
                    error_message = f"{step} failed with exit code {result.returncode}"
                    break

        final_status = "succeeded" if exit_code == 0 else "failed"
        self.save_apply_state(
            {
                "status": final_status,
                "action": action,
                "current_step_index": len(commands) if exit_code == 0 else index,
                "total_steps": len(commands),
                "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "exit_code": exit_code,
                "error": error_message,
                "step": None if exit_code == 0 else step,
            }
        )

    def parse_apply_progress(self, state: dict) -> dict:
        progress = {
            "progress_percent": 0,
            "current_file": None,
            "download_current": None,
            "download_total": None,
        }

        total_steps = state.get("total_steps") or 0
        current_step_index = state.get("current_step_index") or 0
        status = state.get("status")
        step = state.get("step") or ""

        if status == "succeeded":
            progress["progress_percent"] = 100
        elif total_steps > 0 and current_step_index > 0:
            progress["progress_percent"] = int(
                ((current_step_index - 1) / total_steps) * 100
            )

        if not self.apply_log_file.exists():
            return progress

        total_pattern = re.compile(r"^PROGRESS_DOWNLOAD_TOTAL\|(\d+)$")
        file_pattern = re.compile(r"^PROGRESS_DOWNLOAD_FILE\|(\d+)\|(\d+)\|(.+)$")
        done_pattern = re.compile(r"^PROGRESS_DOWNLOAD_DONE\|(\d+)\|(\d+)\|(.+)$")
        complete_pattern = re.compile(r"^PROGRESS_DOWNLOAD_COMPLETE\|(\d+)$")

        download_total = None
        current_file = None
        current_index = None
        last_done_index = None

        for line in self.apply_log_file.read_text().splitlines():
            total_match = total_pattern.match(line)
            if total_match:
                download_total = int(total_match.group(1))
                continue

            file_match = file_pattern.match(line)
            if file_match:
                current_index = int(file_match.group(1))
                download_total = int(file_match.group(2))
                current_file = file_match.group(3)
                continue

            done_match = done_pattern.match(line)
            if done_match:
                last_done_index = int(done_match.group(1))
                download_total = int(done_match.group(2))
                current_file = done_match.group(3)
                continue

            complete_match = complete_pattern.match(line)
            if complete_match:
                download_total = int(complete_match.group(1))
                last_done_index = download_total
                current_index = download_total
                current_file = None

        progress["current_file"] = current_file
        progress["download_total"] = download_total
        progress["download_current"] = current_index or last_done_index

        if (
            status == "running"
            and step == "Downloading selected Kiwix content"
            and total_steps > 0
            and download_total
        ):
            completed_within_download = last_done_index or 0
            if current_index and current_file and completed_within_download < current_index:
                completed_within_download = max(current_index - 1, 0)
            progress["progress_percent"] = int(
                (
                    ((current_step_index - 1) + (completed_within_download / download_total))
                    / total_steps
                )
                * 100
            )

        return progress

    def read_temperature(self) -> float | None:
        thermal = Path("/sys/class/thermal/thermal_zone0/temp")
        if not thermal.exists():
            return None
        try:
            return round(int(thermal.read_text().strip()) / 1000, 1)
        except ValueError:
            return None

    def read_uptime(self) -> str | None:
        uptime_path = Path("/proc/uptime")
        if not uptime_path.exists():
            return None
        try:
            seconds = int(float(uptime_path.read_text().split()[0]))
        except ValueError:
            return None
        hours, remainder = divmod(seconds, 3600)
        minutes = remainder // 60
        return f"{hours}h {minutes}m"

    def read_cpu_load(self) -> dict[str, float | int] | None:
        try:
            load1, _, _ = os.getloadavg()
        except (AttributeError, OSError):
            return None

        cpu_count = os.cpu_count() or 1
        load_percent = max(0, min(100, round((load1 / cpu_count) * 100)))
        return {
            "load_1m": round(load1, 2),
            "cpu_count": cpu_count,
            "load_percent": load_percent,
        }

    def read_service_statuses(self, services: dict[str, str]) -> dict[str, str]:
        try:
            result = subprocess.run(
                ["systemctl", "is-active", *services.values()],
                capture_output=True,
                text=True,
                check=False,
            )
        except FileNotFoundError:
            return {name: "unknown" for name in services}

        lines = [line.strip() or "unknown" for line in result.stdout.splitlines()]
        values = list(services.keys())
        resolved: dict[str, str] = {}
        for index, name in enumerate(values):
            resolved[name] = lines[index] if index < len(lines) else "unknown"
        return resolved

    def detect_primary_host(self) -> str:
        try:
            result = subprocess.run(
                ["hostname", "-I"],
                capture_output=True,
                text=True,
                check=False,
            )
        except FileNotFoundError:
            return "127.0.0.1"

        candidates = [value for value in result.stdout.split() if "." in value]
        return candidates[0] if candidates else "127.0.0.1"

    def request_power_action(self, action: str) -> dict:
        if action not in {"restart", "shutdown"}:
            raise ValueError("Invalid power action")

        try:
            result = subprocess.run(
                ["systemctl", action],
                capture_output=True,
                text=True,
                check=False,
            )
        except FileNotFoundError as exc:
            raise RuntimeError("systemctl is not available on this system") from exc

        if result.returncode != 0:
            message = (result.stderr or result.stdout or "").strip() or f"systemctl {action} failed"
            raise RuntimeError(message)

        return {
            "status": "accepted",
            "action": action,
            "message": f"System {action} requested.",
        }


class PortalHandler(BaseHTTPRequestHandler):
    portal_state: PortalState

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_common_headers()
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self) -> None:
        parsed = parse.urlparse(self.path)
        path = parsed.path
        query = parse.parse_qs(parsed.query)

        if path == "/api/state":
            self.send_json(self.portal_state.load_state())
            return
        if path == "/api/status":
            self.send_json(self.portal_state.status())
            return
        if path == "/api/maps":
            self.send_json(self.portal_state.maps_status())
            return
        if path == "/api/maps/catalog":
            try:
                force_refresh = query.get("refresh", ["0"])[0] == "1"
                self.send_json(self.portal_state.fetch_nomad_maps_catalog_cached(force_refresh=force_refresh))
            except RuntimeError as exc:
                self.send_json({"error": str(exc)}, status=502)
            return
        if path == "/api/maps/sync":
            self.send_json(self.portal_state.load_map_sync_state())
            return
        if path == "/api/apply":
            self.send_json(self.portal_state.load_apply_state())
            return
        self.send_json({"error": "Not found"}, status=404)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            self.send_json({"error": "Invalid JSON"}, status=400)
            return

        if self.path == "/api/setup":
            try:
                state = self.portal_state.save_setup(payload)
            except ValueError as exc:
                self.send_json({"error": str(exc)}, status=400)
                return
            self.send_json(state)
            return

        if self.path == "/api/apply":
            try:
                state = self.portal_state.start_apply(payload.get("action", "full"))
            except ValueError as exc:
                self.send_json({"error": str(exc)}, status=400)
                return
            except RuntimeError as exc:
                self.send_json({"error": str(exc)}, status=409)
                return
            self.send_json(state, status=202)
            return

        if self.path == "/api/maps/select":
            try:
                state = self.portal_state.select_map_package(payload.get("filename", ""))
            except ValueError as exc:
                self.send_json({"error": str(exc)}, status=400)
                return
            self.send_json(state)
            return

        if self.path == "/api/maps/settings":
            try:
                state = self.portal_state.update_map_settings(payload)
            except ValueError as exc:
                self.send_json({"error": str(exc)}, status=400)
                return
            self.send_json(state)
            return

        if self.path == "/api/maps/sync":
            try:
                state = self.portal_state.start_map_sync(payload.get("selected_files", []))
            except ValueError as exc:
                self.send_json({"error": str(exc)}, status=400)
                return
            except RuntimeError as exc:
                self.send_json({"error": str(exc)}, status=409)
                return
            self.send_json(state, status=202)
            return

        if self.path == "/api/system/power":
            try:
                state = self.portal_state.request_power_action(payload.get("action", ""))
            except ValueError as exc:
                self.send_json({"error": str(exc)}, status=400)
                return
            except RuntimeError as exc:
                self.send_json({"error": str(exc)}, status=500)
                return
            self.send_json(state, status=202)
            return

        self.send_json({"error": "Not found"}, status=404)

    def send_json(self, payload: dict, status: int = 200) -> None:
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_common_headers()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def send_common_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")

    def log_message(self, fmt: str, *args) -> None:
        return


def main() -> None:
    args = parse_args()
    handler = PortalHandler
    handler.portal_state = PortalState(Path(args.repo_root), Path(args.data_dir))
    server = ThreadingHTTPServer((args.host, args.port), handler)
    print(f"Prepmaster portal API listening on {args.host}:{args.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
