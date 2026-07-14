#!/usr/bin/env python3
"""Package a built PyChrono install tree into a BlendMotion runtime zip.

Steps:
  1. Locate the `pychrono` package inside the CMake install tree.
  2. Copy a pruned whitelist (wrappers + the 4 native modules + demos) to a
     staging dir, matching the layout of the original BlendMotion runtime zip.
  3. Strip natives (Linux/macOS); ad-hoc codesign AFTER strip on macOS
     (strip invalidates the signature; unsigned binaries are killed at dlopen
     on Apple Silicon).
  4. Smoke-test: import pychrono (+ vehicle/fea/robot) from the staging dir
     using this interpreter, which must match the target Python ABI.
  5. Zip with a flat `pychrono/` directory at the zip root.

Usage:
  package_runtime.py --install-dir <cmake-install-prefix> --plat win_amd64
                     --pytag cp311 [--license path/to/LICENSE] --out dist
"""

import argparse
import os
import shutil
import subprocess
import sys
import zipfile

WRAPPERS = ["__init__.py", "core.py", "fea.py", "robot.py", "vehicle.py"]
NATIVE_STEMS = ["_core", "_fea", "_robot", "_vehicle"]
NATIVE_EXTS = {".pyd", ".so", ".dylib"}


def find_pychrono(install_dir: str) -> str:
    hits = []
    for root, dirs, files in os.walk(install_dir):
        if os.path.basename(root) == "pychrono" and "__init__.py" in files:
            hits.append(root)
        dirs[:] = [d for d in dirs if d != "__pycache__"]
    if not hits:
        sys.exit(f"ERROR: no pychrono package found under {install_dir}")
    if len(hits) > 1:
        print(f"WARNING: multiple pychrono dirs found, using first: {hits}")
    return hits[0]


def is_native(name: str) -> bool:
    stem = name.split(".", 1)[0]
    return stem in NATIVE_STEMS and os.path.splitext(name)[1] in NATIVE_EXTS


def stage_package(src: str, stage_pkg: str, license_path: str) -> list:
    os.makedirs(stage_pkg, exist_ok=True)
    natives = []
    for name in os.listdir(src):
        path = os.path.join(src, name)
        if name in WRAPPERS and os.path.isfile(path):
            shutil.copy2(path, os.path.join(stage_pkg, name))
        elif os.path.isfile(path) and is_native(name):
            shutil.copy2(path, os.path.join(stage_pkg, name))
            natives.append(os.path.join(stage_pkg, name))
        elif name == "demos" and os.path.isdir(path):
            shutil.copytree(path, os.path.join(stage_pkg, "demos"),
                            ignore=shutil.ignore_patterns("__pycache__"))
    missing = [w for w in WRAPPERS if not os.path.exists(os.path.join(stage_pkg, w))]
    if missing:
        sys.exit(f"ERROR: missing wrapper files in built package: {missing}")
    found_stems = {os.path.basename(n).split(".", 1)[0] for n in natives}
    missing_natives = [s for s in NATIVE_STEMS if s not in found_stems]
    if missing_natives:
        sys.exit(f"ERROR: missing native modules in built package: {missing_natives} "
                 f"(contents: {sorted(os.listdir(src))})")
    if license_path and os.path.isfile(license_path):
        shutil.copy2(license_path, os.path.join(stage_pkg, "LICENSE.chrono.txt"))
    return natives


def _run_out(cmd):
    return subprocess.run(cmd, check=True, capture_output=True, text=True).stdout


def fix_linux_libpython(natives: list):
    """Drop the DT_NEEDED libpython entry: inside Blender the interpreter's
    symbols come from the host process, and Blender does not put its bundled
    libpython on the loader path."""
    for n in natives:
        needed = _run_out(["patchelf", "--print-needed", n]).split()
        for dep in needed:
            if dep.startswith("libpython"):
                subprocess.run(["patchelf", "--remove-needed", dep, n], check=True)
        after = _run_out(["patchelf", "--print-needed", n]).split()
        if any(d.startswith("libpython") for d in after):
            sys.exit(f"ERROR: libpython still in DT_NEEDED of {n}: {after}")
        print(f"{os.path.basename(n)} NEEDED: {after}")


