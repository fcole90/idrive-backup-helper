#!/usr/bin/env python3
"""Migrate pyproject.toml from Poetry to uv, then self-clean."""

import re
import sys
from pathlib import Path
import tomllib

ROOT = Path(__file__).parent.parent


def remove_section(content: str, section_name: str) -> str:
    """Remove a TOML section header and its key-value lines."""
    lines = content.splitlines(keepends=True)
    result: list[str] = []
    skipping = False
    header = f"[{section_name}]"
    for line in lines:
        if line.strip() == header:
            skipping = True
            continue
        if skipping and line.strip().startswith("["):
            skipping = False
        if not skipping:
            result.append(line)
    return "".join(result)


def remove_sections_with_prefix(content: str, section_prefix: str) -> str:
    """Remove a TOML section and any nested subsections sharing its prefix."""
    lines = content.splitlines(keepends=True)
    result: list[str] = []
    skipping = False
    header_prefix = f"[{section_prefix}"

    for line in lines:
        stripped = line.strip()
        if stripped == f"[{section_prefix}]" or stripped.startswith(
            f"{header_prefix}."
        ):
            skipping = True
            continue

        if skipping and stripped.startswith("["):
            skipping = False

        if not skipping:
            result.append(line)

    return "".join(result)


def update_build_system(content: str) -> str:
    """Replace poetry-core build system with hatchling."""
    content = re.sub(
        r'requires\s*=\s*\["poetry-core[^"]*"\]',
        'requires = ["hatchling"]',
        content,
    )
    content = re.sub(
        r'build-backend\s*=\s*"poetry\.core\.masonry\.api"',
        'build-backend = "hatchling.build"',
        content,
    )
    return content


def extract_poetry_packages(content: str) -> list[str]:
    """Extract package paths from the [tool.poetry] packages key."""
    section_match = re.search(
        r"^\[tool\.poetry\]\s*(.*?)(?=^\[|\Z)",
        content,
        flags=re.MULTILINE | re.DOTALL,
    )
    if section_match is None:
        return []

    packages_match = re.search(
        r"packages\s*=\s*\[(.*?)\]",
        section_match.group(1),
        flags=re.DOTALL,
    )
    if packages_match is None:
        return []

    packages: list[str] = []
    for package_match in re.finditer(r"\{(.*?)\}", packages_match.group(1), re.DOTALL):
        package_config = package_match.group(1)
        include_match = re.search(r'include\s*=\s*"([^"]+)"', package_config)
        if include_match is None:
            continue

        from_match = re.search(r'from\s*=\s*"([^"]+)"', package_config)
        package_name = include_match.group(1)
        from_dir = from_match.group(1) if from_match is not None else None
        packages.append(f"{from_dir}/{package_name}" if from_dir else package_name)

    return packages


def insert_hatch_section(content: str, packages: list[str]) -> str:
    """Insert [tool.hatch.build.targets.wheel] after the [build-system] block."""
    if "[tool.hatch.build.targets.wheel]" in content:
        return content
    packages_str = ", ".join(f'"{p}"' for p in packages)
    hatch_block = f"[tool.hatch.build.targets.wheel]\n" f"packages = [{packages_str}]\n"
    lines = content.splitlines(keepends=True)
    result: list[str] = []
    in_build_system = False
    inserted = False
    for line in lines:
        if line.strip() == "[build-system]":
            in_build_system = True
        elif in_build_system and line.strip().startswith("[") and not inserted:
            result.append(hatch_block + "\n")
            in_build_system = False
            inserted = True
        result.append(line)
    if in_build_system and not inserted:
        result.append("\n" + hatch_block)
    return "".join(result)


def add_poethepoet_to_dev(content: str) -> str:
    """Add poethepoet to the [dependency-groups] dev list."""
    if "poethepoet" in content:
        return content
    match = re.search(r"dev\s*=\s*\[(.*?)\]", content, flags=re.DOTALL)
    if not match:
        return content
    items = re.findall(r'"[^"]+"', match.group(1))
    all_items = ['"poethepoet>=0.34.0"'] + items
    new_list = "[\n" + "".join(f"    {item},\n" for item in all_items) + "]"
    return content[: match.start()] + f"dev = {new_list}" + content[match.end() :]


