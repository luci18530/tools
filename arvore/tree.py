"""
tree.py

Gera:
- tree.txt: estrutura de pastas/arquivos
- summary.md: resumo com contagem por extensao, tamanho total, top 10 maiores

Uso:
  python tree.py
  python tree.py --root .
  python tree.py --out-dir .
  python tree.py --max-depth 8 --include-hidden
  python tree.py --follow-symlinks

Observacoes:
- Por padrao ignora diretorios comuns gigantes/irrelevantes: .git, node_modules, venv, etc.
- Por padrao nao inclui arquivos/pastas ocultos (nome iniciando com ".").
- Nao usa caracteres especiais fora ASCII nos prints.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import DefaultDict, Dict, List, Optional, Tuple


DEFAULT_IGNORE_DIRS = [
    ".git",
    ".hg",
    ".svn",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "venv",
    "env",
    "node_modules",
    "dist",
    "build",
    "out",
    "target",
    ".idea",
    ".vscode",
]

# Arquivos "lixo" comuns (nao eh obrigatorio ignorar, mas ajuda)
DEFAULT_IGNORE_FILES = [
    ".DS_Store",
    "Thumbs.db",
]


@dataclass
class FileStat:
    rel_path: str
    size_bytes: int


def parse_csv_list(raw: str) -> List[str]:
    raw = (raw or "").strip()
    if not raw:
        return []
    parts = [p.strip() for p in raw.split(",")]
    return [p for p in parts if p]


def is_hidden_name(name: str) -> bool:
    return name.startswith(".")


def human_bytes(n: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(n)
    idx = 0
    while size >= 1024.0 and idx < len(units) - 1:
        size /= 1024.0
        idx += 1
    if idx == 0:
        return f"{int(size)} {units[idx]}"
    return f"{size:.2f} {units[idx]}"


def should_skip_dir(name: str, ignore_dirs: List[str], include_hidden: bool) -> bool:
    if name in ignore_dirs:
        return True
    if (not include_hidden) and is_hidden_name(name):
        return True
    return False


def should_skip_file(name: str, ignore_files: List[str], include_hidden: bool) -> bool:
    if name in ignore_files:
        return True
    if (not include_hidden) and is_hidden_name(name):
        return True
    return False


def safe_relpath(root: Path, target: Path) -> str:
    return str(target.resolve().relative_to(root.resolve())).replace("\\", "/")


def list_dir_entries(
    path: Path,
    ignore_dirs: List[str],
    ignore_files: List[str],
    include_hidden: bool,
    follow_symlinks: bool,
) -> Tuple[List[Path], List[Path]]:
    """
    Retorna (dirs, files) filtrados e ordenados.
    """
    dirs: List[Path] = []
    files: List[Path] = []

    try:
        for entry in path.iterdir():
            name = entry.name

            # Symlinks: por padrao nao seguimos (para evitar loops)
            if entry.is_symlink() and not follow_symlinks:
                continue

            if entry.is_dir():
                if should_skip_dir(name, ignore_dirs, include_hidden):
                    continue
                dirs.append(entry)
            elif entry.is_file():
                if should_skip_file(name, ignore_files, include_hidden):
                    continue
                files.append(entry)
    except OSError:
        # Sem permissao, etc. Apenas retorna vazio
        return [], []

    dirs.sort(key=lambda p: p.name.lower())
    files.sort(key=lambda p: p.name.lower())
    return dirs, files


def build_tree_lines(
    root: Path,
    ignore_dirs: List[str],
    ignore_files: List[str],
    include_hidden: bool,
    follow_symlinks: bool,
    max_depth: int,
) -> List[str]:
    """
    Monta o conteudo do tree.txt.
    Conectores ASCII: |--, `--.
    """
    root = root.resolve()
    lines: List[str] = []
    lines.append(f"{root.name}/")

    def walk_dir(current: Path, prefix: str, depth: int) -> None:
        if depth >= max_depth:
            return

        dirs, files = list_dir_entries(
            current,
            ignore_dirs=ignore_dirs,
            ignore_files=ignore_files,
            include_hidden=include_hidden,
            follow_symlinks=follow_symlinks,
        )

        entries: List[Tuple[Path, bool]] = []
        for d in dirs:
            entries.append((d, True))
        for f in files:
            entries.append((f, False))

        for idx, (entry, is_dir) in enumerate(entries):
            last = (idx == len(entries) - 1)
            connector = "`-- " if last else "|-- "
            name = entry.name + ("/" if is_dir else "")
            lines.append(prefix + connector + name)

            if is_dir:
                next_prefix = prefix + ("    " if last else "|   ")
                walk_dir(entry, next_prefix, depth + 1)

    walk_dir(root, prefix="", depth=0)
    return lines


