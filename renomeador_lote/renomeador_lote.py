"""
renomeador_lote.py

Renomeador em lote para arquivos e/ou pastas, com opções comuns:
- Trocar espaços por "_"
- Slugify (normaliza acentos, força lowercase, remove caracteres "estranhos")
- Prefixo / Sufixo
- Regex replace (com grupos), ex: --regex "IMG_(\\d+)" --to "foto-\\1"

Segurança:
- --dry-run para apenas mostrar o que faria
- Detecta conflitos de nome (dois itens virando o mesmo nome)
- Não sai do root informado
- Opção --recursive para varrer subpastas
- Opção --include-dirs/--include-files para controlar o que renomeia

Exemplos:
  python renomeador_lote.py --root .
  python renomeador_lote.py --root . --spaces-to-underscore --dry-run
  python renomeador_lote.py --root . --slugify --lower --dry-run
  python renomeador_lote.py --root . --prefix "2025_" --suffix "_old" --dry-run
  python renomeador_lote.py --root . --regex "IMG_(\\d+)" --to "foto-\\1" --dry-run
  python renomeador_lote.py --root . --recursive --include-dirs --slugify --yes
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple


DEFAULT_PRUNE_DIRS = [
    ".git",
    "node_modules",
    ".venv",
    "venv",
    "env",
    "__pycache__",
    "dist",
    "build",
]


@dataclass
class RenamePlan:
    src: Path
    dst: Path


def parse_csv_list(raw: str) -> List[str]:
    raw = (raw or "").strip()
    if not raw:
        return []
    parts = [p.strip() for p in raw.split(",")]
    return [p for p in parts if p]


def is_hidden_path(path: Path) -> bool:
    return path.name.startswith(".")


def is_within_root(root: Path, target: Path) -> bool:
    try:
        target.resolve().relative_to(root.resolve())
        return True
    except Exception:
        return False


def prune_dirs_inplace(dirs: List[str], prune_names: List[str], include_hidden: bool) -> None:
    prune_set = set(prune_names)
    keep = []
    for d in dirs:
        if d in prune_set:
            continue
        if (not include_hidden) and d.startswith("."):
            continue
        keep.append(d)
    dirs[:] = keep


def strip_accents(s: str) -> str:
    # Remove acentos: "ação" -> "acao"
    nkfd = unicodedata.normalize("NFKD", s)
    return "".join(ch for ch in nkfd if not unicodedata.combining(ch))


def slugify_name(
    name: str,
    lower: bool,
    spaces_to_underscore: bool,
    keep_dots: bool,
) -> str:
    """
    Slugify "amigável":
    - Remove acentos
    - Troca espaços por underscore (se habilitado)
    - Remove caracteres fora de [a-zA-Z0-9._-] (ou [a-zA-Z0-9_-] se keep_dots=False)
    - Colapsa múltiplos '_' e '-' e espaços
    """
    s = strip_accents(name)

    if lower:
        s = s.lower()

    if spaces_to_underscore:
        s = s.replace(" ", "_")
    else:
        s = s.replace(" ", "-")

    # Permite letras/numeros/underscore/hifen e opcionalmente ponto
    allowed = r"[^a-zA-Z0-9_\-\.]" if keep_dots else r"[^a-zA-Z0-9_\-]"
    s = re.sub(allowed, "", s)

    # Colapsa repetidos
    s = re.sub(r"_{2,}", "_", s)
    s = re.sub(r"-{2,}", "-", s)

    # Remove underscores/hifens no começo/fim
    s = s.strip("_-")

    # Se ficou vazio, devolve algo seguro
    return s if s else "item"


def apply_transformations(
    original_name: str,
    spaces_to_underscore: bool,
    slugify: bool,
    lower: bool,
    prefix: str,
    suffix: str,
    regex_pattern: Optional[str],
    regex_to: Optional[str],
    keep_extension: bool,
) -> str:
    """
    Aplica as transformações na ordem:
    1) regex replace (se fornecido)
    2) spaces_to_underscore (se fornecido e slugify=Falso) ou slugify (se slugify=Verdadeiro)
    3) lower (se fornecido e slugify=Falso) (slugify já usa lower opcional)
    4) prefix/suffix

    keep_extension:
    - Para arquivos, por padrão preservamos extensão e aplicamos regras no "stem" (nome sem extensão),
      exceto quando o usuário pede --no-keep-extension.
    """
    name = original_name

    # Regex replace primeiro (no nome completo)
    if regex_pattern:
        try:
            name = re.sub(regex_pattern, regex_to or "", name)
        except re.error:
            # Se regex é inválida, vamos deixar estourar no main com validação antes
            pass

    # Transformações de slug/space/lower
    if slugify:
        # slugify completo (nome)
        name = slugify_name(
            name,
            lower=lower,
            spaces_to_underscore=spaces_to_underscore,
            keep_dots=True,  # no slugify do "nome completo" aqui, mantemos pontos
        )
    else:
        if spaces_to_underscore:
            name = name.replace(" ", "_")
        if lower:
            name = name.lower()

    # Prefixo/sufixo
    if prefix:
        name = prefix + name
    if suffix:
        name = name + suffix

    # Sanitização final mínima: não permitir vazio
    name = name.strip()
    if not name:
        name = "item"

    return name


def build_rename_plans(
    root: Path,
    recursive: bool,
    include_files: bool,
    include_dirs: bool,
    include_hidden: bool,
    prune_dirs: List[str],
    spaces_to_underscore: bool,
    slugify: bool,
    lower: bool,
    prefix: str,
    suffix: str,
    regex_pattern: Optional[str],
    regex_to: Optional[str],
    no_keep_extension: bool,
) -> Tuple[List[RenamePlan], List[str]]:
    """
    Monta planos de renomeação.
    Importante: para renomear pastas de forma segura em modo recursivo,
    a execução deve ocorrer do mais profundo para o mais raso.
    """
    warnings: List[str] = []
    plans: List[RenamePlan] = []

    root = root.resolve()

    # Se não for recursivo, só considera items do 1º nível
    if not recursive:
        entries = list(root.iterdir())
        for p in entries:
            if (not include_hidden) and is_hidden_path(p):
                continue
            if p.is_dir() and not include_dirs:
                continue
            if p.is_file() and not include_files:
                continue

            new_name = compute_new_name_for_path(
                p=p,
                spaces_to_underscore=spaces_to_underscore,
                slugify=slugify,
                lower=lower,
                prefix=prefix,
                suffix=suffix,
                regex_pattern=regex_pattern,
                regex_to=regex_to,
                no_keep_extension=no_keep_extension,
            )

            if new_name == p.name:
                continue

            dst = p.with_name(new_name)
            plans.append(RenamePlan(src=p, dst=dst))

        # Ordena com dirs depois? aqui não precisa
        plans.sort(key=lambda x: str(x.src).lower())
        return plans, warnings

    # Recursivo: os.walk
    for dirpath, dirs, files in os.walk(root):
        prune_dirs_inplace(dirs, prune_dirs, include_hidden)

        current = Path(dirpath)

        # Arquivos
        if include_files:
            for fn in files:
                p = current / fn
                if (not include_hidden) and is_hidden_path(p):
                    continue
                if not is_within_root(root, p):
                    warnings.append(f"ignorado (fora do root): {str(p)}")
                    continue

                new_name = compute_new_name_for_path(
                    p=p,
                    spaces_to_underscore=spaces_to_underscore,
                    slugify=slugify,
                    lower=lower,
                    prefix=prefix,
                    suffix=suffix,
                    regex_pattern=regex_pattern,
                    regex_to=regex_to,
                    no_keep_extension=no_keep_extension,
                )
                if new_name != p.name:
                    plans.append(RenamePlan(src=p, dst=p.with_name(new_name)))

        # Pastas
        if include_dirs:
            for d in dirs:
                p = current / d
                if (not include_hidden) and is_hidden_path(p):
                    continue
                if not is_within_root(root, p):
                    warnings.append(f"ignorado (fora do root): {str(p)}")
                    continue

                new_name = compute_new_name_for_path(
                    p=p,
                    spaces_to_underscore=spaces_to_underscore,
                    slugify=slugify,
                    lower=lower,
                    prefix=prefix,
                    suffix=suffix,
                    regex_pattern=regex_pattern,
                    regex_to=regex_to,
                    no_keep_extension=no_keep_extension,
                )
                if new_name != p.name:
                    plans.append(RenamePlan(src=p, dst=p.with_name(new_name)))

    # Ordenação dos planos:
    # - renomear arquivos antes das pastas pode evitar que o caminho "quebre"
    # - renomear pastas do mais profundo para o mais raso evita tentar renomear pai
    #   antes de filho.
    def sort_key(plan: RenamePlan) -> Tuple[int, int, str]:
        is_dir = 1 if plan.src.is_dir() else 0
        depth = len(plan.src.parts)
        # arquivos primeiro (is_dir=0), depois dirs (is_dir=1),
        # e dirs em ordem de profundidade decrescente.
        return (is_dir, -depth, str(plan.src).lower())

    plans.sort(key=sort_key)
    return plans, warnings


def compute_new_name_for_path(
    p: Path,
    spaces_to_underscore: bool,
    slugify: bool,
    lower: bool,
    prefix: str,
    suffix: str,
    regex_pattern: Optional[str],
    regex_to: Optional[str],
    no_keep_extension: bool,
) -> str:
    """
    Define como renomear:
    - Para arquivos: por padrão preserva extensão e aplica regras só no "stem"
      (nome sem extensão). Isso evita quebrar tipos.
    - Para pastas: aplica regras no nome inteiro.
    """
    if p.is_file() and (not no_keep_extension):
        stem = p.stem
        ext = p.suffix  # inclui o ponto
        new_stem = apply_transformations(
            original_name=stem,
            spaces_to_underscore=spaces_to_underscore,
            slugify=slugify,
            lower=lower,
            prefix=prefix,
            suffix=suffix,
            regex_pattern=regex_pattern,
            regex_to=regex_to,
            keep_extension=True,
        )
        return new_stem + ext

    # Pastas ou arquivos sem preservar extensão
    return apply_transformations(
        original_name=p.name,
        spaces_to_underscore=spaces_to_underscore,
        slugify=slugify,
        lower=lower,
        prefix=prefix,
        suffix=suffix,
        regex_pattern=regex_pattern,
        regex_to=regex_to,
        keep_extension=False,
    )


def detect_conflicts(plans: List[RenamePlan]) -> List[str]:
    """
    Detecta conflitos:
    - Dois itens diferentes indo para o mesmo destino
    - Destino já existe e não é o próprio item
    """
    errors: List[str] = []
    seen: Dict[str, Path] = {}

    for plan in plans:
        dst_key = str(plan.dst.resolve())
        src_key = str(plan.src.resolve())

        if dst_key in seen and str(seen[dst_key].resolve()) != src_key:
            errors.append(
                f"conflito: '{str(plan.src)}' e '{str(seen[dst_key])}' virariam '{str(plan.dst)}'"
            )
        else:
            seen[dst_key] = plan.src

        if plan.dst.exists() and plan.dst.resolve() != plan.src.resolve():
            errors.append(
                f"destino ja existe: '{str(plan.dst)}' (origem: '{str(plan.src)}')"
            )

    return errors


def execute_plans(plans: List[RenamePlan]) -> Tuple[int, int]:
    ok = 0
    err = 0
    for plan in plans:
        try:
            plan.src.rename(plan.dst)
            ok += 1
            print(f"renomeado: {str(plan.src)} -> {str(plan.dst)}")
        except Exception as e:
            err += 1
            print(f"erro: {str(plan.src)} -> {str(plan.dst)} ({e})")
    return ok, err


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Renomeador em lote (arquivos/pastas) com spaces->underscore, slugify, prefixo/sufixo e regex replace.",
    )
    p.add_argument("--root", type=str, default=None, help="Pasta raiz (default: pasta do script).")
    p.add_argument("--recursive", action="store_true", help="Varrer subpastas recursivamente.")
    p.add_argument("--dry-run", action="store_true", help="Apenas mostra o plano, sem renomear.")
    p.add_argument("--yes", action="store_true", help="Nao pergunta confirmacao.")
    p.add_argument("--include-hidden", action="store_true", help="Inclui itens ocultos (nome iniciando com '.').")

    p.add_argument("--include-files", action="store_true", help="Renomeia arquivos.")
    p.add_argument("--include-dirs", action="store_true", help="Renomeia pastas.")

    p.add_argument("--spaces-to-underscore", action="store_true", help="Troca espacos por '_'.")

    p.add_argument(
        "--slugify",
        action="store_true",
        help="Aplica slugify (remove acentos, remove caracteres especiais, etc).",
    )
    p.add_argument("--lower", action="store_true", help="Forca lowercase (tambem afeta slugify).")

    p.add_argument("--prefix", type=str, default="", help="Prefixo a adicionar no nome.")
    p.add_argument("--suffix", type=str, default="", help="Sufixo a adicionar no nome.")

    p.add_argument("--regex", type=str, default="", help=r"Regex para substituir no nome. Ex: IMG_(\d+)")
    p.add_argument("--to", type=str, default="", help=r"Substituicao (pode usar grupos). Ex: foto-\1")

    p.add_argument(
        "--no-keep-extension",
        action="store_true",
        help="Nao preserva extensao para arquivos (aplica regras no nome inteiro).",
    )

    p.add_argument(
        "--prune-dirs",
        type=str,
        default="",
        help="Lista CSV de diretorios para nao varrer (default inclui .git,node_modules,venv,etc).",
    )

    return p


def main(argv: List[str]) -> int:
    args = build_arg_parser().parse_args(argv)

    if args.root:
        root = Path(args.root).expanduser()
    else:
        root = Path(__file__).resolve().parent
    root = root.resolve()

    # Se o usuário não especificar include-files/dirs, default é ambos
    include_files = bool(args.include_files)
    include_dirs = bool(args.include_dirs)
    if not include_files and not include_dirs:
        include_files = True
        include_dirs = True

    prune_dirs = DEFAULT_PRUNE_DIRS.copy()
    prune_dirs.extend(parse_csv_list(args.prune_dirs))

    regex_pattern = args.regex.strip() or None
    regex_to = args.to

    # Valida regex cedo
    if regex_pattern:
        try:
            re.compile(regex_pattern)
        except re.error as e:
            print(f"Regex invalida: {e}")
            return 2

    plans, warnings = build_rename_plans(
        root=root,
        recursive=bool(args.recursive),
        include_files=include_files,
        include_dirs=include_dirs,
        include_hidden=bool(args.include_hidden),
        prune_dirs=prune_dirs,
        spaces_to_underscore=bool(args.spaces_to_underscore),
        slugify=bool(args.slugify),
        lower=bool(args.lower),
        prefix=args.prefix or "",
        suffix=args.suffix or "",
        regex_pattern=regex_pattern,
        regex_to=regex_to,
        no_keep_extension=bool(args.no_keep_extension),
    )

    if warnings:
        print("AVISOS:")
        for w in warnings:
            print("  - " + w)
        print("")

    if not plans:
        print("Nada para renomear.")
        print(f"Root: {str(root)}")
        return 0

    conflicts = detect_conflicts(plans)
    if conflicts:
        print("ERROS (conflitos detectados):")
        for c in conflicts:
            print("  - " + c)
        print("")
        print("Resolva os conflitos e rode novamente.")
        return 3

    print("PLANO DE RENOMEACAO:")
    for plan in plans:
        print(f"  {str(plan.src)} -> {str(plan.dst)}")
    print("")
    print(f"Total: {len(plans)}")
    print(f"Modo dry-run: {'sim' if args.dry_run else 'nao'}")
    print("")

    if args.dry_run:
        print("DRY-RUN: nenhuma alteracao foi feita.")
        return 0

    if not args.yes:
        resp = input("Confirmar renomeacao? (digite 'sim' para confirmar): ").strip().lower()
        if resp != "sim":
            print("Cancelado. Nenhuma alteracao foi feita.")
            return 1

    ok, err = execute_plans(plans)

    print("")
    print("RESUMO:")
    print(f"Renomeados com sucesso: {ok}")
    print(f"Falhas: {err}")
    print(f"Root: {str(root)}")

    return 0 if err == 0 else 4


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