def parse_toml(content: str) -> dict[str, object]:
    """Parse TOML content into a generic mapping."""
    return tomllib.loads(content)


def format_array(items: list[str]) -> str:
    """Format a TOML array in a stable multiline form."""
    if not items:
        return "[]"

    return "[\n" + "".join(f'    "{item}",\n' for item in items) + "]"


def merge_unique(existing: list[str], additional: list[str]) -> list[str]:
    """Merge lists while preserving order and removing duplicates."""
    merged = list(existing)
    for item in additional:
        if item not in merged:
            merged.append(item)
    return merged


def set_key_in_section(
    content: str, section_name: str, key: str, value_repr: str
) -> str:
    """Set or insert a key in a top-level TOML section."""
    section_pattern = re.compile(
        rf"(^\[{re.escape(section_name)}\]\s*\n)(.*?)(?=^\[|\Z)",
        flags=re.MULTILINE | re.DOTALL,
    )
    match = section_pattern.search(content)
    key_line = f"{key} = {value_repr}\n"

    if match is None:
        separator = "\n" if content and not content.endswith("\n\n") else ""
        return content.rstrip() + f"{separator}\n[{section_name}]\n{key_line}"

    section_body = match.group(2)
    key_pattern = re.compile(
        rf"^{re.escape(key)}\s*=\s*(\[(?:.|\n)*?\]|[^\n]+)\n?",
        flags=re.MULTILINE,
    )
    key_match = key_pattern.search(section_body)

    if key_match is None:
        section_body = (
            section_body.rstrip() + ("\n" if section_body.strip() else "") + key_line
        )
    else:
        section_body = (
            section_body[: key_match.start()]
            + key_line
            + section_body[key_match.end() :]
        )

    return content[: match.start(2)] + section_body + content[match.end(2) :]


def constraint_to_upper_bound(
    version: str,
    index_to_increment: int,
    total_length: int | None = None,
) -> str:
    """Build an upper bound by incrementing a version component and zeroing the rest."""
    parts = [int(part) for part in version.split(".") if part]
    target_length = total_length if total_length is not None else len(parts)
    while len(parts) <= max(index_to_increment, target_length - 1):
        parts.append(0)
    parts[index_to_increment] += 1
    for index in range(index_to_increment + 1, len(parts)):
        parts[index] = 0
    return ".".join(str(part) for part in parts[:target_length])


def convert_constraint_token(token: str) -> list[str]:
    """Convert a Poetry constraint token into PEP 508-compatible specifiers."""
    stripped = token.strip()
    if not stripped or stripped == "*":
        return []

    if stripped.startswith("^"):
        base = stripped[1:]
        parts = [int(part) for part in base.split(".") if part]
        if not parts:
            return []
        if parts[0] != 0:
            upper_index = 0
        elif len(parts) > 1 and parts[1] != 0:
            upper_index = 1
        else:
            upper_index = min(2, len(parts) - 1)
        return [f">={base}", f"<{constraint_to_upper_bound(base, upper_index)}"]

    if stripped.startswith("~") and not stripped.startswith("~="):
        base = stripped[1:]
        parts = [int(part) for part in base.split(".") if part]
        if not parts:
            return []
        upper_index = 0 if len(parts) == 1 else 1
        return [f">={base}", f"<{constraint_to_upper_bound(base, upper_index)}"]

    if "*" in stripped:
        raw_parts = stripped.split(".")
        non_wildcard_parts = [part for part in raw_parts if part != "*"]
        lower = ".".join(
            non_wildcard_parts + ["0"] * (len(raw_parts) - len(non_wildcard_parts))
        )
        upper_index = max(0, len(non_wildcard_parts) - 1)
        upper = constraint_to_upper_bound(
            ".".join(non_wildcard_parts or ["0"]),
            upper_index,
            total_length=len(raw_parts),
        )
        return [f">={lower}", f"<{upper}"]

    if stripped[0] in "<>=!":
        return [stripped]

    return [f"=={stripped}"]


def convert_version_constraint(constraint: str) -> str:
    """Convert Poetry version constraints to a PEP 508-compatible specifier string."""
    tokens = [
        converted
        for token in constraint.split(",")
        for converted in convert_constraint_token(token)
    ]
    return ",".join(tokens)


