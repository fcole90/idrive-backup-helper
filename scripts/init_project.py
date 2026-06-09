#!/usr/bin/env python3
"""Initialize the template project with a validated project name."""

import argparse
from collections.abc import Callable, Sequence
from dataclasses import dataclass
import keyword
from pathlib import Path
import re
import subprocess
import sys
import tomllib

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_PACKAGE_NAME = "my_project"
VALID_PROJECT_NAME_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")
VALID_EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@dataclass(frozen=True)
class AuthorDetails:
    """Author metadata to write into pyproject.toml."""

    name: str
    email: str


@dataclass(frozen=True)
class AuthorPromptDefaults:
    """Suggested author values for the interactive prompt."""

    name: str | None
    email: str | None


def project_name_to_package_name(project_name: str) -> str:
    """Convert a distribution name into a Python package name."""
    return project_name.replace("-", "_")


def normalize_project_name(project_name: str) -> str:
    """Normalize and validate a user-provided project name."""
    normalized_name = project_name.strip().lower()
    if not normalized_name:
        raise ValueError("Project name cannot be empty.")

    if VALID_PROJECT_NAME_PATTERN.fullmatch(normalized_name) is None:
        raise ValueError(
            "Project name must start with a letter and contain only letters, digits, hyphens, or underscores."
        )

    package_name = project_name_to_package_name(normalized_name)
    if not package_name.isidentifier() or keyword.iskeyword(package_name):
        raise ValueError("Project name must map to a valid Python package name.")

    return normalized_name


def normalize_author_name(author_name: str) -> str:
    """Normalize and validate an author full name."""
    normalized_name = " ".join(author_name.split())
    if not normalized_name:
        raise ValueError("Author name cannot be empty.")

    if len(normalized_name.split(" ")) < 2:
        raise ValueError("Author name must include at least a name and surname.")

    return normalized_name


def normalize_author_email(author_email: str) -> str:
    """Normalize and validate an author email."""
    normalized_email = author_email.strip()
    if not normalized_email:
        raise ValueError("Author email cannot be empty.")

    if VALID_EMAIL_PATTERN.fullmatch(normalized_email) is None:
        raise ValueError("Author email must be a valid email address.")

    return normalized_email


def get_git_config_value(key: str) -> str | None:
    """Read a git configuration value if available."""
    try:
        result = subprocess.run(
            ["git", "config", "--get", key],
            capture_output=True,
            check=False,
            cwd=ROOT,
            text=True,
        )
    except OSError:
        return None

    value = result.stdout.strip()
    return value or None


def get_git_author_defaults() -> AuthorPromptDefaults:
    """Read suggested author details from local git configuration."""
    return AuthorPromptDefaults(
        name=get_git_config_value("user.name"),
        email=get_git_config_value("user.email"),
    )


def prompt_for_yes_no(
    question: str,
    default: bool,
    input_func: Callable[[str], str] = input,
    output_func: Callable[[str], None] = print,
) -> bool:
    """Prompt until the user provides a yes/no answer."""
    prompt_suffix = "[Y/n]" if default else "[y/N]"
    while True:
        answer = input_func(f"{question} {prompt_suffix}: ").strip().lower()
        if not answer:
            return default
        if answer in {"y", "yes"}:
            return True
        if answer in {"n", "no"}:
            return False

        output_func("Please answer yes or no.")


def prompt_for_validated_value(
    prompt_label: str,
    validator: Callable[[str], str],
    suggested_value: str | None = None,
    input_func: Callable[[str], str] = input,
    output_func: Callable[[str], None] = print,
) -> str:
    """Prompt until the user provides a valid value or accepts a suggestion."""
    suggestion_suffix = f" [{suggested_value}]" if suggested_value is not None else ""
    prompt = f"{prompt_label}{suggestion_suffix}: "

    while True:
        raw_value = input_func(prompt)
        candidate_value = raw_value.strip() or suggested_value or raw_value
        try:
            return validator(candidate_value)
        except ValueError as exc:
            output_func(f"Invalid {prompt_label.lower()}: {exc}")


