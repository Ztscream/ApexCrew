from __future__ import annotations

import argparse
import shutil
from pathlib import Path


def build(output: Path) -> None:
    source = Path(__file__).parents[1] / "webui"
    output.mkdir(parents=True, exist_ok=True)
    for name in ("index.html", "app.js", "styles.css"):
        shutil.copyfile(source / name, output / name)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path, nargs="?", default=Path("dist/webui"))
    build(parser.parse_args().output)


if __name__ == "__main__":
    main()
