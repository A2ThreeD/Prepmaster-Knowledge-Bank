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
import yaml
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse
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
    for key, value in list(values.items()):
        if key.startswith("PREPMASTER_"):
            values.setdefault(f"SOPR_{key[11:]}", value)
        elif key.startswith("SOPR_"):
            values.setdefault(f"PREPMASTER_{key[5:]}", value)
    return values


def update_env_file(path: Path, updates: dict[str, str]) -> None:
    lines = path.read_text().splitlines() if path.exists() else []
    existing_keys: set[str] = set()
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key, _ = stripped.split("=", 1)
            existing_keys.add(key)

    normalized_updates: dict[str, str] = {}
    for key, value in updates.items():
        target_key = key
        if key not in existing_keys:
            if key.startswith("PREPMASTER_"):
                alias = f"SOPR_{key[11:]}"
                target_key = alias if alias in existing_keys else alias
            elif key.startswith("SOPR_"):
                alias = f"PREPMASTER_{key[5:]}"
                target_key = alias if alias in existing_keys and key not in existing_keys else key
        normalized_updates[target_key] = value

    seen: set[str] = set()
    result: list[str] = []

    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key, _ = stripped.split("=", 1)
            if key in normalized_updates:
                result.append(f"{key}={normalized_updates[key]}")
                seen.add(key)
                continue
        result.append(line)

    for key, value in normalized_updates.items():
        if key not in seen:
            result.append(f"{key}={value}")

    path.write_text("\n".join(result).rstrip() + "\n")


def read_json(path: Path, default: dict) -> dict:
    candidate = path
    if not candidate.exists():
        if candidate.suffix == ".json":
            alternate = candidate.with_suffix(".yaml")
            candidate = alternate if alternate.exists() else candidate
        elif candidate.suffix in {".yaml", ".yml"}:
            alternate = candidate.with_suffix(".json")
            candidate = alternate if alternate.exists() else candidate
    if not candidate.exists():
        return default
    try:
        loaded = yaml.safe_load(candidate.read_text())
        return loaded if isinstance(loaded, dict) else default
    except yaml.YAMLError:
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
    tokens = cleaned.strip().split()
    if not tokens:
        return None, None
    label = tokens[-1].upper()
    if not re.fullmatch(r"(\d+(?:\.\d+)?[KMGTP]?|-)", label):
        return None, None
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


def parse_git_lfs_pointer_size(text: str) -> int | None:
    match = re.search(r"^size\s+(\d+)\s*$", text, re.MULTILINE)
    if not match:
        return None
    return int(match.group(1))


