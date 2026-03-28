#!/usr/bin/env python3

import argparse
import json
import sys
from pathlib import Path


LEVEL_ORDER = {
    "essential": 1,
    "standard": 2,
    "comprehensive": 3,
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a Kiwix ZIM download manifest from kiwix-categories.json."
    )
    parser.add_argument(
        "--source",
        default="catalog/kiwix-categories.json",
        help="Path to the Project NOMAD categories JSON file.",
    )
    parser.add_argument(
        "--output",
        default="config/kiwix-zim-urls.txt",
        help="Path to write the generated manifest.",
    )
    parser.add_argument(
        "--profile",
        choices=("essential", "standard", "comprehensive"),
        default="essential",
        help="Tier depth to include for each category.",
    )
    parser.add_argument(
        "--categories",
        nargs="*",
        help="Optional category slugs to include. Default is all categories in the JSON file.",
    )
    parser.add_argument(
        "--wikipedia-options",
        default="catalog/wikipedia.json",
        help="Path to the Wikipedia options JSON file.",
    )
    parser.add_argument(
        "--wikipedia-choice",
        default="top-mini",
        help="Wikipedia option id to append to the manifest. Use 'none' to skip.",
    )
    return parser.parse_args()


def load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        print(f"Missing source file: {path}", file=sys.stderr)
        sys.exit(1)
    except json.JSONDecodeError as exc:
        print(f"Invalid JSON in {path}: {exc}", file=sys.stderr)
        sys.exit(1)


def tier_level(tier_slug: str) -> int:
    for label, value in LEVEL_ORDER.items():
        if tier_slug.endswith(f"-{label}"):
            return value
    raise ValueError(f"Unrecognized tier slug: {tier_slug}")


def collect_resources(document: dict, profile: str, categories: set[str] | None) -> list[dict]:
    selected_level = LEVEL_ORDER[profile]
    resources: list[dict] = []
    seen_ids: set[str] = set()

    collection_key = "collections" if "collections" in document else "categories"
    loadout_key = "loadouts" if collection_key == "collections" else "tiers"
    resource_key = "library_items" if collection_key == "collections" else "resources"

    for category in document.get(collection_key, []):
        category_slug = category.get("key") if collection_key == "collections" else category.get("slug")
        if categories and category_slug not in categories:
            continue

        for tier in category.get(loadout_key, []):
            try:
                level = tier_level(tier["key"] if loadout_key == "loadouts" else tier["slug"])
            except (KeyError, ValueError) as exc:
                print(str(exc), file=sys.stderr)
                sys.exit(1)

            if level > selected_level:
                continue

            for resource in tier.get(resource_key, []):
                resource_id = resource.get("key") if resource_key == "library_items" else resource.get("id")
                url = resource.get("download_url") if resource_key == "library_items" else resource.get("url")
                if not resource_id or not url:
                    continue
                if resource_id in seen_ids:
                    continue

                seen_ids.add(resource_id)
                resources.append(
                    {
                        "category": (
                            category.get("label", category_slug)
                            if collection_key == "collections"
                            else category.get("name", category_slug)
                        ),
                        "tier": (
                            tier.get("label", tier.get("key", "unknown"))
                            if loadout_key == "loadouts"
                            else tier.get("name", tier.get("slug", "unknown"))
                        ),
                        "title": (
                            resource.get("label", resource_id)
                            if resource_key == "library_items"
                            else resource.get("title", resource_id)
                        ),
                        "url": url,
                        "size_mb": (
                            resource.get("footprint_mb")
                            if resource_key == "library_items"
                            else resource.get("size_mb")
                        ),
                    }
                )

    return resources


def collect_wikipedia_resource(document: dict, choice: str) -> dict | None:
    if choice == "none":
        return None

    option_key = "library_choices" if "library_choices" in document else "options"
    for option in document.get(option_key, []):
        if option.get("id") == choice:
            return {
                "category": "Wikipedia",
                "tier": "Selected Option",
                "title": option.get("name", option.get("label", choice)),
                "url": option.get("url", option.get("download_url")),
                "size_mb": option.get("size_mb"),
            }

    print(f"Unknown Wikipedia option: {choice}", file=sys.stderr)
    sys.exit(1)


def write_manifest(path: Path, resources: list[dict], profile: str, source: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"# Generated from {source}",
        f"# Profile: {profile}",
        f"# Resource count: {len(resources)}",
        "",
    ]

    current_category = None
    for resource in resources:
        if resource["category"] != current_category:
            current_category = resource["category"]
            lines.append(f"# {current_category}")
        size_suffix = ""
        if resource["size_mb"] is not None:
            size_suffix = f" | {resource['size_mb']} MB"
        lines.append(f"# {resource['tier']} | {resource['title']}{size_suffix}")
        lines.append(resource["url"])
        lines.append("")

    path.write_text("\n".join(lines).rstrip() + "\n")


def main() -> None:
    args = parse_args()
    source = Path(args.source)
    output = Path(args.output)
    categories = set(args.categories) if args.categories else None

    document = load_json(source)
    resources = collect_resources(document, args.profile, categories)
    wikipedia_document = load_json(Path(args.wikipedia_options))
    wikipedia_resource = collect_wikipedia_resource(
        wikipedia_document, args.wikipedia_choice
    )
    if wikipedia_resource is not None:
        resources.append(wikipedia_resource)
    write_manifest(output, resources, args.profile, source)

    print(f"Wrote {len(resources)} URLs to {output}")


if __name__ == "__main__":
    main()