def prompt_for_author_details(
    defaults: AuthorPromptDefaults,
    input_func: Callable[[str], str] = input,
    output_func: Callable[[str], None] = print,
) -> AuthorDetails | None:
    """Prompt for optional author metadata."""
    should_set_author = prompt_for_yes_no(
        "Set author name and email in pyproject.toml?",
        default=True,
        input_func=input_func,
        output_func=output_func,
    )
    if not should_set_author:
        return None

    author_name = prompt_for_validated_value(
        "Author name and surname",
        normalize_author_name,
        suggested_value=defaults.name,
        input_func=input_func,
        output_func=output_func,
    )
    author_email = prompt_for_validated_value(
        "Author email",
        normalize_author_email,
        suggested_value=defaults.email,
        input_func=input_func,
        output_func=output_func,
    )
    return AuthorDetails(name=author_name, email=author_email)


def prompt_for_project_name(
    input_func: Callable[[str], str] = input,
    output_func: Callable[[str], None] = print,
) -> str:
    """Prompt until the user provides a valid project name."""
    while True:
        try:
            return normalize_project_name(input_func("Project name: "))
        except ValueError as exc:
            output_func(f"Invalid project name: {exc}")


def parse_toml(content: str) -> dict[str, object]:
    """Parse TOML content into a generic mapping."""
    return tomllib.loads(content)


def format_array(items: list[str]) -> str:
    """Format a TOML array in a stable multiline form."""
    if not items:
        return "[]"

    return "[\n" + "".join(f'    "{item}",\n' for item in items) + "]"


def format_toml_basic_string(value: str) -> str:
    """Format a TOML basic string with minimal escaping."""
    escaped_value = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped_value}"'


def format_authors(authors: list[AuthorDetails]) -> str:
    """Format project authors as a TOML inline-table array."""
    formatted_authors = ", ".join(
        "{ "
        f"name = {format_toml_basic_string(author.name)}, "
        f"email = {format_toml_basic_string(author.email)}"
        " }"
        for author in authors
    )
    return f"[{formatted_authors}]"


def set_key_in_section(
    content: str, section_name: str, key: str, value_repr: str
) -> str:
    """Set or insert a key in a TOML section."""
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


def get_project_scripts(content: str) -> dict[str, str]:
    """Read project scripts from pyproject content."""
    project_config = parse_toml(content).get("project")
    if not isinstance(project_config, dict):
        return {}

    scripts = project_config.get("scripts")
    if not isinstance(scripts, dict):
        return {}

    result: dict[str, str] = {}
    for key, value in scripts.items():
        if isinstance(key, str) and isinstance(value, str):
            result[key] = value
    return result


def get_wheel_packages(content: str) -> list[str]:
    """Read wheel packages from pyproject content."""
    tool_config = parse_toml(content).get("tool")
    if not isinstance(tool_config, dict):
        return []

    hatch_config = tool_config.get("hatch")
    if not isinstance(hatch_config, dict):
        return []

    build_config = hatch_config.get("build")
    if not isinstance(build_config, dict):
        return []

    targets_config = build_config.get("targets")
    if not isinstance(targets_config, dict):
        return []

    wheel_config = targets_config.get("wheel")
    if not isinstance(wheel_config, dict):
        return []

    packages = wheel_config.get("packages")
    if not isinstance(packages, list):
        return []

    return [package for package in packages if isinstance(package, str)]


def update_project_scripts(
    content: str, old_package_name: str, new_package_name: str
) -> str:
    """Rewrite project script import targets that point at the package."""
    updated_content = content
    for script_name, script_target in get_project_scripts(content).items():
        if script_target == old_package_name or script_target.startswith(
            (f"{old_package_name}.", f"{old_package_name}:")
        ):
            new_target = f"{new_package_name}{script_target[len(old_package_name):]}"
            updated_content = set_key_in_section(
                updated_content,
                "project.scripts",
                script_name,
                f'"{new_target}"',
            )

    return updated_content


