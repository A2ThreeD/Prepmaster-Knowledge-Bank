#!/usr/bin/env python3

import argparse
import json
import re
import sys
import yaml
from html.parser import HTMLParser
from pathlib import Path
from urllib import request


DEFAULT_SOURCE_URL = "https://download.kiwix.org/zim/wikipedia/"
DEFAULT_OUTPUT = "catalog/wikipedia.yaml"
SPEC_VERSION = "2026-03-27"
ENGLISH_PREFIX = "wikipedia_en_"
VARIANT_SUFFIXES = {"maxi", "mini", "nopic"}
DEFAULT_OPTION_IDS = [
    "top-mini",
    "top-nopic",
    "simple-all-mini",
    "all-mini",
    "all-maxi",
]


class DirectoryIndexParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.entries: list[dict[str, str]] = []
        self._href: str | None = None
        self._anchor_text: list[str] = []
        self._capturing = False
        self._last_entry: dict[str, str] | None = None
        self._tail_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        href = dict(attrs).get("href")
        if href:
            self._capturing = True
            self._href = href
            self._anchor_text = []

    def handle_endtag(self, tag: str) -> None:
        if tag != "a" or not self._capturing or not self._href:
            return
        self._capturing = False
        name = "".join(self._anchor_text).strip() or self._href
        entry = {
            "href": self._href,
            "name": name,
            "size_label": "",
        }
        self.entries.append(entry)
        self._last_entry = entry
        self._tail_text = []
        self._href = None
        self._anchor_text = []

    def handle_data(self, data: str) -> None:
        if self._capturing:
            self._anchor_text.append(data)
            return
        if not self._last_entry:
            return
        self._tail_text.append(data)
        tail = "".join(self._tail_text).replace("\xa0", " ")
        match = re.search(r"\b(\d+(?:\.\d+)?[KMGTP])\s*$", tail.strip())
        if match:
            self._last_entry["size_label"] = match.group(1)
            self._last_entry = None
            self._tail_text = []


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build the SOPR Wikipedia options catalog from the live English Kiwix Wikipedia index."
    )
    parser.add_argument(
        "--source-url",
        default=DEFAULT_SOURCE_URL,
        help="Wikipedia directory index URL to query.",
    )
    parser.add_argument(
        "--input-html",
        help="Optional local HTML file to parse instead of fetching the live directory index.",
    )
    parser.add_argument(
        "--output",
        default=DEFAULT_OUTPUT,
        help="Path to write the generated Wikipedia options YAML or JSON.",
    )
    parser.add_argument(
        "--all-options",
        action="store_true",
        help="Write every latest English Wikipedia variant instead of the curated shortlist.",
    )
    return parser.parse_args()


def read_html(args: argparse.Namespace) -> str:
    if args.input_html:
        return Path(args.input_html).read_text(errors="replace")

    req = request.Request(
        args.source_url,
        headers={"User-Agent": "SOPR-Wikipedia-Builder"},
    )
    with request.urlopen(req, timeout=60) as response:
        return response.read().decode("utf-8", errors="replace")


def parse_size_label_to_mb(label: str) -> int | None:
    match = re.fullmatch(r"(\d+(?:\.\d+)?)([KMGTP])", label)
    if not match:
        return None
    value = float(match.group(1))
    unit = match.group(2)
    factor = {
        "K": 1 / 1024,
        "M": 1,
        "G": 1024,
        "T": 1024 * 1024,
        "P": 1024 * 1024 * 1024,
    }[unit]
    return int(round(value * factor))


def latest_english_wikipedia_entries(html: str) -> list[dict[str, str]]:
    parser = DirectoryIndexParser()
    parser.feed(html)

    latest: dict[str, dict[str, str]] = {}
    for entry in parser.entries:
        name = entry["name"]
        if not name.startswith(ENGLISH_PREFIX) or not name.endswith(".zim"):
            continue
        match = re.fullmatch(rf"({ENGLISH_PREFIX}.+?)_(\d{{4}}-\d{{2}})\.zim", name)
        if not match:
            continue
        stem, version = match.groups()
        current = {
            "name": name,
            "stem": stem,
            "version": version,
            "size_label": entry.get("size_label", ""),
        }
        previous = latest.get(stem)
        if previous is None or version > previous["version"]:
            latest[stem] = current

    return [latest[key] for key in sorted(latest)]