class PortalState:
    def __init__(self, repo_root: Path, data_dir: Path) -> None:
        self.repo_root = repo_root
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.sopr_env = self.repo_root / "config" / "sopr.env"
        if not self.sopr_env.exists():
            self.sopr_env = self.repo_root / "config" / "prepmaster.env"
        self.install_profile_env = self.repo_root / "config" / "install-profile.env"
        self.state_file = self.data_dir / "portal-state.json"
        self.apply_state_file = self.data_dir / "apply-state.json"
        self.apply_log_file = self.data_dir / "apply.log"
        self.map_sync_state_file = self.data_dir / "map-sync-state.json"
        self.map_sync_log_file = self.data_dir / "map-sync.log"
        self.maps_catalog_cache_file = self.data_dir / "maps-catalog-cache.json"
        self.content_catalog_cache_file = self.data_dir / "content-catalog-cache.json"
        self.external_maps_links_file = self.data_dir / "external-maps-links.json"
        self.external_zims_links_file = self.data_dir / "external-zims-links.json"
        self.apply_lock = threading.Lock()
        self.apply_thread: threading.Thread | None = None
        self.map_sync_lock = threading.Lock()
        self.map_sync_thread: threading.Thread | None = None
        self.content_catalog_lock = threading.Lock()
        self.content_catalog_refresh_thread: threading.Thread | None = None
        self.write_maps_runtime_config()
        self.recover_interrupted_map_sync()
        self.recover_interrupted_apply()
        self.sync_external_content_links()

    def wikipedia_catalog(self) -> dict:
        catalog = read_json(
            self.repo_root / "catalog" / "wikipedia.yaml",
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
                    "size_label": str(option.get("size_label", "")),
                    "url": str(option.get("url", "")),
                    "version": str(option.get("version", "")),
                }
            )
        return {
            "spec_version": catalog.get("spec_version"),
            "options": normalized,
        }

    def kiwix_catalog(self) -> dict:
        return read_json(
            self.repo_root / "catalog" / "kiwix-categories.yaml",
            {"categories": []},
        )

    def kiwix_tier_catalog(self) -> dict[str, dict[str, object]]:
        level_order = {
            "essential": 1,
            "standard": 2,
            "comprehensive": 3,
        }
        document = self.kiwix_catalog()
        categories = document.get("collections") if "collections" in document else document.get("categories", [])
        loadout_key = "loadouts" if "collections" in document else "tiers"
        resource_key = "library_items" if "collections" in document else "resources"
        tier_details: dict[str, dict[str, object]] = {}

        for label, selected_level in level_order.items():
            seen_ids: set[str] = set()
            size_mb = 0
            summary_categories: list[dict[str, object]] = []
            tier_description = ""

            for category in categories:
                category_name = str(category.get("name", "")).strip()
                category_items: list[str] = []
                for tier in category.get(loadout_key, []):
                    tier_slug = str(tier.get("key") if loadout_key == "loadouts" else tier.get("slug", ""))
                    level = 0
                    for level_name, level_value in level_order.items():
                        if tier_slug.endswith(f"-{level_name}"):
                            level = level_value
                            break
                    if level == 0 or level > selected_level:
                        continue
                    if level == selected_level and not tier_description:
                        tier_description = str(tier.get("description", "")).strip()

                    for resource in tier.get(resource_key, []):
                        resource_id = str(resource.get("key") if resource_key == "library_items" else resource.get("id", "")).strip()
                        if not resource_id or resource_id in seen_ids:
                            continue
                        seen_ids.add(resource_id)
                        title = str(resource.get("title", resource_id)).strip()
                        size_value = resource.get("footprint_mb") if resource_key == "library_items" else resource.get("size_mb")
                        try:
                            size_mb += int(size_value or 0)
                        except (TypeError, ValueError):
                            pass
                        if title:
                            category_items.append(title)

                if category_name and category_items:
                    summary_categories.append(
                        {
                            "name": category_name,
                            "items": category_items,
                        }
                    )

            tier_details[label] = {
                "id": label,
                "name": label.capitalize(),
                "description": tier_description,
                "size_mb": size_mb,
                "size_label": format_size_bytes(size_mb * 1024 * 1024),
                "summary": summary_categories,
            }

        return tier_details

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

    def curated_resources(
        self,
        profile: str,
        wikipedia_choice: str,
    ) -> list[dict[str, object]]:
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
        resources: list[dict[str, object]] = []
        seen_ids: set[str] = set()

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
                    resource_url = str(resource.get("download_url") if resource_key == "library_items" else resource.get("url", "")).strip()
                    if not resource_id or not resource_url or resource_id in seen_ids:
                        continue
                    seen_ids.add(resource_id)
                    size_value = resource.get("footprint_mb") if resource_key == "library_items" else resource.get("size_mb")
                    try:
                        size_mb = int(size_value or 0)
                    except (TypeError, ValueError):
                        size_mb = 0
                    resources.append(
                        {
                            "id": resource_id,
                            "url": resource_url,
                            "filename": Path(urlparse(resource_url).path).name,
                            "size_mb": size_mb,
                        }
                    )

        for option in self.wikipedia_catalog().get("options", []):
            if str(option.get("id", "")).strip() != wikipedia_choice:
                continue
            option_url = str(option.get("url", "")).strip()
            if option_url:
                resources.append(
                    {
                        "id": str(option.get("id", "")).strip(),
                        "url": option_url,
                        "filename": Path(urlparse(option_url).path).name,
                        "size_mb": int(option.get("size_mb", 0) or 0),
                    }
                )
            break

        return resources

    def missing_curated_size_mb(self, profile: str, wikipedia_choice: str) -> int:
        installed_names = {
            str(item.get("name", "")).strip()
            for item in self.list_installed_zims()
            if str(item.get("name", "")).strip()
        }
        total_mb = 0
        for resource in self.curated_resources(profile, wikipedia_choice):
            filename = str(resource.get("filename", "")).strip()
            if filename and filename in installed_names:
                continue
            total_mb += int(resource.get("size_mb", 0) or 0)
        return total_mb

    def missing_tier_size_mb(self, profile: str) -> int:
        installed_names = {
            str(item.get("name", "")).strip()
            for item in self.list_installed_zims()
            if str(item.get("name", "")).strip()
        }
        total_mb = 0
        for resource in self.curated_resources(profile, ""):
            filename = str(resource.get("filename", "")).strip()
            if filename and filename in installed_names:
                continue
            total_mb += int(resource.get("size_mb", 0) or 0)
        return total_mb

    def missing_wikipedia_size_mb(self, wikipedia_choice: str) -> int:
        installed_names = {
            str(item.get("name", "")).strip()
            for item in self.list_installed_zims()
            if str(item.get("name", "")).strip()
        }
        for option in self.wikipedia_catalog().get("options", []):
            if str(option.get("id", "")).strip() != wikipedia_choice:
                continue
            option_url = str(option.get("url", "")).strip()
            filename = Path(urlparse(option_url).path).name if option_url else ""
            if filename and filename in installed_names:
                return 0
            return int(option.get("size_mb", 0) or 0)
        return 0

    def setup_storage_summary(self) -> dict:
        total, used, free = shutil.disk_usage("/")
        volumes = self.storage_volumes()
        env = read_env_file(self.sopr_env)
        profile = env.get("PREPMASTER_ZIM_PROFILE", "essential").strip().lower() or "essential"
        if profile not in {"essential", "standard", "comprehensive"}:
            profile = "essential"
        tier_catalog = self.kiwix_tier_catalog()
        kolibri_installed = Path("/usr/bin/kolibri").exists()
        try:
            maps_catalog = self.fetch_nomad_maps_catalog_cached(force_refresh=False)
        except RuntimeError:
            maps_catalog = {"collections": [], "items": []}
        installed_map_names = set(self.list_pmtiles_packages(valid_only=True))
        missing_by_map_collection: dict[str, int] = {}
        for collection in maps_catalog.get("collections", []):
            slug = str(collection.get("slug", "")).strip()
            if not slug:
                continue
            missing_bytes = sum(
                int(item.get("size_bytes", 0))
                for item in maps_catalog.get("items", [])
                if item.get("region_slug") == slug and str(item.get("name", "")) not in installed_map_names
            )
            missing_by_map_collection[slug] = round(missing_bytes / (1024 * 1024))
        missing_all_map_bytes = sum(
            int(item.get("size_bytes", 0))
            for item in maps_catalog.get("items", [])
            if str(item.get("name", "")) not in installed_map_names
        )
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
            "content_tiers": {
                tier_id: {
                    **details,
                    "missing_mb": self.missing_tier_size_mb(tier_id),
                }
                for tier_id, details in tier_catalog.items()
            },
            "content_install_dir": str(self.preferred_zim_install_dir()),
            "content_install_destinations": self.install_destinations("zims"),
            "wikipedia_install_dir": str(self.preferred_wikipedia_install_dir()),
            "wikipedia_install_destinations": self.install_destinations("zims"),
            "missing_by_wikipedia": {
                str(option.get("id", "")).strip(): self.missing_wikipedia_size_mb(
                    str(option.get("id", "")).strip(),
                )
                for option in self.wikipedia_catalog().get("options", [])
                if str(option.get("id", "")).strip()
            },
            "zim_profile": profile,
            "kolibri_estimated_mb": 1500,
            "kolibri_installed": kolibri_installed,
            "kolibri_url": f"http://{self.detect_primary_host()}:{env.get('PREPMASTER_KOLIBRI_PORT', '8082')}/",
            "missing_by_map_collection": missing_by_map_collection,
            "missing_all_maps_mb": round(missing_all_map_bytes / (1024 * 1024)),
            "warning_free_percent": 10,
            "volumes": volumes,
        }

    def load_state(self) -> dict:
        prepmaster = read_env_file(self.sopr_env)
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
            wikipedia_option = "top-mini" if "top-mini" in wikipedia_ids else wikipedia_ids[0]
        try:
            maps_catalog = self.fetch_nomad_maps_catalog_cached(force_refresh=False)
        except RuntimeError:
            maps_catalog = {"collections": [], "items": []}
        map_collections = self.selected_map_collections()
        state = read_json(
            self.state_file,
            {"setup_complete": False, "last_saved_at": None},
        )
        return {
            "setup_complete": bool(state.get("setup_complete", False)),
            "last_saved_at": state.get("last_saved_at"),
            "setup_options": {
                "wikipedia": wikipedia_catalog,
                "maps": {
                    "collections": maps_catalog.get("collections", []),
                    "all": {
                        "slug": "all",
                        "name": "All Regions",
                        "description": "Download the full Project NOMAD regional map collection.",
                        "size_bytes": sum(int(item.get("size_bytes", 0)) for item in maps_catalog.get("items", [])),
                        "size_label": format_size_bytes(
                            sum(int(item.get("size_bytes", 0)) for item in maps_catalog.get("items", []))
                        ) if maps_catalog.get("items") else "",
                        "resource_count": len(maps_catalog.get("items", [])),
                    },
                },
            },
            "profile": {
                "install_kolibri": profile.get("INSTALL_KOLIBRI", "0") == "1",
                "install_ka_lite": profile.get("INSTALL_KA_LITE", "0") == "1",
                "wikipedia_option": wikipedia_option,
                "ap_enabled": prepmaster.get("PREPMASTER_AP_ENABLED", "0") == "1",
                "map_collections": map_collections,
                "map_selected_count": len(self.selected_map_files(map_collections)),
                "zim_mode": prepmaster.get("PREPMASTER_ZIM_MODE", "full"),
                "zim_profile": prepmaster.get("PREPMASTER_ZIM_PROFILE", "essential"),
                "custom_zim_count": len(custom_selection.get("selected_items", [])),
            },
            "storage": self.setup_storage_summary(),
        }

    def maps_env(self) -> dict[str, str]:
        return read_env_file(self.sopr_env)

    def kiwix_library_dir(self) -> Path:
        env = self.maps_env()
        return Path(env.get("KIWIX_LIBRARY_DIR", "/library/zims/content"))

    def preferred_zim_install_dir(self) -> Path:
        env = self.maps_env()
        configured = env.get("PREPMASTER_ZIM_INSTALL_DIR", "").strip()
        return Path(configured) if configured else self.kiwix_library_dir()

    def preferred_wikipedia_install_dir(self) -> Path:
        env = self.maps_env()
        configured = env.get("PREPMASTER_WIKIPEDIA_INSTALL_DIR", "").strip()
        if configured:
            return Path(configured)
        return self.preferred_zim_install_dir()

    def preferred_map_install_dir(self) -> Path:
        env = self.maps_env()
        configured = env.get("PREPMASTER_MAP_INSTALL_DIR", "").strip()
        return Path(configured) if configured else self.maps_root()

    def custom_zim_manifest_path(self) -> Path:
        env = self.maps_env()
        configured = env.get("PREPMASTER_ZIM_CUSTOM_URL_FILE")
        if configured:
            return Path(configured)
        return self.repo_root / "config" / "kiwix-zim-urls.custom.txt"

    def extra_zim_manifest_path(self) -> Path:
        return self.data_dir / "kiwix-zim-urls.extra.txt"

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

    def install_destinations(self, kind: str) -> list[dict[str, object]]:
        if kind == "zims":
            internal_label = "Internal Library"
            internal_path = self.kiwix_library_dir()
            layout_key = "library"
            suffix_label = "Library"
            selected_path = self.preferred_zim_install_dir()
        elif kind == "maps":
            internal_label = "Internal Maps"
            internal_path = self.maps_root()
            layout_key = "maps"
            suffix_label = "Maps"
            selected_path = self.preferred_map_install_dir()
        else:
            raise ValueError(f"Unsupported install destination kind: {kind}")

        try:
            internal_total, internal_used, internal_free = shutil.disk_usage(internal_path)
        except OSError:
            internal_total = internal_used = internal_free = 0

        options: list[dict[str, object]] = [
            {
                "id": str(internal_path),
                "label": internal_label,
                "path": str(internal_path),
                "location": "internal",
                "mounted": True,
                "selected": str(selected_path) == str(internal_path),
                "size_bytes": int(internal_total),
                "used_bytes": int(internal_used),
                "free_bytes": int(internal_free),
            }
        ]

        seen_paths = {str(internal_path)}
        for volume in self.storage_volumes():
            if volume.get("location") != "external":
                continue
            if not volume.get("mounted"):
                continue
            layout = volume.get("sopr_layout") or {}
            if not layout.get(layout_key):
                continue
            mountpoint = str(volume.get("mountpoint") or "").strip()
            if not mountpoint:
                continue
            if not mountpoint.startswith("/media/"):
                continue
            path = Path(mountpoint) / layout_key
            if str(path) in seen_paths:
                continue
            seen_paths.add(str(path))
            volume_name = str(volume.get("label") or volume.get("display_name") or volume.get("name") or "Drive").strip()
            size_label = format_size_bytes(int(volume.get("size_bytes") or 0)) if int(volume.get("size_bytes") or 0) else ""
            mount_label = str(volume.get("mountpoint") or "").strip()
            descriptor_parts = [part for part in (mount_label, size_label) if part]
            descriptor = f" ({' • '.join(descriptor_parts)})" if descriptor_parts else ""
            options.append(
                {
                    "id": str(path),
                    "label": f"External {volume_name} {suffix_label}{descriptor}",
                    "path": str(path),
                    "location": "external",
                    "mounted": path.exists(),
                    "selected": str(selected_path) == str(path),
                    "size_bytes": int(volume.get("size_bytes") or 0),
                    "used_bytes": int(volume.get("used_bytes") or 0),
                    "free_bytes": int(volume.get("free_bytes") or 0),
                }
            )
        return options

    def validate_install_destination(self, kind: str, path_value: str | None) -> str:
        options = self.install_destinations(kind)
        allowed = {str(option["path"]) for option in options}
        if not path_value:
            return str(self.kiwix_library_dir() if kind == "zims" else self.maps_root())
        if path_value not in allowed:
            raise ValueError("Selected install destination is not available.")
        return path_value

    def managed_links_manifest_path(self, kind: str) -> Path:
        if kind == "maps":
            return self.external_maps_links_file
        if kind == "zims":
            return self.external_zims_links_file
        raise ValueError(f"Unsupported managed link kind: {kind}")

    def read_managed_links_manifest(self, kind: str) -> dict[str, str]:
        data = read_json(self.managed_links_manifest_path(kind), {"links": {}})
        links = data.get("links", {})
        if not isinstance(links, dict):
            return {}
        return {
            str(name): str(target)
            for name, target in links.items()
            if isinstance(name, str) and isinstance(target, str)
        }

    def write_managed_links_manifest(self, kind: str, links: dict[str, str]) -> None:
        path = self.managed_links_manifest_path(kind)
        path.write_text(json.dumps({"links": links}, indent=2) + "\n")

    def external_storage_roots(self, directory_name: str) -> list[Path]:
        roots: list[Path] = []
        for volume in self.storage_volumes():
            if not volume.get("mounted"):
                continue
            layout = volume.get("sopr_layout") or {}
            if not layout.get(directory_name):
                continue
            mountpoint = str(volume.get("mountpoint") or "").strip()
            if not mountpoint:
                continue
            roots.append(Path(mountpoint) / directory_name)
        return roots

    def sync_external_links(self, kind: str, canonical_root: Path, external_roots: list[Path], suffix: str) -> None:
        canonical_root.mkdir(parents=True, exist_ok=True)
        manifest = self.read_managed_links_manifest(kind)
        current_targets: dict[str, str] = {}

        for name, target in manifest.items():
            link_path = canonical_root / name
            try:
                if link_path.is_symlink():
                    resolved = str(link_path.resolve(strict=False))
                    if resolved == target or not Path(target).exists():
                        link_path.unlink()
                elif not link_path.exists():
                    pass
            except OSError:
                continue

        for external_root in external_roots:
            if not external_root.exists():
                continue
            for path in sorted(external_root.iterdir()):
                if not path.is_file() or path.suffix.lower() != suffix:
                    continue
                link_path = canonical_root / path.name
                current_targets[path.name] = str(path.resolve())
                if link_path.exists() or link_path.is_symlink():
                    if link_path.is_symlink():
                        try:
                            if str(link_path.resolve(strict=False)) == str(path.resolve()):
                                continue
                        except OSError:
                            pass
                    continue
                try:
                    link_path.symlink_to(path.resolve())
                except FileExistsError:
                    continue

        self.write_managed_links_manifest(kind, current_targets)

    def sync_external_content_links(self) -> None:
        self.sync_external_links("maps", self.maps_root(), self.external_storage_roots("maps"), ".pmtiles")
        zim_roots = self.external_storage_roots("library")
        wikipedia_root = self.preferred_wikipedia_install_dir()
        if (
            str(wikipedia_root) != str(self.kiwix_library_dir())
            and str(wikipedia_root) != str(self.preferred_zim_install_dir())
            and str(wikipedia_root).startswith("/media/")
        ):
            zim_roots.append(wikipedia_root)
        deduped_roots: list[Path] = []
        seen_roots: set[str] = set()
        for root in zim_roots:
            root_key = str(root.resolve(strict=False))
            if root_key in seen_roots:
                continue
            seen_roots.add(root_key)
            deduped_roots.append(root)
        self.sync_external_links("zims", self.kiwix_library_dir(), deduped_roots, ".zim")

    def remove_managed_linked_content(self, kind: str, canonical_root: Path, names: set[str]) -> None:
        manifest = self.read_managed_links_manifest(kind)
        changed = False
        for name in names:
            link_path = canonical_root / name
            target_path = Path(manifest.get(name, ""))
            if target_path and target_path.exists():
                try:
                    target_path.unlink()
                except FileNotFoundError:
                    pass
            try:
                if link_path.is_symlink():
                    link_path.unlink()
            except FileNotFoundError:
                pass
            if name in manifest:
                manifest.pop(name, None)
                changed = True
        if changed:
            self.write_managed_links_manifest(kind, manifest)

    def maps_web_root(self) -> Path:
        env = self.maps_env()
        return Path(env.get("PREPMASTER_MAPS_ROOT", "/srv/sopr/www/maps"))

    def active_pmtiles_file(self) -> str:
        env = self.maps_env()
        return env.get("PREPMASTER_MAP_PMTILES_FILE", "basemap.pmtiles")

    def selected_map_collections(self) -> list[str]:
        env = self.maps_env()
        raw = env.get("PREPMASTER_MAP_SELECTED_COLLECTIONS", "")
        values = [value.strip() for value in raw.split(",") if value.strip()]
        if "all" in values:
            return ["all"]
        seen: set[str] = set()
        ordered: list[str] = []
        for value in values:
            if value in seen:
                continue
            seen.add(value)
            ordered.append(value)
        return ordered

    def selected_map_files(self, collections: list[str] | None = None) -> list[str]:
        chosen = collections if collections is not None else self.selected_map_collections()
        try:
            catalog = self.fetch_nomad_maps_catalog_cached(force_refresh=False)
        except RuntimeError:
            return []
        items = catalog.get("items", [])
        if "all" in chosen:
            return sorted({str(item.get("name", "")) for item in items if str(item.get("name", "")).endswith(".pmtiles")})
        wanted = set(chosen)
        return sorted(
            {
                str(item.get("name", ""))
                for item in items
                if str(item.get("region_slug", "")) in wanted and str(item.get("name", "")).endswith(".pmtiles")
            }
        )

    def list_pmtiles_packages(self, valid_only: bool = False) -> list[str]:
        self.sync_external_content_links()
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
        self.sync_external_content_links()
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
            "install_dir": str(self.preferred_map_install_dir()),
            "install_destinations": self.install_destinations("maps"),
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
            str(self.repo_root / "catalog" / "kiwix-categories.yaml"),
            "--output",
            str(output_path),
            "--profile",
            profile,
            "--wikipedia-options",
            str(self.repo_root / "catalog" / "wikipedia.yaml"),
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

    def write_extra_zim_manifest(
        self,
        items: list[dict[str, object]],
        catalog_root: str,
    ) -> Path:
        manifest = self.extra_zim_manifest_path()
        manifest.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            f"# Extra SOPR ZIM selection from {catalog_root}",
            f"# Saved at: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}",
            f"# Resource count: {len(items)}",
            "",
        ]
        for item in items:
            lines.append(f"# {item.get('category', 'Extra Content')} | {item.get('name', 'ZIM')}")
            lines.append(str(item["download_url"]))
            lines.append("")
        manifest.write_text("\n".join(lines).rstrip() + "\n")
        return manifest

    def catalog_items_for_paths(self, selected_paths: list[str]) -> tuple[list[dict[str, object]], str]:
        catalog = self.fetch_kiwix_catalog_cached(force_refresh=False)
        catalog_by_path = {item["path"]: item for item in catalog["items"]}
        missing = [path for path in selected_paths if path not in catalog_by_path]
        if missing:
            raise ValueError(f"Selected ZIM is not in catalog: {missing[0]}")
        return [catalog_by_path[path] for path in selected_paths], catalog["source"]["root_url"]

    def list_installed_zims(self) -> list[dict[str, object]]:
        self.sync_external_content_links()
        root = self.kiwix_library_dir()
        if not root.exists():
            return []
        volumes = self.storage_volumes()
        inventory: list[dict[str, object]] = []
        for path in sorted(root.iterdir()):
            if not path.is_file() or path.suffix.lower() != ".zim":
                continue
            stat = path.stat()
            resolved = path.resolve(strict=False)
            volume = self.volume_for_path(resolved, volumes)
            location = str(volume.get("location", "internal")) if volume else "internal"
            inventory.append(
                {
                    "name": path.name,
                    "path": str(path),
                    "resolved_path": str(resolved),
                    "location": location,
                    "size_bytes": stat.st_size,
                    "size_label": format_size_bytes(stat.st_size),
                    "modified_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(stat.st_mtime)),
                }
            )
        return inventory

    def current_base_content_filenames(self) -> set[str]:
        env = self.maps_env()
        mode = env.get("PREPMASTER_ZIM_MODE", "full")
        wikipedia_option = env.get("PREPMASTER_WIKIPEDIA_OPTION", "top-mini")
        resources: list[dict[str, object]] = []
        if mode == "quick-test":
            for url in self.read_manifest_urls(self.repo_root / "config" / "kiwix-zim-urls.quick-test.txt"):
                resources.append({"filename": Path(urlparse(url).path).name})
        elif mode == "custom":
            base_profile = self.custom_base_profile()
            if base_profile:
                resources = self.curated_resources(base_profile, wikipedia_option)
        else:
            profile = env.get("PREPMASTER_ZIM_PROFILE", "essential").strip().lower() or "essential"
            resources = self.curated_resources(profile, wikipedia_option)
        return {
            str(item.get("filename", "")).strip()
            for item in resources
            if str(item.get("filename", "")).strip()
        }

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

    def load_content_catalog_cache(self) -> dict | None:
        if not self.content_catalog_cache_file.exists():
            return None
        try:
            return json.loads(self.content_catalog_cache_file.read_text())
        except json.JSONDecodeError:
            return None

    def build_kiwix_catalog_payload(self) -> dict:
        cache_ttl_seconds = 3600
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
            "refreshing": False,
        }
        self.content_catalog_cache_file.write_text(
            json.dumps({"fetched_at": time.time(), "payload": payload}, indent=2) + "\n"
        )
        return payload

    def refresh_kiwix_catalog_in_background(self) -> None:
        try:
            self.build_kiwix_catalog_payload()
        finally:
            with self.content_catalog_lock:
                self.content_catalog_refresh_thread = None

    def start_kiwix_catalog_refresh(self) -> bool:
        with self.content_catalog_lock:
            if self.content_catalog_refresh_thread and self.content_catalog_refresh_thread.is_alive():
                return False
            self.content_catalog_refresh_thread = threading.Thread(
                target=self.refresh_kiwix_catalog_in_background,
                daemon=True,
            )
            self.content_catalog_refresh_thread.start()
            return True

    def fetch_kiwix_catalog_cached(self, force_refresh: bool = False) -> dict:
        cache_ttl_seconds = 3600
        cached = self.load_content_catalog_cache()
        cached_payload = cached.get("payload") if cached else None
        cache_age_seconds = float(cached.get("fetched_at", 0)) if cached else 0
        cache_is_fresh = bool(
            cached_payload and cache_age_seconds and (time.time() - cache_age_seconds < cache_ttl_seconds)
        )

        if force_refresh and cached_payload:
            self.start_kiwix_catalog_refresh()
            payload = dict(cached_payload)
            payload["refreshing"] = True
            payload["error"] = "Refreshing the Kiwix catalog in the background. Showing the most recent saved results for now."
            return payload

        if force_refresh and not cached_payload:
            self.start_kiwix_catalog_refresh()
            return {
                "source": {
                    "root_url": self.zim_catalog_root().rstrip("/") + "/",
                },
                "items": [],
                "stale": False,
                "error": "Building the Kiwix catalog in the background for the first time. Try again in a moment.",
                "refreshing": True,
            }

        if not force_refresh and cache_is_fresh:
            payload = dict(cached_payload)
            payload["refreshing"] = False
            return payload

        if cached_payload and not cache_is_fresh:
            self.start_kiwix_catalog_refresh()
            payload = dict(cached_payload)
            payload["stale"] = True
            payload["refreshing"] = True
            payload["error"] = "Refreshing an older Kiwix catalog in the background. Showing the saved results for now."
            return payload

        return self.build_kiwix_catalog_payload()

    def content_status(self) -> dict:
        env = self.maps_env()
        custom_selection = self.read_custom_zim_selection()
        installed = self.list_installed_zims()
        installed_size_bytes = sum(int(item["size_bytes"]) for item in installed)
        installed_internal_size_bytes = sum(
            int(item["size_bytes"]) for item in installed if item.get("location") != "external"
        )
        installed_external_size_bytes = sum(
            int(item["size_bytes"]) for item in installed if item.get("location") == "external"
        )
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
        base_filenames = self.current_base_content_filenames()
        inventory = []
        for item in installed:
            inventory.append(
                {
                    **item,
                    "selected": item["name"] in selected_names,
                    "in_base_set": item["name"] in base_filenames,
                    "is_extra": item["name"] not in base_filenames,
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
            "installed_internal_size_bytes": installed_internal_size_bytes,
            "installed_internal_size_label": format_size_bytes(installed_internal_size_bytes),
            "installed_external_size_bytes": installed_external_size_bytes,
            "installed_external_size_label": format_size_bytes(installed_external_size_bytes),
            "installed_items": inventory,
            "install_dir": str(self.preferred_zim_install_dir()),
            "install_destinations": self.install_destinations("zims"),
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
        install_dir = payload.get("install_dir")
        if install_dir is not None:
            updates["PREPMASTER_ZIM_INSTALL_DIR"] = self.validate_install_destination("zims", str(install_dir).strip())
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
                selected_items, catalog_root = self.catalog_items_for_paths(cleaned_paths)
                self.write_custom_zim_selection(selected_items, catalog_root)
                self.write_custom_zim_manifest(selected_items, catalog_root, base_profile)
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

        update_env_file(self.sopr_env, updates)
        return self.content_status()

    def download_selected_extra_zims(self, payload: dict) -> dict:
        selected_paths = payload.get("selected_paths")
        if not isinstance(selected_paths, list) or not selected_paths:
            raise ValueError("Select one or more ZIM files first.")

        cleaned_paths = []
        for value in selected_paths:
            if not isinstance(value, str) or "/" not in value or not value.endswith(".zim"):
                raise ValueError("Invalid extra ZIM selection")
            cleaned_paths.append(value)
        cleaned_paths = sorted(set(cleaned_paths))

        selected_items, catalog_root = self.catalog_items_for_paths(cleaned_paths)
        self.write_custom_zim_selection(selected_items, catalog_root)
        self.write_extra_zim_manifest(selected_items, catalog_root)
        content = self.content_status()
        apply = self.start_apply("download-extra-content")
        return {"content": content, "apply": apply}

    def remove_zim_files(self, filenames: list[str]) -> dict:
        if not isinstance(filenames, list) or not filenames:
            raise ValueError("No ZIM files selected")

        self.sync_external_content_links()
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
            path = root / name
            if path.is_symlink():
                self.remove_managed_linked_content("zims", root, {name})
                continue
            try:
                path.unlink()
            except FileNotFoundError:
                continue

        env = dict(os.environ)
        env.update(read_env_file(self.sopr_env))
        env["SOPR_ENV_FILE"] = str(self.sopr_env)
        env["PREPMASTER_ENV_FILE"] = str(self.sopr_env)
        subprocess.run(
            [str(self.repo_root / "scripts" / "rebuild_kiwix_library.sh")],
            cwd=self.repo_root,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        subprocess.run(
            ["systemctl", "restart", "sopr-kiwix.service"],
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
            "collections_url": env.get(
                "PREPMASTER_MAP_COLLECTIONS_URL",
                "https://raw.githubusercontent.com/Crosstalk-Solutions/project-nomad/refs/heads/main/collections/maps.json",
            ),
        }

    def nomad_repo_cache_root(self) -> Path:
        return self.data_dir / "nomad-maps-repo"

    def local_nomad_maps_catalog_path(self) -> Path:
        return self.repo_root / "catalog" / "nomad-maps.json"

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

    def fetch_nomad_maps_collections_document(self, force_refresh: bool = False) -> dict:
        local_path = self.local_nomad_maps_catalog_path()
        if not force_refresh and local_path.exists():
            cached = read_json(local_path, {})
            if cached:
                return cached

        source = self.maps_catalog_source()
        req = request.Request(
            source["collections_url"],
            headers={"User-Agent": "SOPR-Portal"},
        )
        try:
            with request.urlopen(req, timeout=30) as response:
                document = json.loads(response.read().decode("utf-8"))
        except error.URLError as exc:
            cached = read_json(local_path, {})
            if cached:
                return cached
            raise RuntimeError(f"Unable to fetch Project NOMAD maps catalog: {exc}") from exc

        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_text(json.dumps(document, indent=2) + "\n")
        return document

    def lfs_pointer_size_bytes(self, download_url: str | None) -> int | None:
        if not download_url:
            return None
        req = request.Request(
            download_url,
            headers={
                "Accept": "application/vnd.github.raw",
                "User-Agent": "SOPR-Portal",
            },
        )
        try:
            with request.urlopen(req, timeout=15) as response:
                pointer_text = response.read(1024).decode("utf-8", errors="replace")
        except error.URLError:
            return None
        if "git-lfs.github.com/spec" not in pointer_text:
            return None
        return parse_git_lfs_pointer_size(pointer_text)

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
        document = self.fetch_nomad_maps_collections_document(force_refresh=force_refresh)
        installed_info = {item["name"]: item for item in self.pmtiles_inventory()}
        active = self.active_pmtiles_file()
        items = []
        collections = []
        for collection in document.get("collections", []):
            resources = []
            collection_size_bytes = 0
            installed_count = 0
            for resource in collection.get("resources", []):
                url = str(resource.get("url", ""))
                name = Path(urlparse(url).path).name
                if not name.endswith(".pmtiles"):
                    continue
                size_mb = int(resource.get("size_mb", 0) or 0)
                size_bytes = size_mb * 1024 * 1024
                installed = name in installed_info
                if installed:
                    installed_count += 1
                entry = {
                    "name": name,
                    "id": str(resource.get("id", "")),
                    "title": str(resource.get("title", name)),
                    "description": str(resource.get("description", "")),
                    "version": str(resource.get("version", "")),
                    "region_slug": str(collection.get("slug", "")),
                    "region_name": str(collection.get("name", "")),
                    "size_bytes": size_bytes,
                    "size_label": format_size_bytes(size_bytes) if size_bytes else "",
                    "lfs_backed": True,
                    "download_url": url,
                    "installed": installed,
                    "installed_valid": bool(installed_info.get(name, {}).get("valid")),
                    "installed_size_bytes": int(installed_info.get(name, {}).get("size_bytes", 0)),
                    "installed_error": installed_info.get(name, {}).get("error"),
                    "active": name == active,
                }
                resources.append(entry)
                items.append(entry)
                collection_size_bytes += size_bytes

            collections.append(
                {
                    "slug": str(collection.get("slug", "")),
                    "name": str(collection.get("name", "")),
                    "description": str(collection.get("description", "")),
                    "size_bytes": collection_size_bytes,
                    "size_label": format_size_bytes(collection_size_bytes) if collection_size_bytes else "",
                    "resource_names": [item["name"] for item in resources],
                    "resource_count": len(resources),
                    "installed_count": installed_count,
                }
            )

        payload = {
            "source": source,
            "collections": collections,
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
            self.sopr_env,
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

        install_dir = payload.get("install_dir")
        if install_dir is not None:
            updates["PREPMASTER_MAP_INSTALL_DIR"] = self.validate_install_destination("maps", str(install_dir).strip())

        if updates:
            update_env_file(self.sopr_env, updates)
            self.write_maps_runtime_config()

        return self.maps_status()

    def remove_map_packages(self, filenames: list[str]) -> dict:
        if not isinstance(filenames, list) or not filenames:
            raise ValueError("No PMTiles packages selected")

        self.sync_external_content_links()
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
            path = root / name
            if path.is_symlink():
                self.remove_managed_linked_content("maps", root, {name})
                continue
            try:
                path.unlink()
            except FileNotFoundError:
                continue

        remaining_valid = self.list_pmtiles_packages(valid_only=True)
        active = self.active_pmtiles_file()
        if active in valid_names:
            replacement = remaining_valid[0] if remaining_valid else "basemap.pmtiles"
            update_env_file(
                self.sopr_env,
                {"PREPMASTER_MAP_PMTILES_FILE": replacement},
            )

        self.write_maps_runtime_config()
        return self.maps_status()

    def read_map_sync_log_tail(self, lines: int = 30) -> list[str]:
        if not self.map_sync_log_file.exists():
            return []
        return self.map_sync_log_file.read_text().splitlines()[-lines:]

    def recover_interrupted_map_sync(self) -> None:
        state = read_json(self.map_sync_state_file, {})
        if state.get("status") != "running":
            return
        self.save_map_sync_state(
            {
                "status": "failed",
                "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "error": "Map sync was interrupted and is no longer running. Start the sync again if you still want these maps.",
                "current_file": None,
            }
        )

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
        root = self.preferred_map_install_dir()
        canonical_root = self.maps_root()
        root.mkdir(parents=True, exist_ok=True)
        canonical_root.mkdir(parents=True, exist_ok=True)

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

                    if root != canonical_root:
                        link_path = canonical_root / name
                        if not link_path.exists() and not link_path.is_symlink():
                            link_path.symlink_to(destination.resolve())

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

                installed_now = {
                    path.name
                    for path in root.iterdir()
                    if path.is_file() and path.suffix.lower() == ".pmtiles"
                }
                for name in sorted(installed_now):
                    if name in managed_remote_names and name not in set(selected_files):
                        target = root / name
                        log_handle.write(f"Removing unchecked map: {name}\n")
                        log_handle.flush()
                        try:
                            target.unlink()
                        except FileNotFoundError:
                            pass
                        link_path = canonical_root / name
                        if link_path.is_symlink():
                            try:
                                if str(link_path.resolve(strict=False)) == str(target.resolve(strict=False)):
                                    link_path.unlink()
                            except OSError:
                                pass

            self.sync_external_content_links()
            installed_after = self.list_pmtiles_packages(valid_only=True)
            active = self.active_pmtiles_file()
            if active not in installed_after:
                replacement = installed_after[0] if installed_after else "basemap.pmtiles"
                update_env_file(self.sopr_env, {"PREPMASTER_MAP_PMTILES_FILE": replacement})

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
        zim_profile = str(payload.get("zim_profile", "essential")).strip().lower()
        if zim_profile not in {"essential", "standard", "comprehensive"}:
            raise ValueError("Invalid zim_profile")
        content_install_dir = payload.get("content_install_dir")
        if content_install_dir is None:
            content_install_dir = str(self.preferred_zim_install_dir())
        content_install_dir = self.validate_install_destination("zims", str(content_install_dir).strip())
        if zim_profile == "essential":
            content_install_dir = str(self.kiwix_library_dir())
        wikipedia_install_dir = payload.get("wikipedia_install_dir")
        if wikipedia_install_dir is None:
            wikipedia_install_dir = str(self.preferred_wikipedia_install_dir())
        wikipedia_install_dir = self.validate_install_destination("zims", str(wikipedia_install_dir).strip())
        if wikipedia_option == "top-mini":
            wikipedia_install_dir = str(self.kiwix_library_dir())
        zim_mode = payload.get("zim_mode", "full")
        if zim_mode not in {"full", "quick-test", "custom"}:
            raise ValueError("Invalid zim_mode")
        map_collections = payload.get("map_collections", [])
        if map_collections is None:
            map_collections = []
        if not isinstance(map_collections, list):
            raise ValueError("Invalid map_collections")
        try:
            maps_catalog = self.fetch_nomad_maps_catalog_cached(force_refresh=False)
        except RuntimeError:
            maps_catalog = {"collections": [], "items": []}
        valid_map_collections = {
            str(collection.get("slug", "")).strip()
            for collection in maps_catalog.get("collections", [])
            if str(collection.get("slug", "")).strip()
        }
        cleaned_map_collections: list[str] = []
        for value in map_collections:
            slug = str(value).strip()
            if not slug:
                continue
            if slug != "all" and slug not in valid_map_collections:
                raise ValueError(f"Invalid map collection: {slug}")
            cleaned_map_collections.append(slug)
        if "all" in cleaned_map_collections:
            cleaned_map_collections = ["all"]
        else:
            cleaned_map_collections = sorted(set(cleaned_map_collections))
        selected_map_files = self.selected_map_files(cleaned_map_collections)

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
            self.sopr_env,
            {
                "PREPMASTER_WIKIPEDIA_OPTION": wikipedia_option,
                "PREPMASTER_ZIM_PROFILE": zim_profile,
                "PREPMASTER_ZIM_INSTALL_DIR": content_install_dir,
                "PREPMASTER_WIKIPEDIA_INSTALL_DIR": wikipedia_install_dir,
                "PREPMASTER_AP_ENABLED": "1" if ap_enabled else "0",
                "PREPMASTER_ZIM_MODE": zim_mode,
                "PREPMASTER_MAP_SELECTED_COLLECTIONS": ",".join(cleaned_map_collections),
                "PREPMASTER_MAP_SELECTED_FILES": ",".join(selected_map_files),
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
                "portal": "sopr-portal.service",
                "kiwix": "sopr-kiwix.service",
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
            "kiwix_url": f"http://{self.detect_primary_host()}:{read_env_file(self.sopr_env).get('KIWIX_PORT', '8080')}/",
            "content_mode": read_env_file(self.sopr_env).get(
                "PREPMASTER_ZIM_MODE", "full"
            ),
            "services": services,
        }

    def system_health(self) -> dict:
        storage = self.storage_health()
        return {
            "disk": storage["disk"],
            "storage": storage,
            "temperature_c": self.read_temperature(),
            "cpu": self.read_cpu_load(),
            "memory": self.read_memory_stats(),
            "uptime": self.read_uptime(),
            "services": self.system_service_health(),
        }

    def storage_health(self) -> dict:
        total, used, free = shutil.disk_usage("/")
        volumes = self.storage_volumes()
        targets = self.storage_targets(volumes)
        seen_targets: set[str] = set()
        sopr_bytes = 0
        for target in targets:
            resolved = str(target.get("resolved_path", ""))
            if resolved in seen_targets:
                continue
            seen_targets.add(resolved)
            sopr_bytes += int(target.get("current_bytes", 0) or 0)
        return {
            "disk": {
                "total_bytes": total,
                "used_bytes": used,
                "free_bytes": free,
                "total_gb": round(total / (1024 ** 3), 1),
                "used_gb": round(used / (1024 ** 3), 1),
                "free_gb": round(free / (1024 ** 3), 1),
            },
            "sopr_bytes": sopr_bytes,
            "volumes": volumes,
            "targets": targets,
        }

    def storage_volumes(self) -> list[dict]:
        try:
            result = subprocess.run(
                [
                    "lsblk",
                    "-J",
                    "-b",
                    "-o",
                    "NAME,PATH,SIZE,TYPE,MOUNTPOINTS,RM,HOTPLUG,MODEL,LABEL,FSTYPE,UUID,TRAN",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
        except FileNotFoundError:
            return []

        if result.returncode != 0:
            return []

        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            return []

        volumes: list[dict] = []

        def visit(device: dict, inherited: dict[str, object] | None = None) -> None:
            inherited = inherited or {}
            children = device.get("children") or []
            mountpoints = [value for value in (device.get("mountpoints") or []) if value]
            device_type = str(device.get("type") or "").strip().lower()
            removable = (
                str(device.get("rm") or "0") == "1"
                or str(device.get("hotplug") or "0") == "1"
                or bool(inherited.get("removable"))
            )
            transport = str(device.get("tran") or inherited.get("transport") or "").strip().lower()
            external = removable or transport == "usb"
            keep = bool(mountpoints) or external or (device_type == "disk" and not children)
            if device_type in {"disk", "part"} and keep:
                mountpoint = mountpoints[0] if mountpoints else ""
                total_bytes = int(device.get("size") or 0)
                used_bytes = 0
                free_bytes = 0
                if mountpoint:
                    try:
                        disk_total, disk_used, disk_free = shutil.disk_usage(mountpoint)
                        total_bytes = disk_total
                        used_bytes = disk_used
                        free_bytes = disk_free
                    except OSError:
                        pass
                location = "external" if external else "internal"
                label = str(device.get("label") or "").strip()
                model = str(device.get("model") or inherited.get("model") or "").strip()
                display_name = label or model or str(device.get("name") or "").strip()
                filesystem = str(device.get("fstype") or "").strip()
                suggested_mountpoint = self.default_mountpoint_for_volume(
                    {
                        "name": str(device.get("name") or "").strip(),
                        "label": label,
                        "uuid": str(device.get("uuid") or "").strip(),
                    }
                )
                sopr_layout = self.storage_layout_for_mount(mountpoint) if mountpoint else {}
                volumes.append(
                    {
                        "name": str(device.get("name") or "").strip(),
                        "path": str(device.get("path") or "").strip(),
                        "display_name": display_name,
                        "label": label,
                        "model": model,
                        "filesystem": filesystem,
                        "uuid": str(device.get("uuid") or "").strip(),
                        "location": location,
                        "mountpoint": mountpoint,
                        "mountpoints": mountpoints,
                        "mounted": bool(mountpoint),
                        "suggested_mountpoint": suggested_mountpoint,
                        "size_bytes": total_bytes,
                        "used_bytes": used_bytes,
                        "free_bytes": free_bytes,
                        "used_percent": round((used_bytes / total_bytes) * 100) if mountpoint and total_bytes else 0,
                        "is_partition": device_type == "part",
                        "can_mount": bool(filesystem and not mountpoint and device_type == "part"),
                        "can_unmount": bool(external and mountpoint and device_type == "part" and mountpoint.startswith("/media/")),
                        "can_prepare": bool(external and device_type == "part"),
                        "sopr_layout": sopr_layout,
                    }
                )
            inherited_child = {
                "transport": transport,
                "model": str(device.get("model") or inherited.get("model") or "").strip(),
                "removable": removable,
            }
            for child in children:
                visit(child, inherited_child)

        for device in payload.get("blockdevices", []):
            visit(device)

        volumes.sort(
            key=lambda item: (
                0 if item.get("location") == "internal" else 1,
                0 if item.get("mounted") else 1,
                str(item.get("mountpoint") or item.get("path") or item.get("name")),
            )
        )
        return volumes

    def storage_layout_for_mount(self, mountpoint: str) -> dict[str, bool]:
        root = Path(mountpoint)
        return {
            "maps": (root / "maps").is_dir(),
            "library": (root / "library").is_dir(),
            "media": (root / "media").is_dir(),
        }

    def slugify_storage_name(self, value: str) -> str:
        cleaned = re.sub(r"[^a-z0-9]+", "-", value.strip().lower())
        return cleaned.strip("-") or "drive"

    def default_mountpoint_for_volume(self, volume: dict) -> str:
        source = (
            str(volume.get("label") or "").strip()
            or str(volume.get("uuid") or "").strip()
            or str(volume.get("name") or "").strip()
        )
        slug = self.slugify_storage_name(source)
        return f"/media/sopr/{slug}"

    def storage_volume_by_device(self, device_path: str) -> dict | None:
        for volume in self.storage_volumes():
            if str(volume.get("path")) == device_path:
                return volume
        return None

    def run_storage_command(self, command: list[str], error_hint: str) -> None:
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                check=False,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(error_hint) from exc
        if result.returncode != 0:
            message = (result.stderr or result.stdout or "").strip() or error_hint
            raise RuntimeError(message)

    def validate_storage_label(self, value: str) -> str:
        label = str(value or "").strip()
        if not label:
            raise ValueError("Enter a drive name.")
        if len(label) > 16:
            raise ValueError("Drive names must be 16 characters or fewer.")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", label):
            raise ValueError("Drive names may only use letters, numbers, hyphens, and underscores.")
        return label

    def storage_label_command(self, volume: dict, label: str) -> list[str]:
        filesystem = str(volume.get("filesystem") or "").strip().lower()
        device_path = str(volume.get("path") or "").strip()
        if filesystem in {"ext2", "ext3", "ext4"}:
            return ["e2label", device_path, label]
        if filesystem in {"vfat", "fat", "fat16", "fat32"}:
            return ["fatlabel", device_path, label]
        if filesystem == "exfat":
            return ["exfatlabel", device_path, label]
        if filesystem in {"ntfs", "ntfs3"}:
            return ["ntfslabel", device_path, label]
        raise ValueError(f"Renaming is not supported for {filesystem or 'this filesystem'} drives.")

    def update_storage_install_paths_for_mount_change(self, old_mountpoint: str, new_mountpoint: str) -> None:
        old_mountpoint = str(old_mountpoint or "").strip()
        new_mountpoint = str(new_mountpoint or "").strip()
        if not old_mountpoint or not new_mountpoint or old_mountpoint == new_mountpoint:
            return

        preferred_zim_dir = self.preferred_zim_install_dir()
        preferred_map_dir = self.preferred_map_install_dir()
        updates: dict[str, str] = {}
        if str(preferred_zim_dir).startswith(f"{old_mountpoint}/"):
            suffix = str(preferred_zim_dir)[len(old_mountpoint):]
            updates["PREPMASTER_ZIM_INSTALL_DIR"] = f"{new_mountpoint}{suffix}"
        if str(preferred_map_dir).startswith(f"{old_mountpoint}/"):
            suffix = str(preferred_map_dir)[len(old_mountpoint):]
            updates["PREPMASTER_MAP_INSTALL_DIR"] = f"{new_mountpoint}{suffix}"
        if updates:
            update_env_file(self.sopr_env, updates)

    def mount_storage_volume(self, payload: dict) -> dict:
        device_path = str(payload.get("device_path", "")).strip()
        if not device_path:
            raise ValueError("Choose a drive to mount.")
        volume = self.storage_volume_by_device(device_path)
        if not volume:
            raise ValueError("That drive was not found.")
        if not volume.get("is_partition"):
            raise ValueError("Choose a partition instead of the whole disk.")
        if volume.get("mounted"):
            return self.storage_health()
        if not volume.get("filesystem"):
            raise ValueError("This drive is not formatted yet. Use Prepare Drive first.")
        mountpoint = str(volume.get("suggested_mountpoint") or "").strip()
        if not mountpoint:
            raise RuntimeError("Unable to determine a mount point for this drive.")
        Path(mountpoint).mkdir(parents=True, exist_ok=True)
        self.run_storage_command(["mount", device_path, mountpoint], "Unable to mount the selected drive.")
        self.sync_external_content_links()
        return self.storage_health()

    def unmount_storage_volume(self, payload: dict) -> dict:
        device_path = str(payload.get("device_path", "")).strip()
        if not device_path:
            raise ValueError("Choose a drive to unmount.")
        volume = self.storage_volume_by_device(device_path)
        if not volume:
            raise ValueError("That drive was not found.")
        if volume.get("location") != "external":
            raise ValueError("Only external drives can be unmounted from SOPR.")
        if not volume.get("mounted"):
            return self.storage_health()

        mountpoint = str(volume.get("mountpoint") or "").strip()
        if not mountpoint or not mountpoint.startswith("/media/"):
            raise ValueError("This drive is not mounted at a removable-storage path.")

        preferred_zim_dir = self.preferred_zim_install_dir()
        preferred_map_dir = self.preferred_map_install_dir()
        updates: dict[str, str] = {}
        if str(preferred_zim_dir).startswith(f"{mountpoint}/"):
            updates["PREPMASTER_ZIM_INSTALL_DIR"] = str(self.kiwix_library_dir())
        if str(preferred_map_dir).startswith(f"{mountpoint}/"):
            updates["PREPMASTER_MAP_INSTALL_DIR"] = str(self.maps_root())
        if updates:
            update_env_file(self.sopr_env, updates)

        self.run_storage_command(["umount", device_path], "Unable to unmount the selected drive.")
        self.sync_external_content_links()
        self.write_maps_runtime_config()

        env = dict(os.environ)
        env.update(read_env_file(self.sopr_env))
        env["SOPR_ENV_FILE"] = str(self.sopr_env)
        env["PREPMASTER_ENV_FILE"] = str(self.sopr_env)
        subprocess.run(
            [str(self.repo_root / "scripts" / "rebuild_kiwix_library.sh")],
            cwd=self.repo_root,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        subprocess.run(
            ["systemctl", "restart", "sopr-kiwix.service"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return self.storage_health()

    def prepare_storage_volume(self, payload: dict) -> dict:
        device_path = str(payload.get("device_path", "")).strip()
        if not device_path:
            raise ValueError("Choose a drive to prepare.")
        volume = self.storage_volume_by_device(device_path)
        if not volume:
            raise ValueError("That drive was not found.")
        if not volume.get("can_prepare"):
            raise ValueError("Only external partitions can be prepared from SOPR.")
        expected = f"FORMAT {device_path}"
        if str(payload.get("confirm_text", "")).strip() != expected:
            raise ValueError(f'Type "{expected}" to confirm preparing this drive.')
        label = self.validate_storage_label(payload.get("label", "SOPRDATA"))
        if volume.get("mounted"):
            self.run_storage_command(["umount", device_path], "Unable to unmount the selected drive.")
        self.run_storage_command(
            ["mkfs.ext4", "-F", "-L", label, device_path],
            "Unable to format the selected drive.",
        )
        refreshed_volume = self.storage_volume_by_device(device_path) or volume
        mountpoint = str(refreshed_volume.get("suggested_mountpoint") or volume.get("suggested_mountpoint") or "").strip()
        if not mountpoint:
            raise RuntimeError("Unable to determine a mount point for this drive.")
        Path(mountpoint).mkdir(parents=True, exist_ok=True)
        self.run_storage_command(["mount", device_path, mountpoint], "Unable to mount the prepared drive.")
        root = Path(mountpoint)
        for directory_name in ("maps", "library", "media"):
            (root / directory_name).mkdir(parents=True, exist_ok=True)
        (root / "SOPR-DRIVE.txt").write_text(
            "This drive was prepared by SOPR.\n\n"
            "maps/    offline map archives\n"
            "library/ offline knowledge library content\n"
            "media/   extra media files\n"
        )
        self.sync_external_content_links()
        return self.storage_health()

    def rename_storage_volume(self, payload: dict) -> dict:
        device_path = str(payload.get("device_path", "")).strip()
        if not device_path:
            raise ValueError("Choose a drive to rename.")
        volume = self.storage_volume_by_device(device_path)
        if not volume:
            raise ValueError("That drive was not found.")
        if volume.get("location") != "external":
            raise ValueError("Only external drives can be renamed from SOPR.")
        if not volume.get("is_partition"):
            raise ValueError("Choose a partition instead of the whole disk.")
        if not volume.get("filesystem"):
            raise ValueError("This drive is not formatted yet. Prepare it first.")
        label = self.validate_storage_label(payload.get("label", ""))
        command = self.storage_label_command(volume, label)
        old_mountpoint = str(volume.get("mountpoint") or "").strip()
        self.run_storage_command(command, "Unable to rename the selected drive.")
        if old_mountpoint and old_mountpoint.startswith("/media/sopr/"):
            refreshed_volume = self.storage_volume_by_device(device_path) or volume
            new_mountpoint = str(refreshed_volume.get("suggested_mountpoint") or "").strip()
            if new_mountpoint and new_mountpoint != old_mountpoint:
                self.run_storage_command(["umount", device_path], "Unable to remount the renamed drive.")
                Path(new_mountpoint).mkdir(parents=True, exist_ok=True)
                self.run_storage_command(["mount", device_path, new_mountpoint], "Unable to remount the renamed drive.")
                self.update_storage_install_paths_for_mount_change(old_mountpoint, new_mountpoint)
                self.sync_external_content_links()
                self.write_maps_runtime_config()
        return self.storage_health()

    def storage_targets(self, volumes: list[dict]) -> list[dict]:
        targets = [
            ("maps", "Maps", self.maps_root()),
            ("library", "Library", self.kiwix_library_dir()),
        ]
        resolved_targets = [
            self.describe_storage_target(target_id, label, path, volumes)
            for target_id, label, path in targets
        ]
        for volume in volumes:
            mountpoint = str(volume.get("mountpoint") or "").strip()
            if not mountpoint:
                continue
            layout = volume.get("sopr_layout") or {}
            for directory_name, label in (
                ("maps", "Maps Folder"),
                ("library", "Library Folder"),
                ("media", "Media Folder"),
            ):
                if not layout.get(directory_name):
                    continue
                resolved_targets.append(
                    self.describe_storage_target(
                        f"available-{directory_name}-{volume.get('name')}",
                        label,
                        Path(mountpoint) / directory_name,
                        volumes,
                        role="available",
                        source_volume=volume,
                    )
                )
        return resolved_targets

    def describe_storage_target(
        self,
        target_id: str,
        label: str,
        path: Path,
        volumes: list[dict],
        role: str = "active",
        source_volume: dict | None = None,
    ) -> dict:
        resolved_path = path.resolve(strict=False)
        current_bytes = self.directory_size_bytes(path)
        volume = source_volume or self.volume_for_path(resolved_path, volumes)
        symlink_target = ""
        if path.is_symlink():
            try:
                symlink_target = os.readlink(path)
            except OSError:
                symlink_target = ""
        return {
            "id": target_id,
            "label": label,
            "configured_path": str(path),
            "resolved_path": str(resolved_path),
            "exists": path.exists(),
            "is_symlink": path.is_symlink(),
            "symlink_target": symlink_target,
            "current_bytes": current_bytes,
            "role": role,
            "volume": volume,
        }

    def directory_size_bytes(self, path: Path) -> int:
        if not path.exists():
            return 0
        if path.is_file():
            try:
                return path.stat().st_size
            except OSError:
                return 0
        total = 0
        for root, _, files in os.walk(path):
            root_path = Path(root)
            for filename in files:
                try:
                    total += (root_path / filename).stat().st_size
                except OSError:
                    continue
        return total

    def volume_for_path(self, path: Path, volumes: list[dict]) -> dict | None:
        resolved = os.path.realpath(str(path))
        best_match: dict | None = None
        best_length = -1
        for volume in volumes:
            mountpoint = str(volume.get("mountpoint") or "").strip()
            if not mountpoint:
                continue
            mount_real = os.path.realpath(mountpoint)
            if resolved == mount_real or resolved.startswith(mount_real.rstrip("/") + "/"):
                if len(mount_real) > best_length:
                    best_match = volume
                    best_length = len(mount_real)
        return best_match

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
        env = read_env_file(self.sopr_env)
        service_units = {
            "network": "sopr-ap-network.service",
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
            update_env_file(self.sopr_env, updates)

        return self.access_point_status()

    def run_access_point_config(self) -> str:
        script = self.repo_root / "scripts" / "configure_access_point.sh"
        env = os.environ.copy()
        env["SOPR_ENV_FILE"] = str(self.sopr_env)
        env["PREPMASTER_ENV_FILE"] = str(self.sopr_env)
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
        step = str(state.get("step") or "")
        state["downloads_active"] = bool(
            state.get("status") == "running"
            and step in {
                "Downloading selected Kiwix content",
                "Downloading selected extra Kiwix content",
                "Installing selected offline maps",
            }
        )
        state["can_leave_to_home"] = bool(
            state.get("status") == "succeeded"
            or state.get("downloads_active")
        )
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
        if action not in {"full", "refresh-content", "rebuild-library", "download-extra-content"}:
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
        if action not in {"full", "refresh-content", "rebuild-library", "download-extra-content"}:
            return

        self.launch_apply(
            action,
            clear_log=False,
            started_at=state.get("started_at"),
            resumed=True,
        )

    def commands_for_action(self, action: str) -> list[tuple[str, list[str]]]:
        if action == "download-extra-content":
            return [
                (
                    "Downloading selected extra Kiwix content",
                    [str(self.repo_root / "scripts" / "download_kiwix_zims.sh")],
                ),
                (
                    "Restarting Kiwix service",
                    ["systemctl", "restart", "sopr-kiwix.service"],
                ),
            ]

        if action == "refresh-content":
            return [
                (
                    "Downloading selected Kiwix content",
                    [str(self.repo_root / "scripts" / "download_kiwix_zims.sh")],
                ),
                (
                    "Restarting Kiwix service",
                    ["systemctl", "restart", "sopr-kiwix.service"],
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
                    ["systemctl", "restart", "sopr-kiwix.service"],
                ),
            ]

        return [
            (
                "Installing optional components",
                [str(self.repo_root / "scripts" / "install_optional_components.sh")],
            ),
            (
                "Applying wireless AP settings",
                [str(self.repo_root / "scripts" / "configure_access_point.sh")],
            ),
            (
                "Restarting core services",
                ["systemctl", "restart", "sopr-kiwix.service"],
            ),
            (
                "Reloading Nginx",
                ["systemctl", "reload", "nginx"],
            ),
            (
                "Downloading selected Kiwix content",
                [str(self.repo_root / "scripts" / "download_kiwix_zims.sh")],
            ),
            (
                "Installing selected offline maps",
                ["__internal_map_sync__"],
            ),
            (
                "Rebuilding Kiwix library",
                [str(self.repo_root / "scripts" / "rebuild_kiwix_library.sh")],
            ),
            (
                "Restarting core services",
                ["systemctl", "restart", "sopr-kiwix.service"],
            ),
            (
                "Reloading Nginx",
                ["systemctl", "reload", "nginx"],
            ),
        ]

    def run_apply_workflow(self, action: str) -> None:
        commands = self.commands_for_action(action)

        env = dict(os.environ)
        env.update(read_env_file(self.sopr_env))
        env.update(
            {
                "SOPR_ENV_FILE": str(self.sopr_env),
                "PREPMASTER_ENV_FILE": str(self.sopr_env),
                "PREPMASTER_PROFILE_FILE": str(self.install_profile_env),
                "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            }
        )
        if action == "download-extra-content":
            env["PREPMASTER_ZIM_MODE"] = "custom"
            env["PREPMASTER_ZIM_CUSTOM_URL_FILE"] = str(self.extra_zim_manifest_path())

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
                if command == ["__internal_map_sync__"]:
                    selected_files = self.selected_map_files()
                    if not selected_files:
                        log_handle.write("No offline maps selected for setup.\n")
                        log_handle.flush()
                        continue
                    self.map_sync_log_file.write_text("")
                    self.save_map_sync_state(
                        {
                            "status": "running",
                            "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                            "finished_at": None,
                            "current_file": None,
                            "current_index": 0,
                            "total_files": len(selected_files),
                            "progress_percent": 0,
                            "error": None,
                            "selected_files": selected_files,
                        }
                    )
                    log_handle.write(f"Selected map packages: {', '.join(selected_files)}\n")
                    log_handle.write(
                        "Detailed map-sync progress is also available from the Maps section in Settings.\n"
                    )
                    log_handle.flush()
                    self.run_map_sync(selected_files)
                    map_sync_state = self.load_map_sync_state()
                    if map_sync_state.get("status") != "succeeded":
                        exit_code = 1
                        error_message = map_sync_state.get("error") or f"{step} failed"
                        break
                    continue

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
            and step in {"Downloading selected Kiwix content", "Downloading selected extra Kiwix content"}
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

    def read_memory_stats(self) -> dict[str, int | float] | None:
        meminfo = Path("/proc/meminfo")
        if not meminfo.exists():
            return None

        values: dict[str, int] = {}
        try:
            for line in meminfo.read_text().splitlines():
                if ":" not in line:
                    continue
                key, raw_value = line.split(":", 1)
                parts = raw_value.strip().split()
                if not parts:
                    continue
                values[key] = int(parts[0]) * 1024
        except (OSError, ValueError):
            return None

        total = int(values.get("MemTotal", 0))
        available = int(values.get("MemAvailable", values.get("MemFree", 0)))
        if total <= 0:
            return None
        used = max(0, total - available)
        used_percent = max(0, min(100, round((used / total) * 100)))
        return {
            "total_bytes": total,
            "used_bytes": used,
            "available_bytes": available,
            "used_percent": used_percent,
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

    def system_service_definitions(self) -> list[dict[str, str]]:
        return [
            {
                "id": "portal",
                "label": "Portal API",
                "unit": "sopr-portal.service",
                "description": "Admin API and setup workflows.",
            },
            {
                "id": "kiwix",
                "label": "Kiwix",
                "unit": "sopr-kiwix.service",
                "description": "Offline library server.",
            },
            {
                "id": "nginx",
                "label": "Nginx",
                "unit": "nginx.service",
                "description": "Main web front end for SOPR.",
            },
            {
                "id": "kolibri",
                "label": "Kolibri",
                "unit": "kolibri.service",
                "description": "Learning platform service.",
            },
            {
                "id": "access-point",
                "label": "Access Point",
                "unit": "sopr-ap-network.service",
                "description": "Local Wi-Fi hotspot network service.",
            },
        ]

    def inspect_system_service(self, service: dict[str, str]) -> dict[str, str | bool]:
        unit = service["unit"]
        active_state = "unknown"
        enabled_state = "unknown"
        load_state = "unknown"
        sub_state = "unknown"

        try:
            result = subprocess.run(
                ["systemctl", "show", unit, "--property=LoadState,ActiveState,SubState,UnitFileState", "--value"],
                capture_output=True,
                text=True,
                check=False,
            )
        except FileNotFoundError:
            return {
                **service,
                "exists": False,
                "active_state": active_state,
                "enabled_state": enabled_state,
                "load_state": load_state,
                "sub_state": sub_state,
            }

        lines = result.stdout.splitlines()
        if len(lines) >= 4:
            load_state = lines[0].strip() or "unknown"
            active_state = lines[1].strip() or "unknown"
            sub_state = lines[2].strip() or "unknown"
            enabled_state = lines[3].strip() or "unknown"

        exists = load_state not in {"not-found", "unknown", ""}
        return {
            **service,
            "exists": exists,
            "active_state": active_state if exists else "not-installed",
            "enabled_state": enabled_state if exists else "not-installed",
            "load_state": load_state,
            "sub_state": sub_state,
        }

    def system_service_health(self) -> list[dict[str, str | bool]]:
        return [
            self.inspect_system_service(service)
            for service in self.system_service_definitions()
        ]

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

    def restart_system_service(self, service_id: str) -> dict:
        services = {service["id"]: service for service in self.system_service_definitions()}
        service = services.get(service_id)
        if not service:
            raise ValueError("Unknown service.")

        details = self.inspect_system_service(service)
        if not details.get("exists"):
            raise ValueError(f'{service["label"]} is not installed on this device.')

        unit = service["unit"]
        try:
            result = subprocess.run(
                ["systemctl", "restart", unit],
                capture_output=True,
                text=True,
                check=False,
            )
        except FileNotFoundError as exc:
            raise RuntimeError("systemctl is not available on this system") from exc

        if result.returncode != 0:
            message = (result.stderr or result.stdout or "").strip() or f"Unable to restart {service['label']}."
            raise RuntimeError(message)

        return {
            "service": self.inspect_system_service(service),
            "services": self.system_service_health(),
            "message": f'{service["label"]} restarted.',
        }

    def file_manager_roots(self) -> list[dict[str, str]]:
        roots: list[dict[str, str]] = [
            {
                "id": "internal-library",
                "label": "Internal Library",
                "path": str(self.kiwix_library_dir()),
            },
            {
                "id": "internal-maps",
                "label": "Internal Maps",
                "path": str(self.maps_root()),
            },
            {
                "id": "portal-data",
                "label": "SOPR Data",
                "path": str(self.data_dir),
            },
        ]
        seen = {item["path"] for item in roots}
        for volume in self.storage_volumes():
            if not volume.get("mounted"):
                continue
            mountpoint = str(volume.get("mountpoint") or "").strip()
            if not mountpoint:
                continue
            display_name = str(volume.get("label") or volume.get("display_name") or volume.get("name") or "Drive").strip()
            if mountpoint not in seen:
                roots.append(
                    {
                        "id": f"volume-{volume.get('name', display_name)}",
                        "label": f"Drive: {display_name}",
                        "path": mountpoint,
                    }
                )
                seen.add(mountpoint)
            layout = volume.get("sopr_layout") or {}
            for directory_name, label in (
                ("library", f"{display_name} Library"),
                ("maps", f"{display_name} Maps"),
                ("media", f"{display_name} Media"),
            ):
                if not layout.get(directory_name):
                    continue
                path = str(Path(mountpoint) / directory_name)
                if path in seen:
                    continue
                roots.append(
                    {
                        "id": f"{volume.get('name', display_name)}-{directory_name}",
                        "label": label,
                        "path": path,
                    }
                )
                seen.add(path)
        return roots

    def _allowed_file_roots(self) -> list[Path]:
        roots: list[Path] = []
        seen: set[str] = set()
        for item in self.file_manager_roots():
            path = Path(str(item.get("path", ""))).resolve(strict=False)
            key = str(path)
            if key in seen:
                continue
            seen.add(key)
            roots.append(path)
        return roots

    def _resolve_managed_file_path(self, path_value: str, *, allow_missing: bool = False) -> Path:
        candidate = Path(str(path_value or "").strip())
        if not candidate.is_absolute():
            raise ValueError("Choose a valid path.")
        resolved = candidate.resolve(strict=False)
        for root in self._allowed_file_roots():
            root_str = str(root)
            resolved_str = str(resolved)
            if resolved_str == root_str or resolved_str.startswith(root_str.rstrip("/") + "/"):
                if not allow_missing and not resolved.exists():
                    raise ValueError("The selected path is unavailable.")
                return resolved
        raise ValueError("That location is outside the allowed file-manager roots.")

    def _file_manager_parent(self, path: Path) -> str | None:
        parent = path.parent.resolve(strict=False)
        try:
            return str(self._resolve_managed_file_path(str(parent)))
        except ValueError:
            return None

    def _file_manager_entry(self, path: Path) -> dict[str, object]:
        stat = path.stat()
        is_dir = path.is_dir()
        return {
            "name": path.name,
            "path": str(path.resolve(strict=False)),
            "is_dir": is_dir,
            "size_bytes": 0 if is_dir else stat.st_size,
            "size_label": "--" if is_dir else format_size_bytes(stat.st_size),
            "modified_at": time.strftime("%Y-%m-%d %H:%M", time.localtime(stat.st_mtime)),
        }

    def file_manager_list(self, path_value: str | None = None) -> dict:
        roots = self.file_manager_roots()
        default_path = roots[0]["path"] if roots else str(self.repo_root)
        path = self._resolve_managed_file_path(path_value or default_path)
        if not path.exists():
            raise ValueError("The selected path is unavailable.")
        if not path.is_dir():
            raise ValueError("Choose a folder to browse.")

        entries: list[dict[str, object]] = []
        for child in sorted(path.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower())):
            try:
                entries.append(self._file_manager_entry(child))
            except OSError:
                continue

        return {
            "path": str(path),
            "parent_path": self._file_manager_parent(path),
            "entries": entries,
            "roots": roots,
        }

    def file_manager_copy(self, source_path: str, target_dir: str) -> dict:
        source = self._resolve_managed_file_path(source_path)
        destination_dir = self._resolve_managed_file_path(target_dir)
        if not destination_dir.is_dir():
            raise ValueError("Choose a destination folder.")
        destination = destination_dir / source.name
        if destination.exists():
            raise ValueError("A file or folder with that name already exists in the destination.")
        if source.is_dir():
            shutil.copytree(source, destination)
        else:
            shutil.copy2(source, destination)
        return {
            "message": f"Copied {source.name} to {destination_dir}.",
            "source": self.file_manager_list(str(source.parent)),
            "target": self.file_manager_list(str(destination_dir)),
        }

    def file_manager_move(self, source_path: str, target_dir: str) -> dict:
        source = self._resolve_managed_file_path(source_path)
        destination_dir = self._resolve_managed_file_path(target_dir)
        if not destination_dir.is_dir():
            raise ValueError("Choose a destination folder.")
        destination = destination_dir / source.name
        if destination.exists():
            raise ValueError("A file or folder with that name already exists in the destination.")
        shutil.move(str(source), str(destination))
        return {
            "message": f"Moved {source.name} to {destination_dir}.",
            "source": self.file_manager_list(str(source.parent)),
            "target": self.file_manager_list(str(destination_dir)),
        }

    def file_manager_rename(self, source_path: str, new_name: str) -> dict:
        source = self._resolve_managed_file_path(source_path)
        name = str(new_name or "").strip()
        if not name or "/" in name or name in {".", ".."}:
            raise ValueError("Choose a valid new name.")
        destination = source.parent / name
        self._resolve_managed_file_path(str(destination), allow_missing=True)
        if destination.exists():
            raise ValueError("A file or folder with that name already exists.")
        source.rename(destination)
        return {
            "message": f"Renamed to {name}.",
            "pane": self.file_manager_list(str(destination.parent)),
        }

    def file_manager_delete(self, source_path: str) -> dict:
        source = self._resolve_managed_file_path(source_path)
        if str(source) in {str(root) for root in self._allowed_file_roots()}:
            raise ValueError("Cannot delete a protected root folder.")
        parent = source.parent
        if source.is_dir():
            shutil.rmtree(source)
        else:
            source.unlink()
        return {
            "message": f"Deleted {source.name}.",
            "pane": self.file_manager_list(str(parent)),
        }

    def file_manager_mkdir(self, parent_path: str, name: str) -> dict:
        parent = self._resolve_managed_file_path(parent_path)
        if not parent.is_dir():
            raise ValueError("Choose a folder first.")
        folder_name = str(name or "").strip()
        if not folder_name or "/" in folder_name or folder_name in {".", ".."}:
            raise ValueError("Choose a valid folder name.")
        new_dir = parent / folder_name
        self._resolve_managed_file_path(str(new_dir), allow_missing=True)
        if new_dir.exists():
            raise ValueError("A file or folder with that name already exists.")
        new_dir.mkdir(parents=False, exist_ok=False)
        return {
            "message": f"Created folder {folder_name}.",
            "pane": self.file_manager_list(str(parent)),
        }

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
        if path == "/api/files":
            try:
                self.send_json(self.portal_state.file_manager_list(query.get("path", [""])[0]))
            except ValueError as exc:
                self.send_json({"error": str(exc)}, status=400)
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

        if self.path == "/api/content/download-selected":
            try:
                state = self.portal_state.download_selected_extra_zims(payload)
            except ValueError as exc:
                self.send_json({"error": str(exc)}, status=400)
                return
            except RuntimeError as exc:
                self.send_json({"error": str(exc)}, status=409)
                return
            self.send_json(state, status=202)
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

        if self.path == "/api/system/service/restart":
            try:
                state = self.portal_state.restart_system_service(str(payload.get("service", "")).strip())
            except ValueError as exc:
                self.send_json({"error": str(exc)}, status=400)
                return
            except RuntimeError as exc:
                self.send_json({"error": str(exc)}, status=500)
                return
            self.send_json(state)
            return

        if self.path == "/api/files/copy":
            try:
                state = self.portal_state.file_manager_copy(
                    str(payload.get("source_path", "")).strip(),
                    str(payload.get("target_dir", "")).strip(),
                )
            except ValueError as exc:
                self.send_json({"error": str(exc)}, status=400)
                return
            self.send_json(state)
            return

        if self.path == "/api/files/move":
            try:
                state = self.portal_state.file_manager_move(
                    str(payload.get("source_path", "")).strip(),
                    str(payload.get("target_dir", "")).strip(),
                )
            except ValueError as exc:
                self.send_json({"error": str(exc)}, status=400)
                return
            self.send_json(state)
            return

        if self.path == "/api/files/rename":
            try:
                state = self.portal_state.file_manager_rename(
                    str(payload.get("source_path", "")).strip(),
                    str(payload.get("new_name", "")).strip(),
                )
            except ValueError as exc:
                self.send_json({"error": str(exc)}, status=400)
                return
            self.send_json(state)
            return

        if self.path == "/api/files/delete":
            try:
                state = self.portal_state.file_manager_delete(
                    str(payload.get("source_path", "")).strip(),
                )
            except ValueError as exc:
                self.send_json({"error": str(exc)}, status=400)
                return
            self.send_json(state)
            return

        if self.path == "/api/files/mkdir":
            try:
                state = self.portal_state.file_manager_mkdir(
                    str(payload.get("parent_path", "")).strip(),
                    str(payload.get("name", "")).strip(),
                )
            except ValueError as exc:
                self.send_json({"error": str(exc)}, status=400)
                return
            self.send_json(state)
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

        if self.path == "/api/system/storage/mount":
            try:
                state = self.portal_state.mount_storage_volume(payload)
            except ValueError as exc:
                self.send_json({"error": str(exc)}, status=400)
                return
            except RuntimeError as exc:
                self.send_json({"error": str(exc)}, status=500)
                return
            self.send_json(state)
            return

        if self.path == "/api/system/storage/unmount":
            try:
                state = self.portal_state.unmount_storage_volume(payload)
            except ValueError as exc:
                self.send_json({"error": str(exc)}, status=400)
                return
            except RuntimeError as exc:
                self.send_json({"error": str(exc)}, status=500)
                return
            self.send_json(state)
            return

        if self.path == "/api/system/storage/prepare":
            try:
                state = self.portal_state.prepare_storage_volume(payload)
            except ValueError as exc:
                self.send_json({"error": str(exc)}, status=400)
                return
            except RuntimeError as exc:
                self.send_json({"error": str(exc)}, status=500)
                return
            self.send_json(state)
            return

        if self.path == "/api/system/storage/rename":
            try:
                state = self.portal_state.rename_storage_volume(payload)
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