def collect_summary_stats(
    root: Path,
    ignore_dirs: List[str],
    ignore_files: List[str],
    include_hidden: bool,
    follow_symlinks: bool,
) -> Tuple[int, int, int, Dict[str, Tuple[int, int]], List[FileStat]]:
    """
    Retorna:
    - total_dirs
    - total_files
    - total_bytes
    - por_ext: dict ext -> (count, bytes)
    - top_files: lista de FileStat (todos), para depois pegar top 10
    """
    root = root.resolve()

    total_dirs = 0
    total_files = 0
    total_bytes = 0

    # ext -> (count, bytes)
    ext_counts: DefaultDict[str, int] = defaultdict(int)
    ext_bytes: DefaultDict[str, int] = defaultdict(int)

    all_files: List[FileStat] = []

    for dirpath, dirs, files in os.walk(root, followlinks=follow_symlinks):
        # Prune dirs in-place
        pruned = []
        for d in dirs:
            if should_skip_dir(d, ignore_dirs, include_hidden):
                continue
            pruned.append(d)
        dirs[:] = pruned

        current = Path(dirpath)

        # Conta diretorios visitados (exceto root)
        if current != root:
            total_dirs += 1

        # Se nao incluir ocultos, nao conta arquivos dentro de pasta oculta
        if (not include_hidden) and is_hidden_name(current.name) and current != root:
            continue

        for fn in files:
            if should_skip_file(fn, ignore_files, include_hidden):
                continue

            fp = current / fn

            if fp.is_symlink() and not follow_symlinks:
                continue

            try:
                size = fp.stat().st_size
            except OSError:
                continue

            total_files += 1
            total_bytes += size

            ext = fp.suffix.lower()
            if ext == "":
                ext = "(sem_ext)"

            ext_counts[ext] += 1
            ext_bytes[ext] += size

            all_files.append(FileStat(rel_path=safe_relpath(root, fp), size_bytes=size))

    por_ext: Dict[str, Tuple[int, int]] = {}
    for ext, cnt in ext_counts.items():
        por_ext[ext] = (cnt, int(ext_bytes[ext]))

    return total_dirs, total_files, total_bytes, por_ext, all_files


