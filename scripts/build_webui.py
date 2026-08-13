from __future__ import annotations

import argparse
import hashlib
from pathlib import Path


def write_hashed_asset(source: Path, output: Path) -> str:
    content = source.read_bytes()
    filename = f"{source.stem}.{hashlib.sha256(content).hexdigest()[:12]}{source.suffix}"
    (output / filename).write_bytes(content)
    return filename


def build(output: Path) -> None:
    source = Path(__file__).parents[1] / "webui"
    output.mkdir(parents=True, exist_ok=True)
    script_name = write_hashed_asset(source / "app.js", output)
    style_name = write_hashed_asset(source / "styles.css", output)
    index = (source / "index.html").read_text(encoding="utf-8")
    index = index.replace('href="styles.css"', f'href="{style_name}"')
    index = index.replace('src="app.js"', f'src="{script_name}"')
    (output / "index.html").write_text(index, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path, nargs="?", default=Path("dist/webui"))
    build(parser.parse_args().output)


if __name__ == "__main__":
    main()
