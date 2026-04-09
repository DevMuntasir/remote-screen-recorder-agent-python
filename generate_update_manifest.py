import argparse
import hashlib
import json
import os
from datetime import datetime, timezone


def compute_sha256(file_path):
    digest = hashlib.sha256()
    with open(file_path, "rb") as file:
        while True:
            chunk = file.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest().lower()


def read_version_from_file(version_file_path):
    if not version_file_path:
        return ""
    if not os.path.exists(version_file_path):
        return ""

    try:
        with open(version_file_path, "r", encoding="utf-8") as file:
            return file.read().strip()
    except OSError:
        return ""


def resolve_version(explicit_version, exe_path, explicit_version_file):
    if explicit_version:
        return explicit_version.strip()

    if explicit_version_file:
        version_value = read_version_from_file(explicit_version_file)
        if version_value:
            return version_value

    exe_dir = os.path.dirname(os.path.abspath(exe_path))
    default_version_file = os.path.join(exe_dir, "AGENT_VERSION.txt")
    version_value = read_version_from_file(default_version_file)
    if version_value:
        return version_value

    raise RuntimeError(
        "Unable to determine version. Pass --version or provide AGENT_VERSION.txt next to the exe."
    )


def write_json_atomic(output_path, payload):
    output_dir = os.path.dirname(os.path.abspath(output_path))
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)

    temp_output_path = output_path + ".tmp"
    with open(temp_output_path, "w", encoding="utf-8") as file:
        json.dump(payload, file, indent=2)
        file.write("\n")

    os.replace(temp_output_path, output_path)


def parse_args():
    parser = argparse.ArgumentParser(description="Generate latest.json update manifest for RemoteAgent")
    parser.add_argument("--exe", required=True, help="Path to RemoteAgent.exe")
    parser.add_argument("--url", required=True, help="Public download URL for the exe")
    parser.add_argument("--version", default="", help="Version string (optional)")
    parser.add_argument(
        "--version-file",
        default="",
        help="Optional path to AGENT_VERSION.txt (if omitted, tries next to --exe)",
    )
    parser.add_argument(
        "--output",
        default="latest.json",
        help="Output manifest path (default: latest.json)",
    )
    return parser.parse_args()


def main():
    args = parse_args()

    exe_path = os.path.abspath(args.exe)
    if not os.path.exists(exe_path):
        raise RuntimeError(f"Exe not found: {exe_path}")

    version_value = resolve_version(args.version, exe_path, args.version_file)
    sha256_value = compute_sha256(exe_path)
    file_size = os.path.getsize(exe_path)

    manifest = {
        "version": version_value,
        "url": args.url.strip(),
        "sha256": sha256_value,
        "size": file_size,
        "generatedAtUtc": datetime.now(timezone.utc).isoformat(),
    }

    write_json_atomic(os.path.abspath(args.output), manifest)

    print("Manifest generated successfully")
    print(f"output={os.path.abspath(args.output)}")
    print(f"version={version_value}")
    print(f"size={file_size}")
    print(f"sha256={sha256_value}")


if __name__ == "__main__":
    main()
