"""Package the standard-library relay and its Linux installer."""
import io
import os
from pathlib import Path
import tarfile
import tempfile
from zipfile import ZIP_DEFLATED, ZipFile

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent


def server_archive():
    buffer = io.BytesIO()
    package = ROOT / "agent_group_chat"
    with ZipFile(buffer, "w", ZIP_DEFLATED) as archive:
        archive.writestr("__main__.py", "from agent_group_chat.cli import main\nmain()\n")
        for path in sorted(package.rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts and path.suffix in {".py", ".sql", ".html", ".js", ".css", ".md", ".webp"}:
                archive.write(path, path.relative_to(ROOT))
    return buffer.getvalue()


def build():
    dist = ROOT / "dist"
    dist.mkdir(exist_ok=True)
    target = dist / "diesheng-linux.tar.gz"
    with tempfile.NamedTemporaryFile(dir=dist, delete=False) as temp:
        staging = Path(temp.name)
    try:
        with tarfile.open(staging, "w:gz") as archive:
            for name, content in [("relay.pyz", server_archive()), *((name, (HERE / name).read_bytes())
                                  for name in ("setup.py", "README.md", "Dockerfile", "Caddyfile", "compose.yaml"))]:
                entry = tarfile.TarInfo("agent-group-chat-linux/" + name)
                entry.mode, entry.size = 0o644, len(content)
                archive.addfile(entry, io.BytesIO(content))
        staging.chmod(0o644)
        os.replace(staging, target)
    finally:
        staging.unlink(missing_ok=True)
    return target


if __name__ == "__main__":
    print(build())
