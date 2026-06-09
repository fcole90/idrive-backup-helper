"""Tests for transition_to_uv migration.

These tests load the `transition_to_uv.py` script as a module and call the
`migrate_pyproject_content` helper with fixture files stored under `mocks/`
so tests don't touch repository files.
"""

from importlib import util
from pathlib import Path
import tomllib


def load_transition_module():
    module_path = Path(__file__).parent / "transition_to_uv.py"
    spec = util.spec_from_file_location("transition_to_uv", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not load transition_to_uv.py")

    module = util.module_from_spec(spec)
    loader = spec.loader
    exec_module = getattr(loader, "exec_module", None)
    if exec_module is None or not callable(exec_module):
        raise RuntimeError("transition_to_uv.py loader cannot execute the module")

    exec_module(module)
    return module


def test_migration_matches_expected_fixture() -> None:
    module = load_transition_module()

    base = Path(__file__).parent / "mocks"
    input_text = (base / "mock_poetry_project.toml").read_text(encoding="utf-8")
    expected_text = (base / "expected_mock_uv_project.toml").read_text(encoding="utf-8")

    output = module.migrate_pyproject_content(input_text)

    assert tomllib.loads(output) == tomllib.loads(expected_text)


def test_extract_poetry_packages_handles_multiline_entries() -> None:
    module = load_transition_module()

    content = """
[tool.poetry]
packages = [
    { include = "my_project", from = "src" },
    { from = "lib", include = "shared" },
    { include = "top_level_package" },
]
"""

    assert module.extract_poetry_packages(content) == [
        "src/my_project",
        "lib/shared",
        "top_level_package",
    ]


def test_convert_version_constraint_handles_poetry_operators() -> None:
    module = load_transition_module()

    assert module.convert_version_constraint("^2.32.3") == ">=2.32.3,<3.0.0"
    assert module.convert_version_constraint("~0.28.1") == ">=0.28.1,<0.29.0"
    assert module.convert_version_constraint("1.2.*") == ">=1.2.0,<1.3.0"