def update_pyproject_content(
    content: str,
    old_package_name: str,
    project_name: str,
    author_details: AuthorDetails | None = None,
) -> str:
    """Update pyproject metadata for the chosen project name."""
    normalized_project_name = normalize_project_name(project_name)
    new_package_name = project_name_to_package_name(normalized_project_name)

    updated_content = set_key_in_section(
        content,
        "project",
        "name",
        f'"{normalized_project_name}"',
    )

    if author_details is not None:
        updated_content = set_key_in_section(
            updated_content,
            "project",
            "authors",
            format_authors([author_details]),
        )

    existing_packages = get_wheel_packages(updated_content)
    updated_packages = [
        package.replace(old_package_name, new_package_name)
        for package in existing_packages
    ]
    source_package = f"src/{new_package_name}"
    if source_package not in updated_packages:
        updated_packages.append(source_package)

    updated_content = set_key_in_section(
        updated_content,
        "tool.hatch.build.targets.wheel",
        "packages",
        format_array(updated_packages),
    )
    return update_project_scripts(
        updated_content,
        old_package_name,
        new_package_name,
    )


def rename_package_directory(
    root: Path, old_package_name: str, new_package_name: str
) -> Path:
    """Rename the package directory under src/."""
    src_dir = root / "src"
    old_package_dir = src_dir / old_package_name
    new_package_dir = src_dir / new_package_name

    if not old_package_dir.exists():
        raise FileNotFoundError(
            f"Template package directory not found: {old_package_dir}"
        )

    if old_package_name == new_package_name:
        return old_package_dir

    if new_package_dir.exists():
        raise FileExistsError(
            f"Target package directory already exists: {new_package_dir}"
        )

    old_package_dir.rename(new_package_dir)
    return new_package_dir


def replace_package_references_in_tree(
    root: Path, old_package_name: str, new_package_name: str
) -> None:
    """Replace package references inside Python files under the package tree."""
    if old_package_name == new_package_name:
        return

    pattern = re.compile(rf"\b{re.escape(old_package_name)}\b")
    for file_path in root.rglob("*.py"):
        content = file_path.read_text(encoding="utf-8")
        updated_content = pattern.sub(new_package_name, content)
        if updated_content != content:
            file_path.write_text(updated_content, encoding="utf-8")


def initialize_project(
    root: Path,
    project_name: str,
    author_details: AuthorDetails | None = None,
    old_package_name: str = TEMPLATE_PACKAGE_NAME,
) -> tuple[str, str]:
    """Initialize the template repository with a new project name."""
    normalized_project_name = normalize_project_name(project_name)
    new_package_name = project_name_to_package_name(normalized_project_name)

    pyproject_path = root / "pyproject.toml"
    if not pyproject_path.exists():
        raise FileNotFoundError(f"pyproject.toml not found at {pyproject_path}")

    pyproject_content = pyproject_path.read_text(encoding="utf-8")
    updated_pyproject_content = update_pyproject_content(
        pyproject_content,
        old_package_name,
        normalized_project_name,
        author_details=author_details,
    )

    package_dir = rename_package_directory(root, old_package_name, new_package_name)
    replace_package_references_in_tree(package_dir, old_package_name, new_package_name)
    pyproject_path.write_text(updated_pyproject_content, encoding="utf-8")

    return normalized_project_name, new_package_name


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI parser for project initialization."""
    parser = argparse.ArgumentParser(
        description="Initialize the template project with a validated project name."
    )
    parser.add_argument(
        "--name",
        help=(
            "Distribution name to use for the project. Hyphens are converted to "
            "underscores for the Python package."
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    """CLI entrypoint."""
    args = build_parser().parse_args(argv)

    try:
        project_name = (
            normalize_project_name(args.name)
            if args.name is not None
            else prompt_for_project_name()
        )
        author_details = prompt_for_author_details(get_git_author_defaults())
        normalized_project_name, package_name = initialize_project(
            ROOT,
            project_name,
            author_details=author_details,
        )
    except (FileExistsError, FileNotFoundError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    print(
        f'Initialized project "{normalized_project_name}" with package "{package_name}".'
    )


if __name__ == "__main__":
    main()
