"""Build the portable Skill ZIP from an explicit distribution allowlist."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import tempfile
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

PACKAGE_FILES = (
    "SKILL.md", ".env.example",
    "scripts/cloud_logs.py", "scripts/cloud_log_common.py", "scripts/cloud_log_parsing.py",
    "scripts/cloud_log_correlation.py", "scripts/cloud_log_enrichment.py", "scripts/cloud_log_workflow.py",
    "scripts/cloud_log_reporting.py", "scripts/cloud_log_state.py", "scripts/cloud_log_delivery.py",
    "scripts/package_skill.py", "scripts/requirements.txt",
    "scripts/providers/__init__.py", "scripts/providers/azure_log_analytics.py", "scripts/providers/volcengine_tls.py",
    "references/agent-workflow.md", "references/configuration.md", "references/correlation.md",
    "references/notifications.md", "references/extending.md", "references/azure.md",
    "references/volcengine.md", "references/plugin-api.md",
    "assets/connections.example.json", "assets/notification.txt",
    "assets/profiles/generic.json", "assets/profiles/gateway.json", "assets/profiles/plugin-api.json",
)


def build_package(root, output):
    root, output = Path(root).resolve(), Path(output).resolve()
    if output.suffix.lower() != ".zip":
        raise ValueError("Package output must be a .zip file")
    files = {}
    for name in sorted(PACKAGE_FILES):
        source = (root / name).resolve()
        if not source.is_relative_to(root) or source == output:
            raise ValueError("Invalid package source: " + name)
        text = source.read_text(encoding="utf-8-sig")
        if name == ".env.example":
            for line in text.splitlines():
                if line.strip() and not line.lstrip().startswith("#"):
                    if "=" not in line or line.split("=", 1)[1].strip():
                        raise ValueError(".env.example must contain only empty variable assignments")
        files[name] = text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=output.parent, suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
        with ZipFile(temporary, "w", compression=ZIP_DEFLATED, compresslevel=9) as archive:
            for name, content in files.items():
                info = ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.create_system = 3
                info.external_attr = 0o100644 << 16
                info.compress_type = ZIP_DEFLATED
                archive.writestr(info, content)
        with ZipFile(temporary) as archive:
            if archive.testzip() is not None:
                raise ValueError("ZIP integrity check failed")
        os.replace(temporary, output)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
    return {"path": str(output), "files": len(files), "bytes": output.stat().st_size,
            "sha256": hashlib.sha256(output.read_bytes()).hexdigest()}


def main():
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=root / "dist/dify/cloud-log-assistant.zip")
    args = parser.parse_args()
    print(json.dumps(build_package(root, args.output), ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
