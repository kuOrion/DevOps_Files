#!/usr/bin/env python3
"""
Scan every installed addon's __manifest__.py for declared
external_dependencies (Odoo's own manifest mechanism for exactly this) --
both the 'python' list (third-party pip packages) and the 'deb'/'bin'
list (system packages/binaries, e.g. report_py3o's 'deb': ['libreoffice'])
-- and check whether each one actually resolves inside the running web
container.

Originally python-only (found live, 2026-08-05: report_py3o declares both
'python' AND 'deb' keys in the same external_dependencies dict, but this
script only ever read 'python' -- so the missing libreoffice runtime went
undetected by the exact tool that exists to catch this class of gap,
until it silently broke every py3o PDF report). Extending to cover 'deb'/
'bin' closes that blind spot for good, not just for libreoffice.

This is the general version of the py3o.formats gap: rather than reacting
to each missing package one at a time after it causes an outage, this
finds every module's declared-but-possibly-unmet dependency in one pass.

Run whenever the addons checkout changes (new module added/updated), or
before a demo -- not on every dev-start.sh boot, since the answer rarely
changes and a per-boot scan would just slow down every start for no
benefit. Anything it finds missing belongs in build/docker/Dockerfile.odoo,
not a runtime patch.

Usage: ./check_missing_deps.py <addons_path> <container_name>
"""
import ast
import subprocess
import sys
from pathlib import Path


def find_deps(addons_path):
    """module_name -> {"python": [...], "system": [...]}"""
    deps = {}
    for manifest_path in Path(addons_path).glob("*/__manifest__.py"):
        module = manifest_path.parent.name
        try:
            tree = ast.parse(manifest_path.read_text())
            manifest = ast.literal_eval(tree.body[0].value) if isinstance(tree.body[0], ast.Expr) else None
        except Exception as e:
            print(f"WARNING: could not parse {manifest_path}: {e}")
            continue
        if not isinstance(manifest, dict):
            continue
        ext_deps = manifest.get("external_dependencies", {})
        py_deps = ext_deps.get("python", [])
        # Odoo manifests use either key interchangeably depending on author
        # convention -- check both, since we've now seen 'deb' in the wild.
        system_deps = ext_deps.get("deb", []) + ext_deps.get("bin", [])
        if py_deps or system_deps:
            deps[module] = {"python": py_deps, "system": system_deps}
    return deps


def check_importable(container, package):
    result = subprocess.run(
        ["docker", "exec", container, "python3", "-c", f"import {package}"],
        capture_output=True, text=True,
    )
    return result.returncode == 0


def check_deb_installed(container, package):
    """dpkg -s matches what `apt-get install <package>` in the Dockerfile
    actually satisfies -- more reliable than guessing the resulting binary's
    name (e.g. the 'libreoffice' package doesn't ship a binary called
    'libreoffice', it's 'soffice')."""
    result = subprocess.run(
        ["docker", "exec", container, "dpkg", "-s", package],
        capture_output=True, text=True,
    )
    return result.returncode == 0


def main():
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(1)
    addons_path, container = sys.argv[1], sys.argv[2]

    deps = find_deps(addons_path)
    n_python = sum(len(v["python"]) for v in deps.values())
    n_system = sum(len(v["system"]) for v in deps.values())
    print(f"Found {n_python} declared python + {n_system} declared system dependencies across {len(deps)} modules\n")

    missing_python = {}
    missing_system = {}
    for module, d in sorted(deps.items()):
        for pkg in d["python"]:
            import_name = pkg.replace("-", "_")
            ok = check_importable(container, import_name)
            print(f"  [{'OK' if ok else 'MISSING'}] {module}: python:{pkg}")
            if not ok:
                missing_python.setdefault(pkg, []).append(module)
        for pkg in d["system"]:
            ok = check_deb_installed(container, pkg)
            print(f"  [{'OK' if ok else 'MISSING'}] {module}: deb:{pkg}")
            if not ok:
                missing_system.setdefault(pkg, []).append(module)

    print()
    if missing_python or missing_system:
        print(f"=== {len(missing_python) + len(missing_system)} missing dependenc(y/ies) -- add to build/docker/Dockerfile.odoo ===")
        for pkg, modules in missing_python.items():
            print(f"  pip3 install {pkg}   # needed by: {', '.join(modules)}")
        for pkg, modules in missing_system.items():
            print(f"  apt-get install -y {pkg}   # needed by: {', '.join(modules)}")
        sys.exit(1)
    else:
        print("=== All declared dependencies are satisfied ===")


if __name__ == "__main__":
    main()
