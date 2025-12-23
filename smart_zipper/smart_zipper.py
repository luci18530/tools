"""
smart_zipper.py

Cria um ZIP "limpo" do projeto, ignorando pastas e arquivos comuns que nao devem ir
para entrega/envio (ex: .git, node_modules, venv, dist, build, caches, etc).

Recursos:
- Ignora diretorios e arquivos por padrao (customizavel via flags)
- --dry-run para listar o que entraria e o que seria ignorado
- --include-hidden para incluir itens ocultos (nome iniciando com ".")
- --max-bytes para pular arquivos muito grandes
- --follow-symlinks (desligado por padrao)
- Salva zip no local desejado, com nome default baseado no nome da pasta raiz

Exemplos:
  python smart_zipper.py
  python smart_zipper.py --root . --output entrega.zip
  python smart_zipper.py --dry-run
  python smart_zipper.py --include-ext .py,.java,.js,.md
  python smart_zipper.py --exclude-ext .log,.tmp --add-ignore-dir .cache
  python smart_zipper.py --max-bytes 2000000
"""

from __future__ import annotations

import argparse
import os
import sys
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple


DEFAULT_IGNORE_DIRS = [
    ".git",
    ".hg",
    ".svn",
    ".idea",
    ".vscode",
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
    ".gradle",
    ".next",
    ".nuxt",
    "coverage",
    ".cache",
]

DEFAULT_IGNORE_FILES = [
    ".DS_Store",
    "Thumbs.db",
    "*.log",
    "*.tmp",
    "*.bak",
    "*.swp",
    "*.swo",
    "*~",
]

DEFAULT_MAX_BYTES = 10_000_000  # 10 MB por arquivo
SNIFF_BYTES = 8192


@dataclass
class ZipStats:
    added_files: int = 0
    skipped_files: int = 0
    skipped_bytes: int = 0
    added_bytes: int = 0


def parse_csv_list(raw: Optional[str]) -> List[str]:
    if not raw:
        return []
    parts = [p.strip() for p in raw.split(",")]
    return [p for p in parts if p]


def normalize_ext_list(exts: List[str]) -> List[str]:
    out = []
    for e in exts:
        e = e.strip()
        if not e:
            continue
        if not e.startswith("."):
            e = "." + e
        out.append(e.lower())
    return out


def is_hidden_name(name: str) -> bool:
    return name.startswith(".")


def prune_dirs_inplace(dirs: List[str], ignore_dirs: List[str], include_hidden: bool) -> None:
    ignore_set = set(ignore_dirs)
    keep = []
    for d in dirs:
        if d in ignore_set:
            continue
        if (not include_hidden) and is_hidden_name(d):
            continue
        keep.append(d)
    dirs[:] = keep


def looks_binary(sample: bytes) -> bool:
    if not sample:
        return False
    if b"\x00" in sample:
        return True
    text_chars = set(b"\n\r\t\b") | set(range(32, 127))
    nontext = 0
    for b in sample:
        if b not in text_chars:
            nontext += 1
    ratio = nontext / max(1, len(sample))
    return ratio > 0.30


def is_binary_file(path: Path) -> bool:
    try:
        with path.open("rb") as f:
            sample = f.read(SNIFF_BYTES)
        return looks_binary(sample)
    except OSError:
        return True


def match_any_glob(name: str, patterns: List[str]) -> bool:
    # Usa Path.match para padroes simples, mas Path.match considera caminhos.
    # Entao aplicamos sobre um Path ficticio com apenas o nome.
    p = Path(name)
    for pat in patterns:
        pat = pat.strip()
        if not pat:
            continue
        if p.match(pat):
            return True
    return False


