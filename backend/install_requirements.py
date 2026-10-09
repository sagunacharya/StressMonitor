"""Install pinned backend dependencies when requirements.txt changes."""
import argparse
import hashlib
from pathlib import Path
import subprocess
import sys

HERE = Path(__file__).resolve().parent
REQUIREMENTS = HERE / "requirements.txt"
MARKER = HERE / ".venv" / "requirements.sha256"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="Reinstall/resolve pinned requirements even if unchanged")
    args = parser.parse_args()
    digest = hashlib.sha256(REQUIREMENTS.read_bytes()).hexdigest()
    try:
        installed = MARKER.read_text(encoding="ascii").strip()
    except OSError:
        installed = ""
    if installed == digest and not args.force:
        print("Pinned requirements unchanged. Use --force to re-resolve the environment.")
        return 0
    print("Installing pinned Stress Monitor requirements…")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "--upgrade", "-r", str(REQUIREMENTS)])
    MARKER.parent.mkdir(parents=True, exist_ok=True)
    MARKER.write_text(digest + "\n", encoding="ascii")
    print("Requirements installed; manifest hash recorded.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
