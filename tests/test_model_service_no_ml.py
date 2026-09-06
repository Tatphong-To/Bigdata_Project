"""CLAUDE.md: the Model Service reads cluster_id, it never trains. Assert no
ML / clustering import anywhere under model_service/."""

import ast
from pathlib import Path

_MODEL_SERVICE = Path(__file__).resolve().parents[1] / "model_service"

_BANNED_TOP = {"sklearn", "numpy", "pandas", "scipy", "torch", "tensorflow"}
_BANNED_FOOD_PIPELINE_SUBMODULES = {"clustering", "dag_tasks", "features"}


def _imports(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    tops: set[str] = set()
    food_pipeline_subs: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                tops.add(a.name.split(".")[0])
                if a.name.startswith("food_pipeline."):
                    food_pipeline_subs.add(a.name.split(".")[1])
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            tops.add(mod.split(".")[0])
            if mod.startswith("food_pipeline."):
                food_pipeline_subs.add(mod.split(".")[1])
    return tops, food_pipeline_subs


def test_no_ml_imports_anywhere_in_model_service():
    files = sorted(_MODEL_SERVICE.glob("*.py"))
    assert files, "no model_service/*.py found"
    for f in files:
        tops, fp_subs = _imports(f)
        assert not (tops & _BANNED_TOP), f"{f.name} imports {tops & _BANNED_TOP}"
        assert not (fp_subs & _BANNED_FOOD_PIPELINE_SUBMODULES), (
            f"{f.name} imports food_pipeline.{fp_subs & _BANNED_FOOD_PIPELINE_SUBMODULES}"
        )
        src = f.read_text(encoding="utf-8")
        assert "KMeans" not in src, f"{f.name} references KMeans"


def test_only_safety_filter_and_db_are_pulled_from_food_pipeline():
    used: set[str] = set()
    for f in _MODEL_SERVICE.glob("*.py"):
        _, fp_subs = _imports(f)
        used |= fp_subs
    assert used <= {"safety_filter", "db"}, f"unexpected food_pipeline imports: {used}"