def bundle_macos_dylibs(install_dir: str, stage_pkg: str, natives: list) -> list:
    """Copy the shared Chrono dylibs next to the extension modules and rewrite
    every load command that references them to @loader_path/<name>. Also strip
    the hard Python.framework load command (LIEF): Blender resolves Python
    symbols from its own process and end-user Macs need not have python.org
    Python installed."""
    import lief

    libdirs = [os.path.join(install_dir, "lib"), os.path.join(install_dir, "lib64")]

    def find_lib(basename):
        for d in libdirs:
            p = os.path.join(d, basename)
            if os.path.exists(p):
                return os.path.realpath(p)
        return None

    # Transitive closure of Chrono dylib deps referenced by the modules.
    bundled = {}
    queue = list(natives)
    while queue:
        binpath = queue.pop()
        for line in _run_out(["otool", "-L", binpath]).splitlines()[1:]:
            dep = line.strip().split(" (")[0]
            base = os.path.basename(dep)
            if "Chrono" not in base or base in bundled:
                continue
            src = find_lib(base)
            if not src:
                sys.exit(f"ERROR: cannot locate bundled dep {base} under {libdirs}")
            dst = os.path.join(stage_pkg, base)
            shutil.copy2(src, dst)
            bundled[base] = dst
            queue.append(dst)

    all_bins = natives + list(bundled.values())
    for binpath in all_bins:
        for line in _run_out(["otool", "-L", binpath]).splitlines()[1:]:
            dep = line.strip().split(" (")[0]
            base = os.path.basename(dep)
            if base in bundled and dep != f"@loader_path/{base}":
                subprocess.run(["install_name_tool", "-change", dep,
                                f"@loader_path/{base}", binpath], check=True)
        # Remove the absolute Python.framework/libpython load command.
        macho = lief.MachO.parse(binpath)
        changed = False
        for b in macho:
            for lib in list(b.libraries):
                if "Python.framework" in lib.name or "libpython" in lib.name:
                    b.remove(lib)
                    changed = True
        if changed:
            macho.write(binpath)
    for name, path in bundled.items():
        subprocess.run(["install_name_tool", "-id", f"@loader_path/{name}", path],
                       check=True)
    # Hard check: no Python framework reference may survive (the CI runner has
    # the framework installed, so the smoke test alone cannot catch this).
    for binpath in all_bins:
        out = _run_out(["otool", "-L", binpath])
        if "Python.framework" in out or "libpython" in out:
            sys.exit(f"ERROR: Python load command still present in {binpath}:\n{out}")
        print(out.strip())
    return all_bins


def strip_and_sign(install_dir: str, stage_pkg: str, natives: list):
    if sys.platform.startswith("linux"):
        # strip BEFORE patchelf: binutils strip can corrupt a patchelf-edited ELF.
        for n in natives:
            subprocess.run(["strip", "--strip-unneeded", n], check=True)
        fix_linux_libpython(natives)
    elif sys.platform == "darwin":
        all_bins = bundle_macos_dylibs(install_dir, stage_pkg, natives)
        for n in all_bins:
            subprocess.run(["strip", "-x", n], check=True)
        # Re-sign AFTER strip and LIEF edits: both invalidate the ad-hoc
        # signature and Apple Silicon SIGKILLs invalid binaries at dlopen.
        for n in all_bins:
            subprocess.run(["codesign", "--force", "--sign", "-", n], check=True)


def smoke_test(stage_dir: str):
    code = (
        "import sys; sys.path.insert(0, sys.argv[1]); "
        "import pychrono, pychrono.vehicle, pychrono.fea, pychrono.robot; "
        "v = pychrono.ChVector3d(1, 2, 3); "
        "print('smoke test OK:', v.x, v.y, v.z, '| chrono data:', "
        "pychrono.GetChronoDataPath())"
    )
    subprocess.run([sys.executable, "-c", code, stage_dir], check=True)


def make_zip(stage_dir: str, out_zip: str):
    os.makedirs(os.path.dirname(out_zip), exist_ok=True)
    with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for root, dirs, files in os.walk(stage_dir):
            dirs.sort()
            for f in sorted(files):
                full = os.path.join(root, f)
                arc = os.path.relpath(full, stage_dir).replace(os.sep, "/")
                zf.write(full, arc)
    size_mb = os.path.getsize(out_zip) / (1024 * 1024)
    print(f"Wrote {out_zip} ({size_mb:.1f} MB)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--install-dir", required=True)
    ap.add_argument("--plat", required=True,
                    choices=["win_amd64", "linux_x86_64", "macos_arm64",
                             "win_arm64", "linux_arm64", "macos_x86_64"])
    ap.add_argument("--pytag", required=True)
    ap.add_argument("--license", default="")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    runner_tag = f"cp{sys.version_info.major}{sys.version_info.minor}"
    if runner_tag != args.pytag:
        sys.exit(f"ERROR: packaging interpreter is {runner_tag} but target is "
                 f"{args.pytag}; the smoke test would be meaningless.")

    src = find_pychrono(args.install_dir)
    print(f"Found built pychrono at: {src}")

    stage_dir = os.path.abspath("stage")
    if os.path.exists(stage_dir):
        shutil.rmtree(stage_dir)
    stage_pkg = os.path.join(stage_dir, "pychrono")

    natives = stage_package(src, stage_pkg, args.license)
    strip_and_sign(args.install_dir, stage_pkg, natives)
    smoke_test(stage_dir)

    out_zip = os.path.join(args.out, f"pychrono_runtime_{args.plat}_{args.pytag}.zip")
    make_zip(stage_dir, out_zip)


if __name__ == "__main__":
    main()