def should_skip_file(
    file_path: Path,
    ignore_files_patterns: List[str],
    include_hidden: bool,
    include_ext: List[str],
    exclude_ext: List[str],
    max_bytes: int,
    skip_binaries: bool,
) -> Tuple[bool, str]:
    name = file_path.name

    if (not include_hidden) and is_hidden_name(name):
        return True, "hidden"

    if match_any_glob(name, ignore_files_patterns):
        return True, "ignore_pattern"

    ext = file_path.suffix.lower()

    if exclude_ext and ext in exclude_ext:
        return True, "excluded_ext"

    if include_ext and ext not in include_ext:
        return True, "not_included_ext"

    try:
        size = file_path.stat().st_size
    except OSError:
        return True, "stat_error"

    if size > max_bytes:
        return True, "too_large"

    if skip_binaries and is_binary_file(file_path):
        return True, "binary"

    return False, ""


def safe_arcname(root: Path, path: Path) -> str:
    # Caminho dentro do zip: relativo ao root, com "/" sempre
    rel = path.resolve().relative_to(root.resolve())
    return str(rel).replace("\\", "/")


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


def build_file_list(
    root: Path,
    ignore_dirs: List[str],
    ignore_files_patterns: List[str],
    include_hidden: bool,
    follow_symlinks: bool,
    include_ext: List[str],
    exclude_ext: List[str],
    max_bytes: int,
    skip_binaries: bool,
) -> Tuple[List[Tuple[Path, str]], List[str]]:
    """
    Retorna:
    - lista [(path_abs, arcname_rel)]
    - lista de logs de itens ignorados
    """
    root = root.resolve()
    selected: List[Tuple[Path, str]] = []
    ignored_logs: List[str] = []

    for dirpath, dirs, files in os.walk(root, followlinks=follow_symlinks):
        prune_dirs_inplace(dirs, ignore_dirs, include_hidden)

        current = Path(dirpath)

        # Se nao incluir ocultos, pula conteudo de pasta oculta
        if (not include_hidden) and current != root and is_hidden_name(current.name):
            continue

        for fname in files:
            fp = current / fname

            # Symlinks: por padrao nao seguimos para nao "puxar o mundo"
            if fp.is_symlink() and not follow_symlinks:
                ignored_logs.append(f"skip symlink: {safe_arcname(root, fp)}")
                continue

            skip, reason = should_skip_file(
                fp,
                ignore_files_patterns=ignore_files_patterns,
                include_hidden=include_hidden,
                include_ext=include_ext,
                exclude_ext=exclude_ext,
                max_bytes=max_bytes,
                skip_binaries=skip_binaries,
            )
            if skip:
                ignored_logs.append(f"skip ({reason}): {safe_arcname(root, fp)}")
                continue

            selected.append((fp, safe_arcname(root, fp)))

    # Ordena por arcname para zip deterministico
    selected.sort(key=lambda x: x[1].lower())
    return selected, ignored_logs


def write_zip(
    output_zip: Path,
    file_list: List[Tuple[Path, str]],
    root: Path,
    compression: str,
) -> ZipStats:
    output_zip.parent.mkdir(parents=True, exist_ok=True)

    if compression == "store":
        comp = zipfile.ZIP_STORED
    else:
        comp = zipfile.ZIP_DEFLATED

    stats = ZipStats()

    with zipfile.ZipFile(output_zip, "w", compression=comp) as zf:
        for fp, arc in file_list:
            try:
                size = fp.stat().st_size
            except OSError:
                stats.skipped_files += 1
                continue

            zf.write(fp, arcname=arc)
            stats.added_files += 1
            stats.added_bytes += size

    return stats


