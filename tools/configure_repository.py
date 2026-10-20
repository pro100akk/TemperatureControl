"""Set real GitHub metadata once; validate it before HACS publication."""

import argparse
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "custom_components/apartment_comfort/manifest.json"


def repository_name(value):
    value = value.removeprefix("https://github.com/").rstrip("/").removesuffix(".git")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+", value):
        raise ValueError("Expected a GitHub repository URL or OWNER/REPOSITORY")
    return value


def check_metadata(manifest, repository, tag=None):
    if not re.fullmatch(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)", manifest.get("version", "")):
        raise ValueError("Use a release version MAJOR.MINOR.PATCH, not a build identifier")
    url = f"https://github.com/{repository_name(repository)}"
    if manifest.get("documentation") != url or manifest.get("issue_tracker") != f"{url}/issues":
        raise ValueError("Set documentation and issue_tracker for the actual repository before publishing")
    owners = manifest.get("codeowners", [])
    if not owners or not all(re.fullmatch(r"@[A-Za-z0-9][A-Za-z0-9-]*", owner) for owner in owners):
        raise ValueError("Set codeowners to the real maintainer's GitHub username")
    if tag is not None and tag != f"v{manifest['version']}":
        raise ValueError("Release tag must equal v + manifest version")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True, help="Real GitHub URL or OWNER/REPO")
    parser.add_argument("--maintainer", help="GitHub username of the maintainer")
    parser.add_argument("--check", action="store_true", help="Validate without changing files")
    parser.add_argument("--tag", help="Also check release tag matches manifest version")
    args = parser.parse_args()
    try:
        repository = repository_name(args.repository)
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        if not args.check:
            if not args.maintainer:
                raise ValueError("--maintainer is required when configuring metadata")
            manifest.update(documentation=f"https://github.com/{repository}",
                            issue_tracker=f"https://github.com/{repository}/issues",
                            codeowners=[f"@{args.maintainer.lstrip('@')}"])
        check_metadata(manifest, repository, args.tag)
        if not args.check:
            MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"GitHub metadata valid: {repository}")
    except (ValueError, KeyError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
