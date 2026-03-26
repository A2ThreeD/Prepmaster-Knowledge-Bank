#!/usr/bin/env python3

import argparse
import json
import os
import shutil
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


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
        self.apply_lock = threading.Lock()
        self.apply_thread: threading.Thread | None = None

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

        state = {
            "setup_complete": setup_complete,
            "last_saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        self.state_file.write_text(json.dumps(state, indent=2) + "\n")
        return self.load_state()

    def status(self) -> dict:
        total, used, free = shutil.disk_usage("/")
        return {
            "disk": {
                "total_gb": round(total / (1024 ** 3), 1),
                "used_gb": round(used / (1024 ** 3), 1),
                "free_gb": round(free / (1024 ** 3), 1),
            },
            "temperature_c": self.read_temperature(),
            "uptime": self.read_uptime(),
            "kiwix_url": f"http://{self.detect_primary_host()}:{read_env_file(self.prepmaster_env).get('KIWIX_PORT', '8080')}/",
            "content_mode": read_env_file(self.prepmaster_env).get(
                "PREPMASTER_ZIM_MODE", "full"
            ),
            "services": {
                "portal": self.read_service_status("prepmaster-portal.service"),
                "kiwix": self.read_service_status("prepmaster-kiwix.service"),
                "hostapd": self.read_service_status("hostapd.service"),
                "dnsmasq": self.read_service_status("dnsmasq.service"),
                "nginx": self.read_service_status("nginx.service"),
            },
        }

    def load_apply_state(self) -> dict:
        state = read_json(
            self.apply_state_file,
            {
                "status": "idle",
                "action": "full",
                "started_at": None,
                "finished_at": None,
                "step": None,
                "exit_code": None,
                "error": None,
            },
        )
        state["log_tail"] = self.read_log_tail()
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

    def start_apply(self, action: str = "full") -> dict:
        if action not in {"full", "refresh-content", "rebuild-library"}:
            raise ValueError("Invalid apply action")

        with self.apply_lock:
            state = self.load_apply_state()
            if state.get("status") == "running":
                raise RuntimeError("Configuration apply is already running")

            self.apply_log_file.write_text("")
            self.save_apply_state(
                {
                    "status": "running",
                    "action": action,
                    "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "finished_at": None,
                    "step": "Starting apply workflow",
                    "exit_code": None,
                    "error": None,
                }
            )
            self.apply_thread = threading.Thread(
                target=self.run_apply_workflow,
                args=(action,),
                daemon=True,
            )
            self.apply_thread.start()
            return self.load_apply_state()

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
            for step, command in commands:
                self.save_apply_state({"step": step})
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
                "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "exit_code": exit_code,
                "error": error_message,
                "step": None if exit_code == 0 else step,
            }
        )

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

    def read_service_status(self, service: str) -> str:
        try:
            result = subprocess.run(
                ["systemctl", "is-active", service],
                capture_output=True,
                text=True,
                check=False,
            )
        except FileNotFoundError:
            return "unknown"
        status = result.stdout.strip()
        return status or "unknown"

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


class PortalHandler(BaseHTTPRequestHandler):
    portal_state: PortalState

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_common_headers()
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self) -> None:
        if self.path == "/api/state":
            self.send_json(self.portal_state.load_state())
            return
        if self.path == "/api/status":
            self.send_json(self.portal_state.status())
            return
        if self.path == "/api/apply":
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
