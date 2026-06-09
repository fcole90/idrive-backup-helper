"""Tests for project initialization."""

from importlib import util
from pathlib import Path
from types import ModuleType
import tomllib
from typing import cast

import pytest


def load_init_project_module() -> ModuleType:
    module_path = Path(__file__).with_name("init_project.py")
    spec = util.spec_from_file_location("init_project", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not load init_project.py")

    module = util.module_from_spec(spec)
    loader = cast(object, spec.loader)
    exec_module = getattr(loader, "exec_module", None)
    if exec_module is None or not callable(exec_module):
        raise RuntimeError("init_project.py loader cannot execute the module")

    exec_module(module)
    return module


init_project = load_init_project_module()


@pytest.mark.parametrize(
    ("project_name", "error_message"),
    [
        ("", "cannot be empty"),
        ("123app", "must start with a letter"),
        ("bad name", "must start with a letter"),
        ("class", "valid Python package name"),
    ],
)
def test_normalize_project_name_rejects_invalid_names(
    project_name: str, error_message: str
) -> None:
    with pytest.raises(ValueError, match=error_message):
        init_project.normalize_project_name(project_name)


@pytest.mark.parametrize(
    ("author_name", "error_message"),
    [
        ("", "cannot be empty"),
        ("Fabio", "name and surname"),
    ],
)
def test_normalize_author_name_rejects_invalid_names(
    author_name: str, error_message: str
) -> None:
    with pytest.raises(ValueError, match=error_message):
        init_project.normalize_author_name(author_name)


@pytest.mark.parametrize(
    ("author_email", "error_message"),
    [
        ("", "cannot be empty"),
        ("not-an-email", "valid email address"),
    ],
)
def test_normalize_author_email_rejects_invalid_values(
    author_email: str, error_message: str
) -> None:
    with pytest.raises(ValueError, match=error_message):
        init_project.normalize_author_email(author_email)


def test_prompt_for_project_name_retries_until_valid() -> None:
    prompts: list[str] = []
    messages: list[str] = []
    answers = iter(["bad name", "Cool-App"])

    def fake_input(prompt: str) -> str:
        prompts.append(prompt)
        return next(answers)

    def fake_output(message: str) -> None:
        messages.append(message)

    project_name = init_project.prompt_for_project_name(
        input_func=fake_input,
        output_func=fake_output,
    )

    assert project_name == "cool-app"
    assert prompts == ["Project name: ", "Project name: "]
    assert messages == [
        "Invalid project name: Project name must start with a letter and contain only letters, digits, hyphens, or underscores."
    ]


def test_prompt_for_author_details_uses_git_suggestions_by_default() -> None:
    prompts: list[str] = []
    messages: list[str] = []
    answers = iter(["", "", ""])

    def fake_input(prompt: str) -> str:
        prompts.append(prompt)
        return next(answers)

    def fake_output(message: str) -> None:
        messages.append(message)

    author_details = init_project.prompt_for_author_details(
        init_project.AuthorPromptDefaults(
            name="Fabio Colella",
            email="fcole90@gmail.com",
        ),
        input_func=fake_input,
        output_func=fake_output,
    )

    assert author_details == init_project.AuthorDetails(
        name="Fabio Colella",
        email="fcole90@gmail.com",
    )
    assert prompts == [
        "Set author name and email in pyproject.toml? [Y/n]: ",
        "Author name and surname [Fabio Colella]: ",
        "Author email [fcole90@gmail.com]: ",
    ]
    assert messages == []


def test_prompt_for_author_details_allows_skipping_author_metadata() -> None:
    prompts: list[str] = []
    answers = iter(["n"])

    def fake_input(prompt: str) -> str:
        prompts.append(prompt)
        return next(answers)

    author_details = init_project.prompt_for_author_details(
        init_project.AuthorPromptDefaults(
            name="Fabio Colella",
            email="fcole90@gmail.com",
        ),
        input_func=fake_input,
    )

    assert author_details is None
    assert prompts == ["Set author name and email in pyproject.toml? [Y/n]: "]


def test_update_pyproject_content_updates_name_sources_and_entrypoints() -> None:
    pyproject_path = Path(__file__).parent.parent / "pyproject.toml"
    content = pyproject_path.read_text(encoding="utf-8")

    updated_content = init_project.update_pyproject_content(
        content,
        "my_project",
        "cool-app",
        author_details=init_project.AuthorDetails(
            name="Fabio Colella",
            email="fcole90@gmail.com",
        ),
    )
    parsed = tomllib.loads(updated_content)

    assert parsed["project"]["name"] == "cool-app"
    assert parsed["project"]["authors"] == [
        {"name": "Fabio Colella", "email": "fcole90@gmail.com"}
    ]
    assert parsed["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == [
        "src/cool_app",
        "scripts",
    ]
    assert parsed["project"]["scripts"]["init-project"] == "scripts.init_project:main"
    assert parsed["project"]["scripts"]["main"] == "cool_app.main:main"
    assert "sync-ai-policy" not in parsed["project"]["scripts"]


def test_initialize_project_renames_package_and_updates_imports(
    tmp_path: Path,
) -> None:
    pyproject_path = tmp_path / "pyproject.toml"
    pyproject_path.write_text(
        """
[project]
name = "my-project"
authors = [{ name = "Name Surname", email = "email@example.com" }]

[tool.hatch.build.targets.wheel]
packages = ["src/my_project", "scripts"]

[project.scripts]
init-project = "scripts.init_project:main"
main = "my_project.main:main"
""".strip() + "\n",
        encoding="utf-8",
    )

    package_dir = tmp_path / "src" / "my_project"
    package_dir.mkdir(parents=True)
    (package_dir / "main.py").write_text(
        'def some_function() -> str:\n    return "Hello, World!"\n',
        encoding="utf-8",
    )
    (package_dir / "main_test.py").write_text(
        "from my_project.main import some_function\n",
        encoding="utf-8",
    )

    normalized_project_name, package_name = init_project.initialize_project(
        tmp_path,
        "Cool-App",
        author_details=init_project.AuthorDetails(
            name="Fabio Colella",
            email="fcole90@gmail.com",
        ),
    )

    renamed_package_dir = tmp_path / "src" / "cool_app"
    assert normalized_project_name == "cool-app"
    assert package_name == "cool_app"
    assert not package_dir.exists()
    assert renamed_package_dir.exists()
    assert (renamed_package_dir / "main_test.py").read_text(
        encoding="utf-8"
    ) == "from cool_app.main import some_function\n"

    parsed = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    assert parsed["project"]["name"] == "cool-app"
    assert parsed["project"]["authors"] == [
        {"name": "Fabio Colella", "email": "fcole90@gmail.com"}
    ]
    assert parsed["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == [
        "src/cool_app",
        "scripts",
    ]
    assert parsed["project"]["scripts"]["init-project"] == "scripts.init_project:main"
    assert parsed["project"]["scripts"]["main"] == "cool_app.main:main"
