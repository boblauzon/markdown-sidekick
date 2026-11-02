"""Write and sign the update information for a release (spec R17, R21).

    python tools/release_manifest.py installer/Output/MarkdownSidekick-Setup-1.4.0.exe \
        --notes "One paragraph about what changed." [--important]

Writes, next to the installer:
  MarkdownSidekick-Update-1.4.0.exe   the same bytes under the name in-app
                                      updates download (GitHub then counts
                                      updates apart from fresh installs)
  latest.json, latest.json.sig        upload all three to the release

The key is read from %USERPROFILE%\\.markdown-sidekick-release\\update-signing-key.pem
(see docs/release-signing.md). The script refuses an installer whose version
doesn't match the package's __version__, and checks its own signature with the
public keys the app ships before writing anything.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import shutil
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from markdown_sidekick import __version__, updates  # noqa: E402

KEY_FILE = Path.home() / ".markdown-sidekick-release" / "update-signing-key.pem"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("installer", type=Path)
    ap.add_argument("--notes", required=True, help="one paragraph shown under What's new")
    ap.add_argument("--important", action="store_true", help="a privacy or security fix: stronger wording")
    ap.add_argument("--key", type=Path, default=KEY_FILE)
    ap.add_argument("--base-url", default=f"https://github.com/{updates.REPO}/releases/download",
                    help="where release assets live (tests point this at a local server)")
    args = ap.parse_args(argv)

    m = re.search(r"Setup-(\d+(?:\.\d+)+)\.exe$", args.installer.name)
    if not m:
        ap.error("expected an installer named MarkdownSidekick-Setup-X.Y.Z.exe")
    version = m.group(1)
    if version != __version__:
        ap.error(f"the installer is {version} but the package is {__version__}: build the release first")

    from cryptography.hazmat.primitives import serialization

    key = serialization.load_pem_private_key(args.key.read_bytes(), password=None)
    data = args.installer.read_bytes()
    update_name = f"MarkdownSidekick-Update-{version}.exe"
    manifest = {
        "version": version,
        "published": date.today().isoformat(),
        "channel": "stable",
        "min_updater": 1,
        "important": bool(args.important),
        "notes": " ".join(args.notes.split()),
        "installer": {
            "name": update_name,
            "url": f"{args.base_url.rstrip('/')}/v{version}/{update_name}",
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        },
    }
    body = (json.dumps(manifest, indent=2) + "\n").encode("utf-8")
    signature = base64.b64encode(key.sign(body)) + b"\n"
    updates.verify(body, signature)  # the app's own keys accept it, or this raises

    out = args.installer.parent
    shutil.copyfile(args.installer, out / update_name)
    (out / "latest.json").write_bytes(body)
    (out / "latest.json.sig").write_bytes(signature)
    print(f"wrote {update_name}, latest.json and latest.json.sig in {out}")
    print("upload all three to the release, alongside the installer and the zip")
    return 0


if __name__ == "__main__":
    sys.exit(main())