def pretty_scope_name(scope: str) -> str:
    special = {
        "wp1-0.8": "WP1 0.8",
        "top1m": "Top 1M",
        "molcell": "Molecular and Cell Biology",
        "ray-charles": "Ray Charles",
        "climate-change": "Climate Change",
        "ice-hockey": "Ice Hockey",
        "indian-cinema": "Indian Cinema",
        "simple-all": "Simple English Wikipedia",
    }
    if scope in special:
        return special[scope]
    return scope.replace("-", " ").title()


def option_name(scope: str, variant: str | None) -> str:
    if scope == "all" and variant == "maxi":
        return "Complete Wikipedia (Maxi)"
    if scope == "all" and variant == "mini":
        return "Complete Wikipedia (Mini)"
    if scope == "all" and variant == "nopic":
        return "Complete Wikipedia (No Pictures)"
    if scope == "top" and variant == "mini":
        return "Quick Reference"
    if scope == "top":
        suffix = "No Pictures" if variant == "nopic" else variant.title()
        return f"Top Articles ({suffix})"

    scope_name = pretty_scope_name(scope)
    if not variant:
        return scope_name
    suffix = "No Pictures" if variant == "nopic" else variant.title()
    return f"{scope_name} ({suffix})"


def option_description(scope: str, variant: str | None) -> str:
    scope_text = {
        "all": "Full English Wikipedia",
        "top": "Top English Wikipedia article selection",
        "100": "100 featured English Wikipedia articles",
        "simple-all": "Simple English Wikipedia",
    }.get(scope, pretty_scope_name(scope))

    if variant == "maxi":
        return f"{scope_text} with images and media where available."
    if variant == "mini":
        return f"{scope_text} in a smaller condensed format."
    if variant == "nopic":
        return f"{scope_text} without images to save storage."
    return f"{scope_text} reference set."


def build_options(
    entries: list[dict[str, str]],
    source_url: str,
    *,
    include_all: bool,
) -> dict:
    options = []
    for entry in entries:
        stem = entry["stem"][len(ENGLISH_PREFIX):]
        stem_id = stem.replace("_", "-")
        if not include_all and stem_id not in DEFAULT_OPTION_IDS:
            continue
        parts = stem.split("_")
        variant = parts[-1] if parts[-1] in VARIANT_SUFFIXES else None
        scope = "-".join(parts[:-1]) if variant else "-".join(parts)
        size_label = entry["size_label"]
        options.append(
            {
                "id": stem_id,
                "name": option_name(scope, variant),
                "description": option_description(scope, variant),
                "size_mb": parse_size_label_to_mb(size_label),
                "size_label": size_label,
                "url": f"{source_url.rstrip('/')}/{entry['name']}",
                "version": entry["version"],
            }
        )

    return {
        "spec_version": SPEC_VERSION,
        "source_url": source_url,
        "selection_mode": "all" if include_all else "curated",
        "option_count": len(options),
        "options": options,
    }


def main() -> None:
    args = parse_args()
    try:
        html = read_html(args)
    except OSError as exc:
        print(f"Unable to read Wikipedia index: {exc}", file=sys.stderr)
        sys.exit(1)

    payload = build_options(
        latest_english_wikipedia_entries(html),
        args.source_url,
        include_all=args.all_options,
    )
    output_path = Path(args.output)
    if output_path.suffix == ".json":
        output_path.write_text(json.dumps(payload, indent=2) + "\n")
    else:
        output_path.write_text(yaml.safe_dump(payload, sort_keys=False, allow_unicode=False, width=1000))
    print(f"Wrote {payload['option_count']} English Wikipedia options to {output_path}")


if __name__ == "__main__":
    main()