def build_dependency_requirement(name: str, specifier: str) -> str:
    """Build a dependency requirement string from a name and optional specifier."""
    return name if not specifier else f"{name}{specifier}"


def convert_python_constraint_to_marker(constraint: str) -> str:
    """Convert a Python version constraint to a simple marker expression."""
    specifier = convert_version_constraint(constraint)
    if not specifier:
        return ""

    marker_parts: list[str] = []
    for token in specifier.split(","):
        operator_match = re.match(r"(<=|>=|==|!=|<|>)(.+)", token)
        if operator_match is None:
            continue
        operator, version = operator_match.groups()
        marker_parts.append(f'python_version {operator} "{version}"')
    return " and ".join(marker_parts)


def convert_dependency_entry(name: str, value: object) -> str | None:
    """Convert a Poetry dependency entry to a PEP 508 requirement string."""
    if isinstance(value, str):
        return build_dependency_requirement(name, convert_version_constraint(value))

    if not isinstance(value, dict):
        return None

    version = value.get("version", "*")
    if not isinstance(version, str):
        return None

    extras = value.get("extras", [])
    extras_suffix = ""
    if isinstance(extras, list) and all(isinstance(extra, str) for extra in extras):
        extras_suffix = f"[{','.join(extras)}]" if extras else ""

    dependency = build_dependency_requirement(
        f"{name}{extras_suffix}",
        convert_version_constraint(version),
    )

    markers: list[str] = []
    python_constraint = value.get("python")
    if isinstance(python_constraint, str):
        python_marker = convert_python_constraint_to_marker(python_constraint)
        if python_marker:
            markers.append(python_marker)

    explicit_markers = value.get("markers")
    if isinstance(explicit_markers, str) and explicit_markers:
        markers.append(explicit_markers)

    if markers:
        dependency += f" ; {' and '.join(markers)}"

    return dependency


def extract_poetry_dependency_data(
    content: str,
) -> tuple[list[str], dict[str, list[str]], str | None]:
    """Extract Poetry dependencies, groups, and python constraint from TOML content."""
    data = parse_toml(content)
    tool_data = data.get("tool", {})
    if not isinstance(tool_data, dict):
        return [], {}, None

    poetry_data = tool_data.get("poetry", {})
    if not isinstance(poetry_data, dict):
        return [], {}, None

    project_dependencies: list[str] = []
    dependency_groups: dict[str, list[str]] = {}
    requires_python: str | None = None

    dependencies = poetry_data.get("dependencies", {})
    if isinstance(dependencies, dict):
        for name, value in dependencies.items():
            if name == "python" and isinstance(value, str):
                requires_python = convert_version_constraint(value)
                continue
            if not isinstance(name, str):
                continue
            dependency = convert_dependency_entry(name, value)
            if dependency is not None:
                project_dependencies.append(dependency)

    legacy_dev_dependencies = poetry_data.get("dev-dependencies", {})
    if isinstance(legacy_dev_dependencies, dict):
        dependency_groups["dev"] = [
            dependency
            for name, value in legacy_dev_dependencies.items()
            if isinstance(name, str)
            for dependency in [convert_dependency_entry(name, value)]
            if dependency is not None
        ]

    group_data = poetry_data.get("group", {})
    if isinstance(group_data, dict):
        for group_name, group_config in group_data.items():
            if not isinstance(group_name, str) or not isinstance(group_config, dict):
                continue
            group_dependencies = group_config.get("dependencies", {})
            if not isinstance(group_dependencies, dict):
                continue
            converted = [
                dependency
                for name, value in group_dependencies.items()
                if isinstance(name, str)
                for dependency in [convert_dependency_entry(name, value)]
                if dependency is not None
            ]
            dependency_groups[group_name] = merge_unique(
                dependency_groups.get(group_name, []),
                converted,
            )

    return project_dependencies, dependency_groups, requires_python


