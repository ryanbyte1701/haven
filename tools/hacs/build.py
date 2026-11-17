#!/usr/bin/env python3
"""Build the HACS edition repository (ryanbyte1701/haven-hacs) from this source repository.

    python tools/hacs/build.py --out ../haven-hacs            # write/refresh the generated repository
    python tools/hacs/build.py --out DIR --with-workflows     # also (re)write its .github/workflows (by hand)
    python tools/hacs/build.py --out DIR --check              # exit 1 if DIR is not up to date

What goes where (the allow-list is tools/hacs/manifest.py; the rule is in docs/hacs.md):

    custom_components/haven/            <- tools/hacs/integration/ (the Home Assistant wrapper)
    custom_components/haven/core/       <- app/*.py (default-allow), app/adapters/ (default-deny), app/static/
    custom_components/haven/frontend/   <- panel.js, hacs-shim.js, hacs.css + app/static/index.html rewritten
    custom_components/haven/manifest.json, hacs.json, README.md, images/, LICENSE (if the source has one)

The version (manifest.json, and the vYYYY.M.D.N tag the sync workflow makes) only moves when the generated
files change: a source commit that only touches the Docker edition (a new integration, say) builds the same
repository and keeps the version, so the sync commits nothing. Stdlib only.
"""
from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import json
import re
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
VERSION_PLACEHOLDER = "0.0.0-build"
MANAGED = ("custom_components", "hacs.json", "README.md", "LICENSE", "images")      # what the build owns in --out
SKIP_DIRS = {"__pycache__", ".pytest_cache", "node_modules"}


