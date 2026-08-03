#!/usr/bin/env python3
"""
Scan every installed addon's __manifest__.py for a declared
external_dependencies['python'] list (Odoo's own manifest mechanism for
exactly this -- third-party pip packages a module needs), and check
whether each one actually imports inside the running web container.

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


def find_python_deps(addons_path):
    """module_name -> [python package names]"""
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
        if py_deps:
            deps[module] = py_deps
    return deps


def check_importable(container, package):
    result = subprocess.run(
        ["docker", "exec", container, "python3", "-c", f"import {package}"],
        capture_output=True, text=True,
    )
    return result.returncode == 0


def main():
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(1)
    addons_path, container = sys.argv[1], sys.argv[2]

    deps = find_python_deps(addons_path)
    print(f"Found {sum(len(v) for v in deps.values())} declared python dependencies across {len(deps)} modules\n")

    missing = {}
    for module, packages in sorted(deps.items()):
        for pkg in packages:
            import_name = pkg.replace("-", "_")
            ok = check_importable(container, import_name)
            status = "OK" if ok else "MISSING"
            print(f"  [{status}] {module}: {pkg}")
            if not ok:
                missing.setdefault(pkg, []).append(module)

    print()
    if missing:
        print(f"=== {len(missing)} missing package(s) -- add to build/docker/Dockerfile.odoo ===")
        for pkg, modules in missing.items():
            print(f"  pip3 install {pkg}   # needed by: {', '.join(modules)}")
        sys.exit(1)
    else:
        print("=== All declared python dependencies are satisfied ===")


if __name__ == "__main__":
    main()
