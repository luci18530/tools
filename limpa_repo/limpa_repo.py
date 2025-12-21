"""
limpa_repo.py

"Higienizador" de repositório: remove diretórios de cache/build e arquivos lixo
com segurança, com suporte a --dry-run para apenas listar o que seria removido.

Exemplos:
  python limpa_repo.py
  python limpa_repo.py --dry-run
  python limpa_repo.py --root .
  python limpa_repo.py --include-hidden
  python limpa_repo.py --add-dir .ruff_cache --add-glob "*.tmp" --add-glob "*.bak"
  python limpa_repo.py --yes

Comportamento:
- Por padrão, não remove nada automaticamente se o alvo estiver fora do root.
- Por padrão, não entra em pastas ocultas (nomes começando com ".") a menos que --include-hidden.
- Diretórios comuns: __pycache__, .pytest_cache, .mypy_cache, dist, build, etc.
- Arquivos comuns por padrão: *.log, *.tmp, *.bak, *.swp, *.swo, *~ etc.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Tuple


# Diretórios típicos que queremos remover
DEFAULT_TARGET_DIRS = [
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".tox",
    ".coverage",
    "dist",
    "build",
    "out",
    "target",          # comum em Java (Maven/Gradle)
    ".gradle",
    ".idea",           # IDEs (opcional, mas útil)
    ".vscode",         # idem
]

# Padrões de arquivos típicos
DEFAULT_TARGET_GLOBS = [
    "*.log",
    "*.tmp",
    "*.bak",
    "*.swp",
    "*.swo",
    "*~",
    ".DS_Store",
    "Thumbs.db",
]

# Diretórios que normalmente não faz sentido varrer (porque são enormes)
# Não removemos eles aqui (a não ser que estejam na lista de alvos),
# mas evitamos descer neles para economizar tempo.
DEFAULT_PRUNE_DIRS = [
    ".git",
    "node_modules",
    ".venv",
    "venv",
    "env",
]


@dataclass
class RemovalItem:
    kind: str  # "dir" ou "file"
    path: Path


def parse_csv_list(raw: str) -> List[str]:
    raw = raw.strip()
    if not raw:
        return []
    parts = [p.strip() for p in raw.split(",")]
    return [p for p in parts if p]


def is_hidden_path(path: Path) -> bool:
    return path.name.startswith(".")


def is_within_root(root: Path, target: Path) -> bool:
    """Garante que target está dentro do root (segurança)."""
    try:
        target.resolve().relative_to(root.resolve())
        return True
    except Exception:
        return False


def prune_dirs_inplace(dirs: List[str], prune_names: List[str], include_hidden: bool) -> None:
    """Modifica dirs in-place, evitando descer em pastas que não precisamos."""
    prune_set = set(prune_names)
    keep = []
    for d in dirs:
        if d in prune_set:
            continue
        if (not include_hidden) and d.startswith("."):
            continue
        keep.append(d)
    dirs[:] = keep


def collect_targets(
    root: Path,
    target_dirs: List[str],
    target_globs: List[str],
    prune_dirs: List[str],
    include_hidden: bool,
) -> Tuple[List[RemovalItem], List[str]]:
    """
    Varre o root e monta a lista do que deve ser removido.
    Retorna (items, warnings).
    """
    items: List[RemovalItem] = []
    warnings: List[str] = []

    root = root.resolve()

    # 1) Diretórios-alvo (por nome exato) encontrados em qualquer nível
    for dirpath, dirs, files in os.walk(root):
        # Evita entrar em pastas muito grandes/irrelevantes
        prune_dirs_inplace(dirs, prune_dirs, include_hidden)

        # Checa subdirs atuais (nomes) para coletar alvos
        for d in list(dirs):
            if d in target_dirs:
                p = (Path(dirpath) / d)
                # Segurança extra
                if not is_within_root(root, p):
                    warnings.append(f"ignorado (fora do root): {str(p)}")
                    continue
                items.append(RemovalItem(kind="dir", path=p))

        # 2) Arquivos-alvo por glob no diretório atual
        # Observação: glob é aplicado só no nível atual (mais eficiente).
        current = Path(dirpath)
        if (not include_hidden) and is_hidden_path(current) and current != root:
            # Se não incluir ocultos, não coletamos arquivos dentro de pastas ocultas
            # (mesmo que o walk ainda possa chegar aqui em alguns cenários).
            continue

        for pattern in target_globs:
            for fp in current.glob(pattern):
                if fp.is_dir():
                    continue
                if (not include_hidden) and is_hidden_path(fp):
                    continue
                if not is_within_root(root, fp):
                    warnings.append(f"ignorado (fora do root): {str(fp)}")
                    continue
                items.append(RemovalItem(kind="file", path=fp))

    # Dedup (pode acontecer com padrões sobrepostos)
    seen = set()
    unique: List[RemovalItem] = []
    for it in items:
        key = (it.kind, str(it.path.resolve()))
        if key in seen:
            continue
        seen.add(key)
        unique.append(it)

    # Ordena por caminho para saída previsível
    unique.sort(key=lambda x: str(x.path).lower())

    return unique, warnings


def human_bytes(n: int) -> str:
    # Conversão simples para leitura humana (ASCII)
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(n)
    idx = 0
    while size >= 1024.0 and idx < len(units) - 1:
        size /= 1024.0
        idx += 1
    if idx == 0:
        return f"{int(size)} {units[idx]}"
    return f"{size:.2f} {units[idx]}"


def estimate_size(path: Path) -> int:
    """Estimativa de bytes a remover (para diretório soma tudo; para arquivo size)."""
    try:
        if path.is_file():
            return path.stat().st_size
        if path.is_dir():
            total = 0
            for p in path.rglob("*"):
                try:
                    if p.is_file():
                        total += p.stat().st_size
                except Exception:
                    # Se der erro em algum arquivo, ignora só aquele
                    pass
            return total
    except Exception:
        return 0
    return 0


def remove_item(item: RemovalItem) -> Tuple[bool, str]:
    """
    Remove de fato. Retorna (ok, mensagem).
    """
    p = item.path
    try:
        if item.kind == "file":
            if p.exists():
                p.unlink()
            return True, f"removido arquivo: {str(p)}"
        else:
            if p.exists():
                shutil.rmtree(p)
            return True, f"removido diretorio: {str(p)}"
    except Exception as e:
        return False, f"erro removendo {item.kind} {str(p)}: {e}"


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Limpa caches/builds e arquivos lixo de um repositório, com suporte a --dry-run.",
    )
    p.add_argument(
        "--root",
        type=str,
        default=None,
        help="Pasta raiz (default: pasta onde o script esta).",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Apenas lista o que seria removido, sem remover nada.",
    )
    p.add_argument(
        "--yes",
        action="store_true",
        help="Nao pergunta confirmacao (use com cuidado).",
    )
    p.add_argument(
        "--include-hidden",
        action="store_true",
        help="Inclui varredura e alvos dentro de pastas/arquivos ocultos (nome iniciando com '.').",
    )
    p.add_argument(
        "--add-dir",
        action="append",
        default=[],
        help="Adiciona nome de diretorio alvo (pode repetir). Ex: --add-dir .cache",
    )
    p.add_argument(
        "--add-glob",
        action="append",
        default=[],
        help='Adiciona padrao glob de arquivo alvo (pode repetir). Ex: --add-glob "*.sqlite"',
    )
    p.add_argument(
        "--prune-dirs",
        type=str,
        default="",
        help="Lista CSV de nomes de diretorios para nao varrer (default inclui .git,node_modules,venv,etc).",
    )
    return p


def main(argv: List[str]) -> int:
    args = build_arg_parser().parse_args(argv)

    if args.root:
        root = Path(args.root).expanduser()
    else:
        root = Path(__file__).resolve().parent

    root = root.resolve()

    target_dirs = DEFAULT_TARGET_DIRS.copy()
    for d in args.add_dir:
        d = (d or "").strip()
        if d:
            target_dirs.append(d)

    target_globs = DEFAULT_TARGET_GLOBS.copy()
    for g in args.add_glob:
        g = (g or "").strip()
        if g:
            target_globs.append(g)

    prune_dirs = DEFAULT_PRUNE_DIRS.copy()
    extra_prune = parse_csv_list(args.prune_dirs)
    prune_dirs.extend(extra_prune)

    items, warnings = collect_targets(
        root=root,
        target_dirs=target_dirs,
        target_globs=target_globs,
        prune_dirs=prune_dirs,
        include_hidden=bool(args.include_hidden),
    )

    if warnings:
        print("AVISOS:")
        for w in warnings:
            print("  - " + w)
        print("")

    if not items:
        print("Nada para remover.")
        print(f"Root: {str(root)}")
        return 0

    # Estima impacto
    total_bytes = 0
    for it in items:
        total_bytes += estimate_size(it.path)

    print("ALVOS ENCONTRADOS:")
    for it in items:
        kind = "DIR " if it.kind == "dir" else "FILE"
        print(f"  [{kind}] {str(it.path)}")
    print("")
    print(f"Total de itens: {len(items)}")
    print(f"Tamanho estimado a remover: {human_bytes(total_bytes)}")
    print(f"Modo dry-run: {'sim' if args.dry_run else 'nao'}")
    print("")

    if args.dry_run:
        print("DRY-RUN: nenhuma remocao foi feita.")
        return 0

    if not args.yes:
        resp = input("Confirmar remocao? (digite 'sim' para confirmar): ").strip().lower()
        if resp != "sim":
            print("Cancelado. Nenhuma remocao foi feita.")
            return 1

    ok_count = 0
    err_count = 0

    for it in items:
        ok, msg = remove_item(it)
        if ok:
            ok_count += 1
            print(msg)
        else:
            err_count += 1
            print(msg)

    print("")
    print("RESUMO:")
    print(f"Removidos com sucesso: {ok_count}")
    print(f"Falhas: {err_count}")
    print(f"Root: {str(root)}")

    return 0 if err_count == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
