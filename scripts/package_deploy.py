"""Create a source-only deployment ZIP without credentials or research data."""
from __future__ import annotations

import argparse
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parents[1]


def package(destination: Path) -> list[str]:
    sources = [ROOT / "requirements.txt", ROOT / "startup.sh"]
    sources += sorted((ROOT / "app").rglob("*.py"))
    sources += sorted((ROOT / "app" / "static").rglob("*"))
    sources += sorted((ROOT / "config").glob("*.json"))
    sources += sorted((ROOT / "resources" / "worksheets").rglob("*.pdf"))
    sources = [p for p in sources if p.is_file() and "__pycache__" not in p.parts]
    missing = [str(p.relative_to(ROOT)) for p in [ROOT / "requirements.txt", ROOT / "startup.sh", ROOT / "app" / "main.py"] if not p.is_file()]
    if missing:
        raise SystemExit("必須ファイルがありません: " + ", ".join(missing))
    destination = destination.resolve()
    if destination.suffix.lower() != ".zip":
        raise SystemExit("出力先は .zip にしてください。")
    if destination.exists():
        raise SystemExit(f"既存のZIPは上書きしません: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    names = []
    with ZipFile(destination, "x", compression=ZIP_DEFLATED) as archive:
        for source in sources:
            name = source.relative_to(ROOT).as_posix()
            archive.write(source, name)
            names.append(name)
    return names


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts" / "deploy.zip")
    args = parser.parse_args()
    names = package(args.output)
    print(f"Created {args.output.resolve()} ({len(names)} files)")
    print("Included: app code/static assets, config/*.json, resources/worksheets/*.pdf, requirements.txt, startup.sh")
    print("Excluded: .env, local data, photographs, test artifacts, virtual environments")


if __name__ == "__main__":
    main()