def render_summary_md(
    root: Path,
    generated_at: str,
    total_dirs: int,
    total_files: int,
    total_bytes: int,
    por_ext: Dict[str, Tuple[int, int]],
    all_files: List[FileStat],
    top_n: int,
) -> str:
    lines: List[str] = []
    lines.append("# Repo summary")
    lines.append("")
    lines.append(f"- Generated at: {generated_at}")
    lines.append(f"- Root: `{str(root.resolve()).replace('`', '')}`")
    lines.append("")
    lines.append("## Totals")
    lines.append("")
    lines.append(f"- Directories (visited): **{total_dirs}**")
    lines.append(f"- Files: **{total_files}**")
    lines.append(f"- Total size: **{human_bytes(total_bytes)}** ({total_bytes} bytes)")
    lines.append("")

    # Tabela por extensao
    lines.append("## Files by extension")
    lines.append("")
    lines.append("| Extension | Count | Total size | Bytes |")
    lines.append("|---|---:|---:|---:|")

    rows = []
    for ext, (cnt, b) in por_ext.items():
        rows.append((b, ext, cnt))
    # Ordena por tamanho total desc
    rows.sort(key=lambda x: x[0], reverse=True)

    for b, ext, cnt in rows:
        lines.append(f"| `{ext}` | {cnt} | {human_bytes(b)} | {b} |")

    lines.append("")

    # Top N maiores arquivos
    lines.append(f"## Top {top_n} largest files")
    lines.append("")
    lines.append("| Rank | File | Size | Bytes |")
    lines.append("|---:|---|---:|---:|")

    all_files_sorted = sorted(all_files, key=lambda x: x.size_bytes, reverse=True)
    top = all_files_sorted[:top_n]

    for i, fs in enumerate(top, start=1):
        lines.append(f"| {i} | `{fs.rel_path}` | {human_bytes(fs.size_bytes)} | {fs.size_bytes} |")

    lines.append("")
    return "\n".join(lines)


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", errors="replace")


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Gera tree.txt e summary.md para documentacao e PR.",
    )
    p.add_argument("--root", type=str, default=None, help="Pasta raiz (default: pasta do script).")
    p.add_argument("--out-dir", type=str, default=None, help="Pasta de saida (default: root).")
    p.add_argument(
        "--max-depth",
        type=int,
        default=8,
        help="Profundidade maxima no tree.txt (default: 8).",
    )
    p.add_argument(
        "--top-n",
        type=int,
        default=10,
        help="Quantidade de maiores arquivos no summary.md (default: 10).",
    )
    p.add_argument(
        "--include-hidden",
        action="store_true",
        help="Inclui arquivos/pastas ocultos (nome iniciando com '.').",
    )
    p.add_argument(
        "--follow-symlinks",
        action="store_true",
        help="Segue symlinks (use com cuidado, pode haver loops).",
    )
    p.add_argument(
        "--ignore-dirs",
        type=str,
        default="",
        help="Lista CSV de diretorios para ignorar (alem do padrao).",
    )
    p.add_argument(
        "--ignore-files",
        type=str,
        default="",
        help="Lista CSV de arquivos para ignorar (alem do padrao).",
    )
    p.add_argument(
        "--tree-name",
        type=str,
        default="tree.txt",
        help="Nome do arquivo de tree (default: tree.txt).",
    )
    p.add_argument(
        "--summary-name",
        type=str,
        default="summary.md",
        help="Nome do arquivo de resumo (default: summary.md).",
    )
    return p


def main(argv: List[str]) -> int:
    args = build_arg_parser().parse_args(argv)

    if args.root:
        root = Path(args.root).expanduser()
    else:
        root = Path(__file__).resolve().parent
    root = root.resolve()

    if args.out_dir:
        out_dir = Path(args.out_dir).expanduser().resolve()
    else:
        out_dir = root

    ignore_dirs = DEFAULT_IGNORE_DIRS.copy()
    ignore_dirs.extend(parse_csv_list(args.ignore_dirs))

    ignore_files = DEFAULT_IGNORE_FILES.copy()
    ignore_files.extend(parse_csv_list(args.ignore_files))

    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 1) tree.txt
    tree_lines = build_tree_lines(
        root=root,
        ignore_dirs=ignore_dirs,
        ignore_files=ignore_files,
        include_hidden=bool(args.include_hidden),
        follow_symlinks=bool(args.follow_symlinks),
        max_depth=int(args.max_depth),
    )
    tree_path = out_dir / args.tree_name
    write_text(tree_path, "\n".join(tree_lines) + "\n")

    # 2) summary.md
    total_dirs, total_files, total_bytes, por_ext, all_files = collect_summary_stats(
        root=root,
        ignore_dirs=ignore_dirs,
        ignore_files=ignore_files,
        include_hidden=bool(args.include_hidden),
        follow_symlinks=bool(args.follow_symlinks),
    )
    summary_text = render_summary_md(
        root=root,
        generated_at=generated_at,
        total_dirs=total_dirs,
        total_files=total_files,
        total_bytes=total_bytes,
        por_ext=por_ext,
        all_files=all_files,
        top_n=int(args.top_n),
    )
    summary_path = out_dir / args.summary_name
    write_text(summary_path, summary_text)

    # Prints (ASCII only)
    print("OK")
    print(f"Root: {str(root)}")
    print(f"Tree: {str(tree_path)}")
    print(f"Summary: {str(summary_path)}")
    print(f"Files counted: {total_files}")
    print(f"Total size: {total_bytes} bytes")

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
