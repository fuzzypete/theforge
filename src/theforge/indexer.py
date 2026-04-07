"""AST-based structural index generation for forge index."""

from __future__ import annotations

import ast
import hashlib
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import yaml

INDEX_PATH = Path(".forge/index/modules.yaml")


@dataclass(frozen=True)
class ModuleEntry:
    path: str
    public_symbols: tuple[str, ...]
    imports: tuple[str, ...]
    interface_hash: str
    generated_at: str
    git_sha: str

    def to_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "public_symbols": list(self.public_symbols),
            "imports": list(self.imports),
            "generated_at": self.generated_at,
            "git_sha": self.git_sha,
        }


class _ModuleAnalyzer(ast.NodeVisitor):
    def __init__(self) -> None:
        self.imports: set[str] = set()
        self.public_symbols: list[str] = []
        self._explicit_all: list[str] | None = None

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.imports.add(alias.name)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = "." * node.level + (node.module or "")
        if any(alias.name == "*" for alias in node.names):
            if module:
                self.imports.add(module)
            return
        for alias in node.names:
            imported = f"{module}.{alias.name}" if module else alias.name
            self.imports.add(imported)

    def visit_Assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            if isinstance(target, ast.Name):
                if target.id == "__all__":
                    explicit = _extract_string_list(node.value)
                    if explicit is not None:
                        self._explicit_all = explicit
                elif _is_public_name(target.id):
                    self.public_symbols.append(target.id)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if isinstance(node.target, ast.Name) and _is_public_name(node.target.id):
            self.public_symbols.append(node.target.id)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        if _is_public_name(node.name):
            self.public_symbols.append(node.name)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        if _is_public_name(node.name):
            self.public_symbols.append(node.name)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        if _is_public_name(node.name):
            self.public_symbols.append(node.name)

    def finalize(self) -> tuple[tuple[str, ...], tuple[str, ...]]:
        public = self._explicit_all if self._explicit_all is not None else self.public_symbols
        return tuple(sorted(set(public))), tuple(sorted(self.imports))


def _is_public_name(name: str) -> bool:
    return not name.startswith("_")


def _extract_string_list(node: ast.AST) -> list[str] | None:
    if not isinstance(node, (ast.List, ast.Tuple)):
        return None
    values: list[str] = []
    for elt in node.elts:
        if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
            values.append(elt.value)
        else:
            return None
    return values


def _iter_python_files(project_root: Path) -> list[Path]:
    excluded = {".git", ".venv", ".forge"}
    files: list[Path] = []
    for path in project_root.rglob("*.py"):
        if any(part in excluded for part in path.parts):
            continue
        files.append(path)
    return sorted(files)


def _analyze_module(
    path: Path, project_root: Path, generated_at: str, git_sha: str
) -> ModuleEntry:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    analyzer = _ModuleAnalyzer()
    analyzer.visit(tree)
    public_symbols, imports = analyzer.finalize()
    interface_hash = hashlib.sha256(
        "\n".join([str(path.relative_to(project_root)), *public_symbols]).encode("utf-8")
    ).hexdigest()
    return ModuleEntry(
        path=str(path.relative_to(project_root)),
        public_symbols=public_symbols,
        imports=imports,
        interface_hash=interface_hash,
        generated_at=generated_at,
        git_sha=git_sha,
    )


def _load_existing_index(index_path: Path) -> dict[str, dict[str, object]]:
    if not index_path.exists():
        return {}
    data = yaml.safe_load(index_path.read_text(encoding="utf-8")) or {}
    modules = data.get("modules", [])
    result: dict[str, dict[str, object]] = {}
    if isinstance(modules, list):
        for entry in modules:
            if isinstance(entry, dict) and isinstance(entry.get("path"), str):
                result[entry["path"]] = entry
    return result


def _current_git_sha(project_root: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=project_root,
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return "UNKNOWN"
    return result.stdout.strip() or "UNKNOWN"


def generate_index(project_root: Path, now: datetime | None = None) -> dict[str, object]:
    generated_at = (now or datetime.now(timezone.utc)).replace(microsecond=0).isoformat()
    git_sha = _current_git_sha(project_root)
    index_path = project_root / INDEX_PATH
    existing = _load_existing_index(index_path)

    modules: list[dict[str, object]] = []
    for path in _iter_python_files(project_root):
        entry = _analyze_module(path, project_root, generated_at, git_sha)
        prior = existing.get(entry.path)
        if prior and prior.get("interface_hash") == entry.interface_hash:
            reused = dict(prior)
            reused["imports"] = list(entry.imports)
            reused["generated_at"] = entry.generated_at
            reused["git_sha"] = entry.git_sha
            modules.append(reused)
            continue
        data = entry.to_dict()
        data["interface_hash"] = entry.interface_hash
        modules.append(data)

    payload = {"modules": modules}
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text(
        yaml.safe_dump(payload, sort_keys=False, default_flow_style=False),
        encoding="utf-8",
    )
    return payload