def apply_poetry_dependency_migration(content: str) -> str:
    """Migrate Poetry dependency declarations into project and dependency-groups sections."""
    data = parse_toml(content)
    project_data = data.get("project", {})
    existing_dependencies = (
        list(project_data.get("dependencies", []))
        if isinstance(project_data, dict)
        and isinstance(project_data.get("dependencies", []), list)
        else []
    )
    existing_requires_python = (
        project_data.get("requires-python")
        if isinstance(project_data, dict)
        and isinstance(project_data.get("requires-python"), str)
        else None
    )

    dependency_group_data = data.get("dependency-groups", {})
    existing_groups = (
        {
            key: list(value)
            for key, value in dependency_group_data.items()
            if isinstance(key, str)
            and isinstance(value, list)
            and all(isinstance(item, str) for item in value)
        }
        if isinstance(dependency_group_data, dict)
        else {}
    )

    migrated_dependencies, migrated_groups, migrated_requires_python = (
        extract_poetry_dependency_data(content)
    )

    merged_dependencies = merge_unique(existing_dependencies, migrated_dependencies)
    content = set_key_in_section(
        content, "project", "dependencies", format_array(merged_dependencies)
    )

    if not existing_requires_python and migrated_requires_python:
        content = set_key_in_section(
            content,
            "project",
            "requires-python",
            f'"{migrated_requires_python}"',
        )

    for group_name in sorted(set(existing_groups) | set(migrated_groups)):
        merged_group = merge_unique(
            existing_groups.get(group_name, []),
            migrated_groups.get(group_name, []),
        )
        content = set_key_in_section(
            content,
            "dependency-groups",
            group_name,
            format_array(merged_group),
        )

    return content


def remove_script_key(content: str, key: str) -> str:
    """Remove a single key-value line from [project.scripts]."""
    lines = content.splitlines(keepends=True)
    result: list[str] = []
    in_scripts = False
    for line in lines:
        if line.strip() == "[project.scripts]":
            in_scripts = True
        elif in_scripts and line.strip().startswith("["):
            in_scripts = False
        if in_scripts and line.strip().startswith(f"{key}"):
            continue
        result.append(line)
    return "".join(result)


def delete_if_exists(path: Path) -> None:
    if path.exists():
        path.unlink()
        print(f"Deleted {path.name}")


def migrate_pyproject_content(content: str) -> str:
    """Perform the migration transforms on a pyproject.toml content string and return the new content."""
    packages = extract_poetry_packages(content)

    content = apply_poetry_dependency_migration(content)
    content = update_build_system(content)
    if packages:
        content = insert_hatch_section(content, packages)
    content = remove_sections_with_prefix(content, "tool.poetry")
    content = add_poethepoet_to_dev(content)
    content = remove_script_key(content, "transition-to-uv")

    return content


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint. Defaults to operating on `pyproject.toml` in the repo root.

    Args:
        argv: Optional list of command-line arguments for testing.

    Returns:
        Exit code (0 on success).
    """
    from argparse import ArgumentParser

    parser = ArgumentParser(description="Migrate pyproject.toml from Poetry to uv.")
    parser.add_argument(
        "--input",
        "-i",
        default=str(ROOT / "pyproject.toml"),
        help="Path to the input pyproject.toml",
    )
    parser.add_argument(
        "--output",
        "-o",
        default=None,
        help="Path to write the migrated pyproject.toml (defaults to input path)",
    )
    parser.add_argument(
        "--no-delete",
        action="store_true",
        help="Don't delete poetry.lock or poetry.toml (useful for tests)",
    )

    args = parser.parse_args(argv)

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"Error: {input_path} not found", file=sys.stderr)
        return 1

    content = input_path.read_text(encoding="utf-8")
    new_content = migrate_pyproject_content(content)

    output_path = Path(args.output) if args.output else input_path
    output_path.write_text(new_content, encoding="utf-8")
    print(f"{output_path.name} migrated to uv.")

    if not args.no_delete:
        delete_if_exists(ROOT / "poetry.lock")
        delete_if_exists(ROOT / "poetry.toml")

    print("Update complete. ⚠️ Please review changes.")
    print(
        "Next steps:\n"
        "1. Remove .venv and this migration script.\n"
        "2. Run 'uv sync' to install dependencies and generate the new lockfile.\n"
        "3. Review changes, test your project to ensure everything works as expected.\n"
        "4. Commit the changes with a message like 'chore: Migrate from Poetry to uv'."
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