def load_manifest(source: Path):
    path = source / "tools" / "hacs" / "manifest.py"
    spec = importlib.util.spec_from_file_location("haven_hacs_manifest", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _files(root: Path):
    for p in sorted(root.rglob("*")):
        if p.is_file() and not (set(p.relative_to(root).parts) & SKIP_DIRS) and p.suffix not in (".pyc", ".pyo"):
            yield p


def requirement_specs(source: Path, names: list[str]) -> list[str]:
    """`fastapi==0.163.*` etc., exactly as requirements.txt pins them (the Docker edition's versions)."""
    found = {}
    for line in (source / "requirements.txt").read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        m = re.match(r"^([A-Za-z0-9_.-]+)(\[[^\]]*\])?\s*(.*)$", line)
        if m and m.group(1).lower() in names:
            found[m.group(1).lower()] = m.group(1) + m.group(3).replace(" ", "")
    missing = [n for n in names if n not in found]
    if missing:
        raise SystemExit(f"requirements.txt has no line for {', '.join(missing)} (tools/hacs/manifest.py HACS_REQUIREMENTS)")
    return [found[n] for n in names]


def core_files(source: Path, m) -> dict[str, Path]:
    """Haven's core as shipped in custom_components/haven/core/: {relative path: source file}."""
    app = source / "app"
    out: dict[str, Path] = {}
    for name in m.OG_ONLY_CORE:
        if not (app / name).exists():
            raise SystemExit(f"tools/hacs/manifest.py lists app/{name} in OG_ONLY_CORE but it doesn't exist")
    for p in sorted(app.glob("*.py")):                       # default-allow
        if p.name not in m.OG_ONLY_CORE:
            out[p.name] = p
    for name in sorted(m.HACS_ADAPTERS):                     # default-deny
        p = app / "adapters" / name
        if not p.exists():
            raise SystemExit(f"tools/hacs/manifest.py lists app/adapters/{name} in HACS_ADAPTERS but it doesn't exist")
        out[f"adapters/{name}"] = p
    for p in _files(app / "static"):
        rel = p.relative_to(app / "static").as_posix()
        if rel not in m.OG_ONLY_STATIC:
            out[f"static/{rel}"] = p
    return out


def dashboard_page(source: Path) -> str:
    """app/static/index.html for the panel's frame: Haven's files from /haven_static, the shim first."""
    html = (source / "app" / "static" / "index.html").read_text()
    html = re.sub(r'(["\'])/static/', r"\1/haven_static/", html)
    shim = '<script src="/haven_panel/hacs-shim.js?v={v}"></script>\n'
    head = html.index("<head>") + len("<head>\n")
    first_script = html.find("<script", head)
    at = first_script if first_script != -1 else html.index("</head>")
    html = html[:at] + shim + html[at:]
    html = html.replace("</head>", '<link rel="stylesheet" href="/haven_panel/hacs.css?v={v}">\n</head>', 1)
    html = re.sub(r'((?:src|href)="/haven_static/[^"?]+)"', r'\1?v={v}"', html)     # new version, new files
    return html.replace("{v}", VERSION_PLACEHOLDER)


def manifest_json(m, source: Path) -> str:
    data = {
        "domain": m.DOMAIN,
        "name": m.NAME,
        "codeowners": m.CODEOWNERS,
        "config_flow": True,
        "dependencies": ["frontend", "http", "panel_custom"],
        "documentation": f"https://github.com/{m.HACS_REPO}",
        "integration_type": "service",
        "iot_class": "local_push",
        "issue_tracker": f"https://github.com/{m.SOURCE_REPO}/issues",
        "requirements": requirement_specs(source, m.HACS_REQUIREMENTS),
        "single_config_entry": True,
        "version": VERSION_PLACEHOLDER,
    }
    return json.dumps(data, indent=2) + "\n"


def render(source: Path, with_workflows: bool = False) -> dict[str, bytes]:
    """Every generated file: {path in the HACS repository: content}, with VERSION_PLACEHOLDER for the version."""
    m = load_manifest(source)
    files: dict[str, bytes] = {}
    pkg = f"custom_components/{m.DOMAIN}"
    for p in _files(HERE / "integration"):
        files[f"{pkg}/{p.relative_to(HERE / 'integration').as_posix()}"] = p.read_bytes()
    for rel, p in core_files(source, m).items():
        files[f"{pkg}/core/{rel}"] = p.read_bytes()
    files[f"{pkg}/frontend/index.html"] = dashboard_page(source).encode()
    files[f"{pkg}/manifest.json"] = manifest_json(m, source).encode()
    files["hacs.json"] = (json.dumps({"name": m.NAME, "homeassistant": m.MIN_HOME_ASSISTANT, "render_readme": True},
                                     indent=2) + "\n").encode()
    readme = (HERE / "repo" / "README.md").read_text()
    for k, v in {"source_repo": m.SOURCE_REPO, "hacs_repo": m.HACS_REPO, "min_ha": m.MIN_HOME_ASSISTANT}.items():
        readme = readme.replace("{" + k + "}", v)
    files["README.md"] = readme.encode()
    for p in _files(HERE / "repo" / "images"):
        files[f"images/{p.relative_to(HERE / 'repo' / 'images').as_posix()}"] = p.read_bytes()
    for lic in ("LICENSE", "LICENSE.md", "LICENSE.txt"):
        if (source / lic).exists():
            files["LICENSE"] = (source / lic).read_bytes()
            break
    if with_workflows:
        for p in _files(HERE / "repo" / ".github"):
            files[f".github/{p.relative_to(HERE / 'repo' / '.github').as_posix()}"] = p.read_bytes()
    return files


def _with_version(files: dict[str, bytes], version: str) -> dict[str, bytes]:
    ph = VERSION_PLACEHOLDER.encode()
    return {k: v.replace(ph, version.encode()) if ph in v else v for k, v in files.items()}


def existing(out: Path, managed_extra: tuple[str, ...] = ()) -> dict[str, bytes]:
    files = {}
    for top in MANAGED + managed_extra:
        p = out / top
        if p.is_file():
            files[top] = p.read_bytes()
        elif p.is_dir():
            for f in _files(p):
                files[f.relative_to(out).as_posix()] = f.read_bytes()
    return files


def current_version(out: Path, domain: str) -> str | None:
    try:
        return json.loads((out / "custom_components" / domain / "manifest.json").read_text())["version"]
    except (OSError, ValueError, KeyError):
        return None


def next_version(previous: str | None, today: dt.date) -> str:
    """YYYY.M.D.N: N counts the releases made that day (1, 2, ...)."""
    day = f"{today.year}.{today.month}.{today.day}"
    if previous and previous.startswith(day + "."):
        try:
            return f"{day}.{int(previous.rsplit('.', 1)[1]) + 1}"
        except ValueError:
            pass
    return f"{day}.1"


def build(source: Path, out: Path, *, version: str | None = None, with_workflows: bool = False,
          check: bool = False, today: dt.date | None = None) -> tuple[bool, str]:
    """Write the generated repository into `out`. Returns (changed, version)."""
    m = load_manifest(source)
    files = render(source, with_workflows)
    extra = (".github",) if with_workflows else ()
    before = existing(out, extra)
    prev = current_version(out, m.DOMAIN)
    def published(d):                                        # what HACS installs (workflows aren't)
        return {k: v for k, v in d.items() if not k.startswith(".github/")}
    if version is None and prev and published(_with_version(files, prev)) == published(before):
        version = prev                                       # nothing HACS installs changed: keep the version
    version = version or next_version(prev, today or dt.datetime.now(dt.timezone.utc).date())
    final = _with_version(files, version)
    if final == before:
        return False, version
    if check:
        return True, version
    for top in MANAGED + extra:
        p = out / top
        if p.is_dir():
            shutil.rmtree(p)
        elif p.exists():
            p.unlink()
    for rel, data in sorted(final.items()):
        dest = out / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
    return True, version


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--source", type=Path, default=HERE.parent.parent, help="the haven source checkout")
    ap.add_argument("--out", type=Path, required=True, help="the haven-hacs checkout (or an empty folder)")
    ap.add_argument("--version", help="force this version (default: keep it, or the next YYYY.M.D.N on change)")
    ap.add_argument("--with-workflows", action="store_true", help="also write .github/workflows (pushed by hand)")
    ap.add_argument("--check", action="store_true", help="only report: exit 1 if --out is out of date")
    a = ap.parse_args(argv)
    a.out.mkdir(parents=True, exist_ok=True)
    changed, version = build(a.source.resolve(), a.out.resolve(), version=a.version,
                             with_workflows=a.with_workflows, check=a.check)
    print(f"haven-hacs {version}: {'changed' if changed else 'up to date'}")
    return 1 if (a.check and changed) else 0


if __name__ == "__main__":
    sys.exit(main())
