#!/usr/bin/env python3

import argparse
import html
import json
import os
import re
import shutil
import subprocess
import threading
import time
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib import error, parse, request


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="SOPR portal API service.")
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
        with path.open("rb") as handle:
            header = handle.read(64)
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


def format_size_bytes(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 ** 2:
        return f"{size_bytes / 1024:.1f} KB"
    if size_bytes < 1024 ** 3:
        return f"{size_bytes / (1024 ** 2):.1f} MB"
    return f"{size_bytes / (1024 ** 3):.1f} GB"


def preset_to_profile(preset: str) -> str | None:
    if preset == "compact":
        return "essential"
    if preset == "full":
        return "comprehensive"
    if preset == "empty":
        return None
    raise ValueError(f"Unknown content preset: {preset}")


def profile_to_preset(profile: str | None) -> str:
    if profile == "comprehensive":
        return "full"
    if profile in {"essential", "standard", None, ""}:
        return "compact" if profile else "empty"
    return "compact"


class DirectoryIndexParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[dict[str, object]] = []
        self._current_href: str | None = None
        self._current_text: list[str] = []
        self._capturing_anchor = False
        self._last_anchor: dict[str, object] | None = None
        self._tail_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        attributes = dict(attrs)
        href = attributes.get("href")
        if href:
            self._capturing_anchor = True
            self._current_href = href
            self._current_text = []

    def handle_endtag(self, tag: str) -> None:
        if tag != "a" or not self._capturing_anchor or not self._current_href:
            return
        name = "".join(self._current_text).strip()
        entry = {
            "href": self._current_href,
            "name": name or self._current_href,
            "size_bytes": None,
            "size_label": None,
        }
        self.links.append(entry)
        self._last_anchor = entry
        self._tail_text = []
        self._capturing_anchor = False
        self._current_href = None
        self._current_text = []

    def handle_data(self, data: str) -> None:
        if self._capturing_anchor:
            self._current_text.append(data)
            return
        if not self._last_anchor:
            return
        self._tail_text.append(data)
        size_bytes, size_label = parse_apache_index_tail("".join(self._tail_text))
        if size_label:
            self._last_anchor["size_bytes"] = size_bytes
            self._last_anchor["size_label"] = size_label
            self._last_anchor = None
            self._tail_text = []


def parse_apache_index_tail(text: str) -> tuple[int | None, str | None]:
    cleaned = html.unescape(text).replace("\xa0", " ")
    match = re.search(r"\b(\d+(?:\.\d+)?[KMGTP]?|-)($|\s)", cleaned.strip())
    if not match:
        return None, None
    label = match.group(1)
    if label == "-":
        return None, "-"
    return parse_size_label_to_bytes(label), label


def parse_size_label_to_bytes(label: str) -> int | None:
    match = re.fullmatch(r"(\d+(?:\.\d+)?)([KMGTP]?)", label.strip())
    if not match:
        return None
    value = float(match.group(1))
    suffix = match.group(2)
    multiplier = {
        "": 1,
        "K": 1024,
        "M": 1024 ** 2,
        "G": 1024 ** 3,
        "T": 1024 ** 4,
        "P": 1024 ** 5,
    }[suffix]
    return int(value * multiplier)


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
        self.content_catalog_cache_file = self.data_dir / "content-catalog-cache.json"
        self.apply_lock = threading.Lock()
        self.apply_thread: threading.Thread | None = None
        self.map_sync_lock = threading.Lock()
        self.map_sync_thread: threading.Thread | None = None
        self.write_maps_runtime_config()
        self.recover_interrupted_apply()

    def wikipedia_catalog(self) -> dict:
        catalog = read_json(
            self.repo_root / "catalog" / "wikipedia.json",
            {"spec_version": None, "options": []},
        )
        options = catalog.get("options")
        if not isinstance(options, list):
            options = []
        normalized: list[dict[str, object]] = []
        for option in options:
            if not isinstance(option, dict):
                continue
            option_id = str(option.get("id", "")).strip()
            if not option_id:
                continue
            normalized.append(
                {
                    "id": option_id,
                    "name": str(option.get("name", option_id)),
                    "description": str(option.get("description", "")),
                    "size_mb": int(option.get("size_mb", 0) or 0),
                    "version": str(option.get("version", "")),
                }
            )
        return {
            "spec_version": catalog.get("spec_version"),
            "options": normalized,
        }

    def kiwix_catalog(self) -> dict:
        return read_json(
            self.repo_root / "catalog" / "kiwix-categories.json",
            {"categories": []},
        )

    def profile_library_size_mb(self, profile: str) -> int:
        level_order = {
            "essential": 1,
            "standard": 2,
            "comprehensive": 3,
        }
        selected_level = level_order.get(profile, 1)
        document = self.kiwix_catalog()
        categories = document.get("collections") if "collections" in document else document.get("categories", [])
        loadout_key = "loadouts" if "collections" in document else "tiers"
        resource_key = "library_items" if "collections" in document else "resources"
        seen_ids: set[str] = set()
        total_mb = 0

        for category in categories:
            for tier in category.get(loadout_key, []):
                tier_slug = str(tier.get("key") if loadout_key == "loadouts" else tier.get("slug", ""))
                level = 0
                for label, value in level_order.items():
                    if tier_slug.endswith(f"-{label}"):
                        level = value
                        break
                if level == 0 or level > selected_level:
                    continue

                for resource in tier.get(resource_key, []):
                    resource_id = str(resource.get("key") if resource_key == "library_items" else resource.get("id", "")).strip()
                    if not resource_id or resource_id in seen_ids:
                        continue
                    seen_ids.add(resource_id)
                    size_value = resource.get("footprint_mb") if resource_key == "library_items" else resource.get("size_mb")
                    try:
                        total_mb += int(size_value or 0)
                    except (TypeError, ValueError):
                        continue

        return total_mb

    def setup_storage_summary(self) -> dict:
        total, used, free = shutil.disk_usage("/")
        env = read_env_file(self.prepmaster_env)
        profile = env.get("PREPMASTER_ZIM_PROFILE", "essential").strip().lower() or "essential"
        if profile not in {"essential", "standard", "comprehensive"}:
            profile = "essential"
        kolibri_installed = Path("/usr/bin/kolibri").exists()
        return {
            "disk": {
                "total_bytes": total,
                "used_bytes": used,
                "free_bytes": free,
                "total_gb": round(total / (1024 ** 3), 1),
                "used_gb": round(used / (1024 ** 3), 1),
                "free_gb": round(free / (1024 ** 3), 1),
            },
            "base_library_mb": self.profile_library_size_mb(profile),
            "zim_profile": profile,
            "kolibri_estimated_mb": 1500,
            "kolibri_installed": kolibri_installed,
            "warning_free_percent": 10,
        }

    def load_state(self) -> dict:
        prepmaster = read_env_file(self.prepmaster_env)
        profile = read_env_file(self.install_profile_env)
        custom_selection = self.read_custom_zim_selection()
        wikipedia_catalog = self.wikipedia_catalog()
        wikipedia_ids = [
            str(option.get("id", "")).strip()
            for option in wikipedia_catalog["options"]
            if str(option.get("id", "")).strip()
        ]
        wikipedia_option = prepmaster.get("PREPMASTER_WIKIPEDIA_OPTION", "top-mini")
        if wikipedia_ids and wikipedia_option not in wikipedia_ids:
            wikipedia_option = wikipedia_ids[0]
        state = read_json(
            self.state_file,
            {"setup_complete": False, "last_saved_at": None},
        )
        return {
            "setup_complete": bool(state.get("setup_complete", False)),
            "last_saved_at": state.get("last_saved_at"),
            "setup_options": {
                "wikipedia": wikipedia_catalog,
            },
            "profile": {
                "install_kolibri": profile.get("INSTALL_KOLIBRI", "0") == "1",
                "install_ka_lite": profile.get("INSTALL_KA_LITE", "0") == "1",
                "wikipedia_option": wikipedia_option,
                "ap_enabled": prepmaster.get("PREPMASTER_AP_ENABLED", "0") == "1",
                "zim_mode": prepmaster.get("PREPMASTER_ZIM_MODE", "full"),
                "zim_profile": prepmaster.get("PREPMASTER_ZIM_PROFILE", "essential"),
                "custom_zim_count": len(custom_selection.get("selected_items", [])),
            },
            "storage": self.setup_storage_summary(),
        }

    def maps_env(self) -> dict[str, str]:
        return read_env_file(self.prepmaster_env)

    def kiwix_library_dir(self) -> Path:
        env = self.maps_env()
        return Path(env.get("KIWIX_LIBRARY_DIR", "/library/zims/content"))

    def custom_zim_manifest_path(self) -> Path:
        env = self.maps_env()
        configured = env.get("PREPMASTER_ZIM_CUSTOM_URL_FILE")
        if configured:
            return Path(configured)
        return self.repo_root / "config" / "kiwix-zim-urls.custom.txt"

    def custom_zim_selection_path(self) -> Path:
        env = self.maps_env()
        configured = env.get("PREPMASTER_ZIM_CUSTOM_SELECTION_FILE")
        if configured:
            return Path(configured)
        return self.repo_root / "config" / "kiwix-zim-selection.json"

    def custom_base_profile(self) -> str | None:
        env = self.maps_env()
        value = env.get("PREPMASTER_ZIM_CUSTOM_BASE_PROFILE", "essential").strip().lower()
        if value in {"", "none", "empty"}:
            return None
        if value in {"essential", "standard", "comprehensive"}:
            return value
        return "essential"

    def maps_root(self) -> Path:
        env = self.maps_env()
        return Path(env.get("PREPMASTER_MAP_PMTILES_ROOT", "/maps"))

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
        active_file = env.get("PREPMASTER_MAP_PMTILES_FILE", "basemap.pmtiles")
        active_path = self.maps_root() / active_file
        cache_buster = ""
        if active_path.exists() and active_path.is_file():
            stat = active_path.stat()
            cache_buster = f"?v={stat.st_mtime_ns}-{stat.st_size}"
        config = {
            "pmtilesUrl": f"/pmtiles/{active_file}{cache_buster}",
            "pmtilesFile": active_file,
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

    def read_custom_zim_selection(self) -> dict:
        return read_json(
            self.custom_zim_selection_path(),
            {
                "catalog_root": "https://download.kiwix.org/zim/",
                "selected_items": [],
                "saved_at": None,
            },
        )

    def write_custom_zim_selection(self, items: list[dict[str, object]], catalog_root: str) -> dict:
        payload = {
            "catalog_root": catalog_root,
            "selected_items": items,
            "saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        path = self.custom_zim_selection_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2) + "\n")
        return payload

    def read_manifest_urls(self, path: Path) -> list[str]:
        if not path.exists():
            return []
        return [
            line.strip()
            for line in path.read_text().splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]

    def curated_manifest_urls(self, profile: str | None) -> list[str]:
        if not profile:
            return []

        output_path = self.data_dir / f"curated-{profile}-manifest.txt"
        command = [
            "python3",
            str(self.repo_root / "scripts" / "build_kiwix_zim_manifest.py"),
            "--source",
            str(self.repo_root / "catalog" / "kiwix-categories.json"),
            "--output",
            str(output_path),
            "--profile",
            profile,
            "--wikipedia-options",
            str(self.repo_root / "catalog" / "wikipedia.json"),
            "--wikipedia-choice",
            self.maps_env().get("PREPMASTER_WIKIPEDIA_OPTION", "top-mini"),
        ]
        result = subprocess.run(
            command,
            cwd=self.repo_root,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError("Unable to build curated Kiwix manifest")
        return self.read_manifest_urls(output_path)

    def write_custom_zim_manifest(
        self,
        items: list[dict[str, object]],
        catalog_root: str,
        base_profile: str | None,
    ) -> Path:
        manifest = self.custom_zim_manifest_path()
        manifest.parent.mkdir(parents=True, exist_ok=True)
        base_urls = self.curated_manifest_urls(base_profile)
        seen_urls: set[str] = set()
        merged_urls: list[tuple[str, str, str]] = []

        for url in base_urls:
            if url in seen_urls:
                continue
            seen_urls.add(url)
            merged_urls.append(("Curated Base", profile_to_preset(base_profile), url))

        for item in items:
            url = str(item["download_url"])
            if url in seen_urls:
                continue
            seen_urls.add(url)
            merged_urls.append((str(item["category"]), str(item["name"]), url))

        lines = [
            f"# Custom SOPR ZIM selection from {catalog_root}",
            f"# Saved at: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}",
            f"# Base preset: {profile_to_preset(base_profile)}",
            f"# Resource count: {len(merged_urls)}",
            "",
        ]
        for category, title, url in merged_urls:
            lines.append(f"# {category} | {title}")
            lines.append(url)
            lines.append("")
        manifest.write_text("\n".join(lines).rstrip() + "\n")
        return manifest

    def list_installed_zims(self) -> list[dict[str, object]]:
        root = self.kiwix_library_dir()
        if not root.exists():
            return []
        inventory: list[dict[str, object]] = []
        for path in sorted(root.iterdir()):
            if not path.is_file() or path.suffix.lower() != ".zim":
                continue
            stat = path.stat()
            inventory.append(
                {
                    "name": path.name,
                    "size_bytes": stat.st_size,
                    "size_label": format_size_bytes(stat.st_size),
                    "modified_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(stat.st_mtime)),
                }
            )
        return inventory

    def fetch_directory_listing(self, url: str) -> list[dict[str, object]]:
        req = request.Request(
            url,
            headers={
                "User-Agent": "SOPR-Portal",
            },
        )
        with request.urlopen(req, timeout=30) as response:
            html = response.read().decode("utf-8", errors="replace")
        parser = DirectoryIndexParser()
        parser.feed(html)
        return parser.links

    def zim_catalog_root(self) -> str:
        env = self.maps_env()
        return env.get("PREPMASTER_ZIM_CATALOG_URL", "https://download.kiwix.org/zim/")

    def fetch_kiwix_catalog_cached(self, force_refresh: bool = False) -> dict:
        cache_ttl_seconds = 3600
        if not force_refresh and self.content_catalog_cache_file.exists():
            try:
                cached = json.loads(self.content_catalog_cache_file.read_text())
            except json.JSONDecodeError:
                cached = None
            if cached:
                fetched_at = float(cached.get("fetched_at", 0))
                if time.time() - fetched_at < cache_ttl_seconds and "payload" in cached:
                    return cached["payload"]

        root_url = self.zim_catalog_root().rstrip("/") + "/"
        installed = {item["name"]: item for item in self.list_installed_zims()}
        custom_selection = self.read_custom_zim_selection()
        selected_paths = {
            item.get("path")
            for item in custom_selection.get("selected_items", [])
            if item.get("path")
        }

        try:
            root_links = self.fetch_directory_listing(root_url)
            directories = sorted(
                {
                    str(entry["href"]).rstrip("/")
                    for entry in root_links
                    if entry.get("href")
                    and entry["href"] not in {"../", "/"}
                    and str(entry["href"]).endswith("/")
                    and not str(entry["href"]).startswith("?")
                }
            )

            items: list[dict[str, object]] = []
            for directory in directories:
                directory_url = parse.urljoin(root_url, directory + "/")
                file_links = self.fetch_directory_listing(directory_url)
                for entry in sorted(file_links, key=lambda item: str(item.get("href", ""))):
                    href = str(entry.get("href", ""))
                    if not href or href in {"../", "/"} or not href.endswith(".zim"):
                        continue
                    path = f"{directory}/{Path(href).name}"
                    name = Path(href).name
                    installed_item = installed.get(name)
                    items.append(
                        {
                            "path": path,
                            "name": name,
                            "category": directory,
                            "download_url": parse.urljoin(directory_url, href),
                            "size_bytes": int(entry["size_bytes"]) if entry.get("size_bytes") else 0,
                            "size_label": str(entry["size_label"]) if entry.get("size_label") else "",
                            "installed": name in installed,
                            "installed_size_bytes": int(installed_item["size_bytes"]) if installed_item else 0,
                            "installed_size_label": installed_item["size_label"] if installed_item else "",
                            "selected": path in selected_paths,
                        }
                    )
        except error.URLError as exc:
            if self.content_catalog_cache_file.exists():
                cached = read_json(self.content_catalog_cache_file, {})
                if cached.get("payload"):
                    payload = cached["payload"]
                    payload["stale"] = True
                    payload["error"] = f"Using cached Kiwix catalog after refresh failed: {exc}"
                    return payload
            raise RuntimeError(f"Unable to fetch Kiwix catalog: {exc}") from exc

        payload = {
            "source": {
                "root_url": root_url,
            },
            "items": items,
            "stale": False,
            "error": None,
        }
        self.content_catalog_cache_file.write_text(
            json.dumps({"fetched_at": time.time(), "payload": payload}, indent=2) + "\n"
        )
        return payload

    def content_status(self) -> dict:
        env = self.maps_env()
        custom_selection = self.read_custom_zim_selection()
        installed = self.list_installed_zims()
        installed_size_bytes = sum(int(item["size_bytes"]) for item in installed)
        selected_paths = {
            item.get("path")
            for item in custom_selection.get("selected_items", [])
            if item.get("path")
        }
        selected_names = {
            Path(str(item.get("path", ""))).name
            for item in custom_selection.get("selected_items", [])
            if item.get("path")
        }
        inventory = []
        for item in installed:
            inventory.append(
                {
                    **item,
                    "selected": item["name"] in selected_names,
                }
            )
        mode = env.get("PREPMASTER_ZIM_MODE", "full")
        profile = env.get("PREPMASTER_ZIM_PROFILE", "essential")
        if mode == "custom":
            library_preset = "custom"
        elif mode == "quick-test":
            library_preset = "quick-test"
        else:
            library_preset = profile_to_preset(profile)
        return {
            "mode": mode,
            "profile": profile,
            "library_preset": library_preset,
            "custom_base_preset": profile_to_preset(self.custom_base_profile()),
            "wikipedia_option": env.get("PREPMASTER_WIKIPEDIA_OPTION", "top-mini"),
            "library_root": str(self.kiwix_library_dir()),
            "catalog_root": self.zim_catalog_root(),
            "custom_manifest": str(self.custom_zim_manifest_path()),
            "custom_selection_file": str(self.custom_zim_selection_path()),
            "custom_selected_paths": sorted(selected_paths),
            "custom_selected_count": len(selected_paths),
            "installed_count": len(inventory),
            "installed_size_bytes": installed_size_bytes,
            "installed_size_label": format_size_bytes(installed_size_bytes),
            "installed_items": inventory,
        }

    def save_content_settings(self, payload: dict) -> dict:
        env = self.maps_env()
        library_preset = payload.get("library_preset")
        if library_preset is None:
            current_mode = env.get("PREPMASTER_ZIM_MODE", "full")
            current_profile = env.get("PREPMASTER_ZIM_PROFILE", "essential")
            library_preset = "custom" if current_mode == "custom" else profile_to_preset(current_profile)
        if library_preset not in {"compact", "full", "custom", "quick-test"}:
            raise ValueError("Invalid library_preset")

        custom_base_preset = payload.get("custom_base_preset")
        if custom_base_preset is None:
            custom_base_preset = profile_to_preset(self.custom_base_profile())
        if custom_base_preset not in {"compact", "full", "empty"}:
            raise ValueError("Invalid custom_base_preset")

        updates: dict[str, str] = {}
        if library_preset == "quick-test":
            updates["PREPMASTER_ZIM_MODE"] = "quick-test"
        elif library_preset == "custom":
            updates["PREPMASTER_ZIM_MODE"] = "custom"
            updates["PREPMASTER_ZIM_CUSTOM_BASE_PROFILE"] = preset_to_profile(custom_base_preset) or "none"
        else:
            updates["PREPMASTER_ZIM_MODE"] = "full"
            mapped_profile = preset_to_profile(library_preset)
            if mapped_profile:
                updates["PREPMASTER_ZIM_PROFILE"] = mapped_profile

        selected_paths = payload.get("selected_paths")
        catalog = None
        if selected_paths is not None:
            if not isinstance(selected_paths, list):
                raise ValueError("selected_paths must be a list")
            cleaned_paths = []
            for value in selected_paths:
                if not isinstance(value, str) or "/" not in value or not value.endswith(".zim"):
                    raise ValueError("Invalid custom ZIM selection")
                cleaned_paths.append(value)
            cleaned_paths = sorted(set(cleaned_paths))
            if library_preset == "custom":
                base_profile = preset_to_profile(custom_base_preset)
                catalog = self.fetch_kiwix_catalog_cached(force_refresh=False)
                catalog_by_path = {item["path"]: item for item in catalog["items"]}
                missing = [path for path in cleaned_paths if path not in catalog_by_path]
                if missing:
                    raise ValueError(f"Selected ZIM is not in catalog: {missing[0]}")
                selected_items = [catalog_by_path[path] for path in cleaned_paths]
                self.write_custom_zim_selection(selected_items, catalog["source"]["root_url"])
                self.write_custom_zim_manifest(selected_items, catalog["source"]["root_url"], base_profile)
            elif cleaned_paths:
                # Preserve the saved custom selection even when mode is switched away from custom.
                if self.custom_zim_selection_path().exists():
                    existing = self.read_custom_zim_selection()
                    existing_by_path = {
                        item.get("path"): item
                        for item in existing.get("selected_items", [])
                        if item.get("path")
                    }
                    selected_items = [
                        existing_by_path[path]
                        for path in cleaned_paths
                        if path in existing_by_path
                    ]
                    self.write_custom_zim_selection(selected_items, existing.get("catalog_root", self.zim_catalog_root()))
                    self.write_custom_zim_manifest(
                        selected_items,
                        existing.get("catalog_root", self.zim_catalog_root()),
                        self.custom_base_profile(),
                    )

        update_env_file(self.prepmaster_env, updates)
        return self.content_status()

    def remove_zim_files(self, filenames: list[str]) -> dict:
        if not isinstance(filenames, list) or not filenames:
            raise ValueError("No ZIM files selected")

        root = self.kiwix_library_dir()
        inventory = {item["name"]: item for item in self.list_installed_zims()}
        valid_names = {
            name
            for name in filenames
            if isinstance(name, str)
            and Path(name).name == name
            and name.endswith(".zim")
            and name in inventory
        }
        if not valid_names:
            raise ValueError("Selected ZIM files were not found")

        for name in valid_names:
            try:
                (root / name).unlink()
            except FileNotFoundError:
                continue

        env = dict(os.environ)
        env.update(read_env_file(self.prepmaster_env))
        env["PREPMASTER_ENV_FILE"] = str(self.prepmaster_env)
        subprocess.run(
            [str(self.repo_root / "scripts" / "rebuild_kiwix_library.sh")],
            cwd=self.repo_root,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        subprocess.run(
            ["systemctl", "restart", "prepmaster-kiwix.service"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )

        return self.content_status()

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

    def remove_map_packages(self, filenames: list[str]) -> dict:
        if not isinstance(filenames, list) or not filenames:
            raise ValueError("No PMTiles packages selected")

        root = self.maps_root()
        inventory = {item["name"]: item for item in self.pmtiles_inventory()}
        valid_names = {
            name for name in filenames
            if isinstance(name, str)
            and Path(name).name == name
            and name.endswith(".pmtiles")
            and name in inventory
        }

        if not valid_names:
            raise ValueError("Selected PMTiles packages were not found")

        for name in valid_names:
            try:
                (root / name).unlink()
            except FileNotFoundError:
                continue

        remaining_valid = self.list_pmtiles_packages(valid_only=True)
        active = self.active_pmtiles_file()
        if active in valid_names:
            replacement = remaining_valid[0] if remaining_valid else "basemap.pmtiles"
            update_env_file(
                self.prepmaster_env,
                {"PREPMASTER_MAP_PMTILES_FILE": replacement},
            )

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
        valid_wikipedia_options = {
            str(option.get("id", "")).strip()
            for option in self.wikipedia_catalog()["options"]
            if str(option.get("id", "")).strip()
        }
        if valid_wikipedia_options and wikipedia_option not in valid_wikipedia_options:
            raise ValueError("Invalid wikipedia_option")
        zim_mode = payload.get("zim_mode", "full")
        if zim_mode not in {"full", "quick-test", "custom"}:
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

    def system_health(self) -> dict:
        total, used, free = shutil.disk_usage("/")
        return {
            "disk": {
                "total_gb": round(total / (1024 ** 3), 1),
                "used_gb": round(used / (1024 ** 3), 1),
                "free_gb": round(free / (1024 ** 3), 1),
            },
            "temperature_c": self.read_temperature(),
            "cpu": self.read_cpu_load(),
            "uptime": self.read_uptime(),
        }

    def read_service_enabled_statuses(self, services: dict[str, str]) -> dict[str, str]:
        try:
            result = subprocess.run(
                ["systemctl", "is-enabled", *services.values()],
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

    def access_point_status(self) -> dict:
        env = read_env_file(self.prepmaster_env)
        service_units = {
            "network": "prepmaster-ap-network.service",
            "hostapd": "hostapd.service",
            "dnsmasq": "dnsmasq.service",
        }
        services = self.read_service_statuses(service_units)
        enabled_services = self.read_service_enabled_statuses(service_units)
        configured_enabled = env.get("PREPMASTER_AP_ENABLED", "0") == "1"
        running = (
            services.get("network") == "active"
            and services.get("hostapd") == "active"
            and services.get("dnsmasq") == "active"
        )
        return {
            "enabled": configured_enabled,
            "interface": env.get("PREPMASTER_AP_INTERFACE", "wlan0"),
            "ssid": env.get("PREPMASTER_AP_SSID", "SOPRHub"),
            "passphrase": env.get("PREPMASTER_AP_PASSPHRASE", ""),
            "country": env.get("PREPMASTER_AP_COUNTRY", "US"),
            "channel": env.get("PREPMASTER_AP_CHANNEL", "6"),
            "address": env.get("PREPMASTER_AP_ADDRESS", "192.168.50.1"),
            "netmask": env.get("PREPMASTER_AP_NETMASK", "255.255.255.0"),
            "cidr": env.get("PREPMASTER_AP_CIDR", "24"),
            "dhcp_start": env.get("PREPMASTER_AP_DHCP_START", "192.168.50.20"),
            "dhcp_end": env.get("PREPMASTER_AP_DHCP_END", "192.168.50.120"),
            "lease": env.get("PREPMASTER_AP_DHCP_LEASE", "24h"),
            "services": services,
            "enabled_services": enabled_services,
            "running": running,
        }

    def update_access_point_settings(self, payload: dict) -> dict:
        updates: dict[str, str] = {}

        field_map = {
            "interface": "PREPMASTER_AP_INTERFACE",
            "ssid": "PREPMASTER_AP_SSID",
            "passphrase": "PREPMASTER_AP_PASSPHRASE",
            "country": "PREPMASTER_AP_COUNTRY",
            "channel": "PREPMASTER_AP_CHANNEL",
            "address": "PREPMASTER_AP_ADDRESS",
            "netmask": "PREPMASTER_AP_NETMASK",
            "cidr": "PREPMASTER_AP_CIDR",
            "dhcp_start": "PREPMASTER_AP_DHCP_START",
            "dhcp_end": "PREPMASTER_AP_DHCP_END",
            "lease": "PREPMASTER_AP_DHCP_LEASE",
        }

        for payload_key, env_key in field_map.items():
            if payload_key not in payload:
                continue
            value = str(payload.get(payload_key, "")).strip()
            if not value:
                raise ValueError(f"Invalid access point setting: {payload_key}")
            updates[env_key] = value

        if "country" in payload:
            country = updates["PREPMASTER_AP_COUNTRY"].upper()
            if len(country) != 2 or not country.isalpha():
                raise ValueError("Country must be a 2-letter code")
            updates["PREPMASTER_AP_COUNTRY"] = country

        if "channel" in payload:
            channel = updates["PREPMASTER_AP_CHANNEL"]
            if not channel.isdigit():
                raise ValueError("Channel must be numeric")

        if "cidr" in payload:
            cidr = updates["PREPMASTER_AP_CIDR"]
            if not cidr.isdigit():
                raise ValueError("CIDR must be numeric")
            cidr_value = int(cidr)
            if cidr_value < 1 or cidr_value > 32:
                raise ValueError("CIDR must be between 1 and 32")

        if "passphrase" in payload:
            passphrase = updates["PREPMASTER_AP_PASSPHRASE"]
            if len(passphrase) < 8 or len(passphrase) > 63:
                raise ValueError("Passphrase must be between 8 and 63 characters")

        if "enabled" in payload:
            updates["PREPMASTER_AP_ENABLED"] = "1" if bool(payload.get("enabled")) else "0"

        if updates:
            update_env_file(self.prepmaster_env, updates)

        return self.access_point_status()

    def run_access_point_config(self) -> str:
        script = self.repo_root / "scripts" / "configure_access_point.sh"
        env = os.environ.copy()
        env["PREPMASTER_ENV_FILE"] = str(self.prepmaster_env)
        try:
            result = subprocess.run(
                [str(script)],
                capture_output=True,
                text=True,
                check=False,
                env=env,
            )
        except FileNotFoundError as exc:
            raise RuntimeError("Access point configuration script is unavailable") from exc

        output = (result.stdout or "").strip()
        errors = (result.stderr or "").strip()
        if result.returncode != 0:
            raise RuntimeError(errors or output or "Unable to apply access point settings")
        return output

    def apply_access_point_action(self, payload: dict) -> dict:
        action = payload.get("action", "save")
        if action not in {"save", "start", "stop", "apply"}:
            raise ValueError("Invalid access point action")

        updated_payload = dict(payload)
        if action == "start":
            updated_payload["enabled"] = True
        elif action == "stop":
            updated_payload["enabled"] = False

        state = self.update_access_point_settings(updated_payload)
        message = "Access point settings saved."

        if action in {"start", "stop", "apply"}:
            output = self.run_access_point_config()
            state = self.access_point_status()
            if action == "start":
                message = "Access point started."
            elif action == "stop":
                message = "Access point stopped."
            else:
                message = "Access point configuration applied."
            state["message"] = output or message
        else:
            state["message"] = message

        return state

    def access_point_clients(self) -> dict:
        ap = self.access_point_status()
        leases_path = Path("/var/lib/misc/dnsmasq.leases")
        leases_by_mac: dict[str, dict[str, object]] = {}

        if leases_path.exists():
            for line in leases_path.read_text().splitlines():
                parts = line.split()
                if len(parts) < 5:
                    continue
                expires_at, mac, ip_address, hostname, client_id = parts[:5]
                leases_by_mac[mac.lower()] = {
                    "mac": mac.lower(),
                    "ip": ip_address,
                    "hostname": "" if hostname == "*" else hostname,
                    "lease_expires_at": expires_at,
                    "client_id": "" if client_id == "*" else client_id,
                }

        stations: list[dict[str, object]] = []
        try:
            result = subprocess.run(
                ["iw", "dev", ap["interface"], "station", "dump"],
                capture_output=True,
                text=True,
                check=False,
            )
        except FileNotFoundError:
            result = None

        current: dict[str, object] | None = None
        if result and result.returncode == 0:
            for raw_line in result.stdout.splitlines():
                line = raw_line.strip()
                if line.startswith("Station "):
                    if current:
                        stations.append(current)
                    mac = line.split()[1].lower()
                    lease = leases_by_mac.get(mac, {})
                    current = {
                        "mac": mac,
                        "ip": lease.get("ip"),
                        "hostname": lease.get("hostname"),
                        "lease_expires_at": lease.get("lease_expires_at"),
                        "connected": True,
                    }
                    continue
                if not current or ":" not in line:
                    continue
                key, value = [part.strip() for part in line.split(":", 1)]
                if key == "connected time":
                    current["connected_time_seconds"] = int(value.split()[0])
                elif key == "rx bytes":
                    current["rx_bytes"] = int(value)
                elif key == "tx bytes":
                    current["tx_bytes"] = int(value)
                elif key == "rx packets":
                    current["rx_packets"] = int(value)
                elif key == "tx packets":
                    current["tx_packets"] = int(value)
                elif key == "rx bitrate":
                    current["rx_bitrate"] = value
                elif key == "tx bitrate":
                    current["tx_bitrate"] = value
                elif key == "signal":
                    current["signal"] = value
            if current:
                stations.append(current)

        connected_macs = {item["mac"] for item in stations}
        for mac, lease in leases_by_mac.items():
            if mac in connected_macs:
                continue
            stations.append(
                {
                    "mac": mac,
                    "ip": lease.get("ip"),
                    "hostname": lease.get("hostname"),
                    "lease_expires_at": lease.get("lease_expires_at"),
                    "connected": False,
                }
            )

        stations.sort(key=lambda item: (not bool(item.get("connected")), str(item.get("hostname") or item.get("ip") or item["mac"])))
        return {
            "interface": ap["interface"],
            "running": ap["running"],
            "clients": stations,
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
        if path == "/api/system/health":
            self.send_json(self.portal_state.system_health())
            return
        if path == "/api/system/access-point":
            self.send_json(self.portal_state.access_point_status())
            return
        if path == "/api/system/access-point/clients":
            self.send_json(self.portal_state.access_point_clients())
            return
        if path == "/api/content":
            self.send_json(self.portal_state.content_status())
            return
        if path == "/api/content/catalog":
            try:
                force_refresh = query.get("refresh", ["0"])[0] == "1"
                self.send_json(self.portal_state.fetch_kiwix_catalog_cached(force_refresh=force_refresh))
            except RuntimeError as exc:
                self.send_json({"error": str(exc)}, status=502)
            return
        if path == "/api/maps":
            self.portal_state.write_maps_runtime_config()
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

        if self.path == "/api/content/settings":
            try:
                state = self.portal_state.save_content_settings(payload)
            except ValueError as exc:
                self.send_json({"error": str(exc)}, status=400)
                return
            except RuntimeError as exc:
                self.send_json({"error": str(exc)}, status=502)
                return
            self.send_json(state)
            return

        if self.path == "/api/content/remove":
            try:
                state = self.portal_state.remove_zim_files(payload.get("filenames", []))
            except ValueError as exc:
                self.send_json({"error": str(exc)}, status=400)
                return
            self.send_json(state)
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

        if self.path == "/api/maps/remove":
            try:
                state = self.portal_state.remove_map_packages(payload.get("filenames", []))
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

        if self.path == "/api/system/access-point":
            try:
                state = self.portal_state.apply_access_point_action(payload)
            except ValueError as exc:
                self.send_json({"error": str(exc)}, status=400)
                return
            except RuntimeError as exc:
                self.send_json({"error": str(exc)}, status=500)
                return
            self.send_json(state)
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
    print(f"SOPR portal API listening on {args.host}:{args.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
