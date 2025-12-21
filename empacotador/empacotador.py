#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
empacotador.py

Lê arquivos de texto a partir de uma pasta raiz (default: pasta deste script),
varre subpastas, ignora diretórios comuns de build/cache, pula binários/arquivos
muito grandes, e gera um arquivo unico (.txt ou .md) contendo:

- Nome do arquivo
- Caminho (relativo e absoluto)
- Conteúdo

Uso rapido:
  python empacotador.py
  python empacotador.py --root . --output projeto_pack.txt
  python empacotador.py --include-ext .py,.js,.java --exclude-ext .log,.lock
  python empacotador.py --format md --output prompt.md
  python empacotador.py --zip

Saida TXT (default) usa separadores ASCII.
Saida MD envolve cada arquivo em bloco de codigo.
"""

from __future__ import annotations

import argparse
import os
import sys
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, List, Optional, Tuple


DEFAULT_IGNORE_DIRS = [
    ".git",
    ".svn",
    ".hg",
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


DEFAULT_IGNORE_FILES = [
    ".DS_Store",
    "Thumbs.db",
]


DEFAULT_MAX_BYTES = 1_000_000
DEFAULT_MAX_TOTAL_BYTES = 15_000_000  
SNIFF_BYTES = 8192

@dataclass
class FileItem:
    rel_path: str
    abs_path: str
    size_bytes: int
    content: str


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


def is_hidden_path(path: Path) -> bool:
    # Ocultos no Unix: começam com "."
    # No Windows, atributo hidden nao e acessivel facilmente sem libs; entao usamos apenas nome.
    return path.name.startswith(".")


def looks_binary(sample: bytes) -> bool:
    if not sample:
        return False
    if b"\x00" in sample:
        return True
    # Heuristica simples: se muitos bytes sao nao imprimiveis (exceto whitespace comum), trata como binario.
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


def read_text_file(path: Path) -> Tuple[str, str]:
    """
    Retorna (conteudo, encoding_usado).
    Usa utf-8 primeiro, depois tenta latin-1.
    """
    try:
        data = path.read_bytes()
    except OSError as e:
        return f"[ERROR reading file: {e}]\n", "binary"

    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        # latin-1 cobre todos bytes, mas pode gerar lixo
        try:
            return data.decode("latin-1"), "latin-1"
        except Exception as e:
            return f"[ERROR decoding file: {e}]\n", "binary"


def should_skip_file(
    file_path: Path,
    include_ext: List[str],
    exclude_ext: List[str],
    ignore_files: List[str],
    include_hidden: bool,
) -> bool:
    name = file_path.name
    if name in ignore_files:
        return True

    if not include_hidden and is_hidden_path(file_path):
        return True

    ext = file_path.suffix.lower()

    if exclude_ext and ext in exclude_ext:
        return True

    if include_ext and ext not in include_ext:
        return True

    return False


def prune_dirs_inplace(
    dirs: List[str],
    ignore_dirs: List[str],
    include_hidden: bool,
) -> None:
    """
    Modifica a lista 'dirs' in-place para o os.walk nao descer em diretorios ignorados.
    """
    keep = []
    ignore_set = set(ignore_dirs)
    for d in dirs:
        p = Path(d)
        if d in ignore_set:
            continue
        if (not include_hidden) and p.name.startswith("."):
            continue
        keep.append(d)
    dirs[:] = keep


def collect_files(
    root: Path,
    include_ext: List[str],
    exclude_ext: List[str],
    ignore_dirs: List[str],
    ignore_files: List[str],
    include_hidden: bool,
    follow_symlinks: bool,
    max_bytes: int,
    max_total_bytes: int,
    skip_paths: Optional[Iterable[Path]] = None,
) -> Tuple[List[FileItem], List[str]]:
    items: List[FileItem] = []
    skipped: List[str] = []
    total_bytes = 0

    root = root.resolve()
    # Normaliza paths para skip   
    skip_set = {p.resolve() for p in skip_paths} if skip_paths else set()

    for dirpath, dirs, files in os.walk(root, followlinks=follow_symlinks):
        # prune dirs para ignorar
        prune_dirs_inplace(dirs, ignore_dirs, include_hidden)

        for fname in files:
            fpath = Path(dirpath) / fname

            # pula caminhos na lista de skip
            try:
                resolved = fpath.resolve()
            except OSError:
                resolved = fpath
            if resolved in skip_set:
                skipped.append(f"skip by skip_paths: {str(fpath)}")
                continue

            # pula symlinks se nao for para seguir
            if fpath.is_symlink() and not follow_symlinks:
                skipped.append(f"skip symlink: {str(fpath)}")
                continue

            if should_skip_file(
                fpath,
                include_ext=include_ext,
                exclude_ext=exclude_ext,
                ignore_files=ignore_files,
                include_hidden=include_hidden,
            ):
                skipped.append(f"skip by rule: {str(fpath)}")
                continue

            try:
                size = fpath.stat().st_size
            except OSError as e:
                skipped.append(f"skip stat error: {str(fpath)} ({e})")
                continue

            if size > max_bytes:
                skipped.append(f"skip too large ({size} bytes): {str(fpath)}")
                continue

            if is_binary_file(fpath):
                skipped.append(f"skip binary: {str(fpath)}")
                continue

            if total_bytes + size > max_total_bytes:
                skipped.append(
                    f"skip total limit reached (would exceed {max_total_bytes}): {str(fpath)}"
                )
                continue

            content, enc = read_text_file(fpath)
            # Opcional: anotar encoding dentro do conteudo? Aqui so guardamos no texto final pelo header.
            rel = str(fpath.relative_to(root)).replace("\\", "/")
            abs_ = str(fpath.resolve())
            items.append(FileItem(rel_path=rel, abs_path=abs_, size_bytes=size, content=content))
            total_bytes += size

    # ordena por caminho
    items.sort(key=lambda x: x.rel_path.lower())
    return items, skipped


def render_txt(
    items: List[FileItem],
    root: Path,
    generated_at: str,
) -> str:
    lines: List[str] = []
    lines.append("PROJECT PACK")
    lines.append(f"Generated at: {generated_at}")
    lines.append(f"Root: {str(root.resolve())}")
    lines.append(f"Files: {len(items)}")
    lines.append("")
    lines.append("INDEX")
    for i, it in enumerate(items, start=1):
        lines.append(f"{i:04d}  {it.rel_path}  ({it.size_bytes} bytes)")
    lines.append("")
    lines.append("CONTENT")
    lines.append("")

    for it in items:
        lines.append("=" * 78)
        lines.append(f"FILE: {it.rel_path}")
        lines.append(f"PATH: {it.abs_path}")
        lines.append(f"SIZE: {it.size_bytes} bytes")
        lines.append("-" * 78)
        lines.append("CONTENT START")
        lines.append("-" * 78)
        # Mantem conteudo como esta
        lines.append(it.content.rstrip("\n"))
        lines.append("")
        lines.append("-" * 78)
        lines.append("CONTENT END")
        lines.append("=" * 78)
        lines.append("")

    return "\n".join(lines)


def render_md(
    items: List[FileItem],
    root: Path,
    generated_at: str,
) -> str:
    lines: List[str] = []
    lines.append("# Project Pack")
    lines.append("")
    lines.append(f"- Generated at: {generated_at}")
    lines.append(f"- Root: `{str(root.resolve()).replace('`', '')}`")
    lines.append(f"- Files: **{len(items)}**")
    lines.append("")
    lines.append("## Index")
    lines.append("")
    for i, it in enumerate(items, start=1):
        lines.append(f"{i}. `{it.rel_path}` ({it.size_bytes} bytes)")
    lines.append("")
    lines.append("## Content")
    lines.append("")

    for it in items:
        lines.append(f"### `{it.rel_path}`")
        lines.append("")
        lines.append(f"- Path: `{it.abs_path.replace('`', '')}`")
        lines.append(f"- Size: {it.size_bytes} bytes")
        lines.append("")
        # tenta usar linguagem pelo sufixo
        lang = it.rel_path.split(".")[-1].lower() if "." in it.rel_path else ""
        lines.append(f"```{lang}")
        lines.append(it.content.rstrip("\n"))
        lines.append("```")
        lines.append("")

    return "\n".join(lines)


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", errors="replace")


def zip_output(zip_path: Path, file_path: Path) -> None:
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.write(file_path, arcname=file_path.name)


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Empacota projeto em um unico arquivo TXT ou MD para colar como prompt.",
    )
    p.add_argument(
        "--root",
        type=str,
        default=None,
        help="Pasta raiz para varrer (default: pasta do script).",
    )
    p.add_argument(
        "--output",
        type=str,
        default=None,
        help="Arquivo de saida (default: project_pack.txt ou project_pack.md).",
    )
    p.add_argument(
        "--format",
        type=str,
        choices=["txt", "md"],
        default="txt",
        help="Formato da saida: txt ou md (default: txt).",
    )
    p.add_argument(
        "--include-ext",
        type=str,
        default="",
        help="Lista CSV de extensoes para incluir (ex: .py,.js). Vazio = qualquer texto detectado.",
    )
    p.add_argument(
        "--exclude-ext",
        type=str,
        default="",
        help="Lista CSV de extensoes para excluir (ex: .log,.lock).",
    )
    p.add_argument(
        "--ignore-dirs",
        type=str,
        default="",
        help="Lista CSV de nomes de diretorios para ignorar (ex: .git,node_modules).",
    )
    p.add_argument(
        "--ignore-files",
        type=str,
        default="",
        help="Lista CSV de nomes de arquivos para ignorar (ex: package-lock.json).",
    )
    p.add_argument(
        "--max-bytes",
        type=int,
        default=DEFAULT_MAX_BYTES,
        help=f"Tamanho maximo por arquivo em bytes (default: {DEFAULT_MAX_BYTES}).",
    )
    p.add_argument(
        "--max-total-bytes",
        type=int,
        default=DEFAULT_MAX_TOTAL_BYTES,
        help=f"Tamanho maximo total em bytes somando arquivos (default: {DEFAULT_MAX_TOTAL_BYTES}).",
    )
    p.add_argument(
        "--include-hidden",
        action="store_true",
        help="Inclui arquivos/pastas ocultos (nomes iniciando com '.').",
    )
    p.add_argument(
        "--follow-symlinks",
        action="store_true",
        help="Segue symlinks durante a varredura.",
    )
    p.add_argument(
        "--zip",
        action="store_true",
        help="Gera um .zip contendo o arquivo de saida.",
    )
    p.add_argument(
        "--zip-output",
        type=str,
        default=None,
        help="Caminho do zip (default: <output>.zip).",
    )
    p.add_argument(
        "--write-skipped-log",
        action="store_true",
        help="Gera um arquivo '<output>.skipped.log' listando o que foi pulado.",
    )
    return p


def main(argv: List[str]) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    # root default: pasta do script
    if args.root:
        root = Path(args.root).expanduser()
    else:
        root = Path(__file__).resolve().parent

    include_ext = normalize_ext_list(parse_csv_list(args.include_ext))
    exclude_ext = normalize_ext_list(parse_csv_list(args.exclude_ext))

    ignore_dirs = DEFAULT_IGNORE_DIRS.copy()
    extra_ignore_dirs = [x.strip() for x in parse_csv_list(args.ignore_dirs)]
    ignore_dirs.extend([x for x in extra_ignore_dirs if x])

    ignore_files = DEFAULT_IGNORE_FILES.copy()
    extra_ignore_files = [x.strip() for x in parse_csv_list(args.ignore_files)]
    ignore_files.extend([x for x in extra_ignore_files if x])

    # output default baseado no format
    if args.output:
        out_path = Path(args.output).expanduser()
    else:
        out_name = "project_pack.md" if args.format == "md" else "project_pack.txt"
        out_path = root / out_name

    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    skip_paths = [Path(__file__).resolve(), out_path.resolve()]
    items, skipped = collect_files(
        root=root,
        include_ext=include_ext,
        exclude_ext=exclude_ext,
        ignore_dirs=ignore_dirs,
        ignore_files=ignore_files,
        include_hidden=bool(args.include_hidden),
        follow_symlinks=bool(args.follow_symlinks),
        max_bytes=int(args.max_bytes),
        max_total_bytes=int(args.max_total_bytes),
        skip_paths=skip_paths,
    )

    if args.format == "md":
        output_text = render_md(items, root=root, generated_at=generated_at)
    else:
        output_text = render_txt(items, root=root, generated_at=generated_at)

    write_text(out_path, output_text)

    if args.write_skipped_log:
        log_path = Path(str(out_path) + ".skipped.log")
        write_text(log_path, "\n".join(skipped) + ("\n" if skipped else ""))

    if args.zip:
        if args.zip_output:
            zip_path = Path(args.zip_output).expanduser()
        else:
            zip_path = Path(str(out_path) + ".zip")
        zip_output(zip_path, out_path)

    # Prints (ASCII only)
    print("OK")
    print(f"Root: {str(root.resolve())}")
    print(f"Output: {str(out_path.resolve())}")
    print(f"Files packed: {len(items)}")
    print(f"Skipped: {len(skipped)}")
    if args.zip:
        print(f"Zip: {str(zip_path.resolve())}")
    if args.write_skipped_log:
        print(f"Skipped log: {str((Path(str(out_path) + '.skipped.log')).resolve())}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