def write_ignored_log(path: Path, ignored_logs: List[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(ignored_logs) + ("\n" if ignored_logs else ""), encoding="utf-8", errors="replace")


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Cria um zip limpo do projeto, ignorando pastas/arquivos comuns (git, node_modules, venv, etc).",
    )
    p.add_argument("--root", type=str, default=None, help="Pasta raiz (default: pasta do script).")
    p.add_argument("--output", type=str, default=None, help="Caminho do zip (default: <rootname>_clean.zip).")

    p.add_argument("--dry-run", action="store_true", help="Apenas lista o que entraria no zip.")
    p.add_argument("--include-hidden", action="store_true", help="Inclui arquivos/pastas ocultos (nome iniciando com '.').")
    p.add_argument("--follow-symlinks", action="store_true", help="Segue symlinks (use com cuidado).")

    p.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES, help=f"Tamanho maximo por arquivo (default: {DEFAULT_MAX_BYTES}).")
    p.add_argument("--no-skip-binaries", action="store_true", help="Nao pula arquivos binarios (por padrao, pula).")

    p.add_argument("--include-ext", type=str, default="", help="CSV de extensoes para incluir (ex: .py,.java). Vazio = qualquer ext.")
    p.add_argument("--exclude-ext", type=str, default="", help="CSV de extensoes para excluir (ex: .log,.zip).")

    p.add_argument("--add-ignore-dir", action="append", default=[], help="Adiciona nome de diretorio para ignorar (pode repetir).")
    p.add_argument("--add-ignore-file", action="append", default=[], help='Adiciona padrao glob de arquivo para ignorar (pode repetir). Ex: "*.sqlite"')

    p.add_argument("--compression", choices=["deflate", "store"], default="deflate", help="Compressao: deflate (default) ou store (sem compressao).")

    p.add_argument("--write-ignored-log", action="store_true", help="Gera um log '<zip>.ignored.log' com itens ignorados.")
    return p


def main(argv: List[str]) -> int:
    args = build_arg_parser().parse_args(argv)

    # Root default: pasta do script
    if args.root:
        root = Path(args.root).expanduser()
    else:
        root = Path(__file__).resolve().parent
    root = root.resolve()

    include_ext = normalize_ext_list(parse_csv_list(args.include_ext))
    exclude_ext = normalize_ext_list(parse_csv_list(args.exclude_ext))

    ignore_dirs = DEFAULT_IGNORE_DIRS.copy()
    for d in args.add_ignore_dir:
        d = (d or "").strip()
        if d:
            ignore_dirs.append(d)

    ignore_files_patterns = DEFAULT_IGNORE_FILES.copy()
    for pat in args.add_ignore_file:
        pat = (pat or "").strip()
        if pat:
            ignore_files_patterns.append(pat)

    # Output default
    if args.output:
        output_zip = Path(args.output).expanduser()
    else:
        output_zip = root.parent / f"{root.name}_clean.zip"
    output_zip = output_zip.resolve()

    file_list, ignored_logs = build_file_list(
        root=root,
        ignore_dirs=ignore_dirs,
        ignore_files_patterns=ignore_files_patterns,
        include_hidden=bool(args.include_hidden),
        follow_symlinks=bool(args.follow_symlinks),
        include_ext=include_ext,
        exclude_ext=exclude_ext,
        max_bytes=int(args.max_bytes),
        skip_binaries=(not bool(args.no_skip_binaries)),
    )

    print("SMART ZIPPER")
    print(f"Root: {str(root)}")
    print(f"Output zip: {str(output_zip)}")
    print(f"Selected files: {len(file_list)}")
    print(f"Ignored entries: {len(ignored_logs)}")
    print(f"Compression: {args.compression}")
    print(f"Dry-run: {'sim' if args.dry_run else 'nao'}")
    print("")

    if args.dry_run:
        print("ARQUIVOS QUE ENTRARIAM NO ZIP:")
        for _, arc in file_list:
            print("  " + arc)
        print("")
        print("ITENS IGNORADOS (amostra):")
        # Evita output enorme
        sample = ignored_logs[:50]
        for line in sample:
            print("  " + line)
        if len(ignored_logs) > 50:
            print(f"  ... ({len(ignored_logs) - 50} a mais)")
        print("")
        print("DRY-RUN: nenhum zip foi gerado.")
        return 0

    stats = write_zip(
        output_zip=output_zip,
        file_list=file_list,
        root=root,
        compression=args.compression,
    )

    if args.write_ignored_log:
        log_path = Path(str(output_zip) + ".ignored.log")
        write_ignored_log(log_path, ignored_logs)
        print(f"Ignored log: {str(log_path)}")

    print("")
    print("OK")
    print(f"Added files: {stats.added_files}")
    print(f"Added size: {human_bytes(stats.added_bytes)} ({stats.added_bytes} bytes)")
    print(f"Zip path: {str(output_zip)}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
