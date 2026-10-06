"""Build the dependency-free Firefox extension ZIP (unsigned)."""
from pathlib import Path
import json
import zipfile

root = Path(__file__).resolve().parent.parent
source = root / "extension"
manifest = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
output = root / "dist" / f"application-copilot-{manifest['version']}.zip"
output.parent.mkdir(exist_ok=True)
with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
    for path in sorted(source.rglob("*")):
        if path.is_file():
            archive.write(path, path.relative_to(source))
print(output)
