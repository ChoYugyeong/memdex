"""Build a relocatable Windows app from an installed CPython and the Memdex desktop extra.

Run with Windows CPython 3.10+ and pip: python packaging/windows/build.py dist/Memdex-Studio
"""
import argparse
import hashlib
from importlib.metadata import distribution
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import zipfile

from pip._vendor.packaging.requirements import Requirement


REPO = Path(__file__).resolve().parents[2]


def copy_file(source, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)


def include_dependencies(name, target, included):
    dist = distribution(name)
    key = dist.metadata["Name"].lower()
    if key in included:
        return
    included.add(key)
    for file in dist.files or ():
        if file.is_absolute() or ".." in file.parts or "__pycache__" in file.parts:
            continue
        # Install just the wheel payload and notices; omit machine-specific install metadata.
        if file.name not in ("direct_url.json", "RECORD", "INSTALLER", "REQUESTED"):
            copy_file(dist.locate_file(file), target / file)
    for dependency in dist.requires or ():
        req = Requirement(dependency)
        if not req.marker or req.marker.evaluate({"extra": ""}):
            include_dependencies(req.name, target, included)


def copy_runtime(runtime):
    base = Path(sys.base_prefix)
    tag = f"python{sys.version_info.major}{sys.version_info.minor}"
    for name in ("python.exe", "pythonw.exe", "python3.dll", tag + ".dll", "LICENSE.txt"):
        copy_file(base / name, runtime / name)
    for dll in base.glob("vcruntime*.dll"):
        copy_file(dll, runtime / dll.name)
    for file in (base / "DLLs").iterdir():
        if file.suffix.lower() in (".dll", ".pyd"):
            copy_file(file, runtime / "DLLs" / file.name)
    # Import from this bundle even when another Python or PYTHONPATH is configured.
    (runtime / (tag + "._pth")).write_text("Lib\nDLLs\nLib/site-packages\n", encoding="ascii")
    for item in (base / "Lib").iterdir():
        if item.stem not in sys.stdlib_module_names or item.stem in {"test", "idlelib", "tkinter", "turtledemo", "ensurepip"}:
            continue
        target = runtime / "Lib" / item.name
        if item.is_dir():
            shutil.copytree(item, target, ignore=shutil.ignore_patterns("__pycache__", "test", "tests"))
        elif item.suffix == ".py":
            copy_file(item, target)
    packages = runtime / "Lib" / "site-packages"
    for file in (REPO / "memdex").glob("*.py"):
        copy_file(file, packages / "memdex" / file.name)
    shutil.copytree(REPO / "memdex/board_assets", packages / "memdex/board_assets")
    return packages


def webview_app(output):
    compiler = Path(os.environ["SystemRoot"], "Microsoft.NET", "Framework", "v4.0.30319", "csc.exe")
    output.mkdir(parents=True)
    subprocess.run([str(compiler), "/nologo", "/target:winexe", "/reference:System.Windows.Forms.dll",
                    f"/win32icon:{REPO / 'memdex/board_assets/studio.ico'}",
                    f"/out:{output / 'Memdex-Studio.exe'}", str(Path(__file__).with_name("Studio.cs"))], check=True)
    runtime = output / "runtime"
    include_dependencies("pywebview", copy_runtime(runtime), set())
    # Exercise the relocated CLR and WebView2 assemblies before publishing the ZIP.
    subprocess.run([str(runtime / "python.exe"), "-I", "-B", "-c",
                    "from memdex import desktop; from webview.platforms import edgechromium"],
                   cwd=output, check=True)


def electron_app(output):
    studio = REPO / "studio"
    modules = studio / "node_modules"
    binaries = modules / "electron/dist"
    if not (binaries / "electron.exe").is_file():
        raise SystemExit("Run npm ci and npx install-electron in studio/ first.")
    shutil.copytree(binaries, output, ignore=shutil.ignore_patterns("default_app.asar"))
    (output / "electron.exe").rename(output / "Memdex-Studio.exe")
    app = output / "resources/app"
    for name in ("package.json", "main.cjs", "preload.cjs", "backend.cjs", "terminals.cjs", "studio.js", "chats.js", "compose.js", "studio.css"):
        copy_file(studio / name, app / name)
    shutil.copytree(studio / "fonts", app / "fonts")
    for name in ("@xterm/xterm", "@xterm/addon-fit", "node-pty"):
        source, target = modules / name, app / "node_modules" / name
        copy_file(source / "package.json", target / "package.json")
        for license_file in source.glob("LICENSE*"):
            copy_file(license_file, target / license_file.name)
        shutil.copytree(source / "lib", target / "lib", ignore=shutil.ignore_patterns("*.map", "*.test.js"))
        if (source / "css").is_dir():
            shutil.copytree(source / "css", target / "css")
    pty = Path("node-pty/prebuilds/win32-x64")
    shutil.copytree(modules / pty, app / "node_modules" / pty, ignore=shutil.ignore_patterns("*.pdb"))
    notices = modules / "node-pty/third_party"
    if notices.is_dir():
        shutil.copytree(notices, app / "node_modules/node-pty/third_party")
    runtime = output / "resources/runtime"
    copy_runtime(runtime)
    subprocess.run([str(runtime / "python.exe"), "-I", "-B", "-m", "memdex.studio_rpc", "--help"],
                   cwd=output, stdout=subprocess.DEVNULL, check=True)


def build(output, electron=False):
    if os.name != "nt" or sys.implementation.name != "cpython" or sys.version_info < (3, 10):
        raise SystemExit("Build with Windows CPython 3.10 or newer.")
    archive = output.parent / (output.name + ".zip")
    checksum = archive.with_name(archive.name + ".sha256")
    if output.exists() or archive.exists() or (electron and checksum.exists()):
        raise SystemExit("Choose a new output folder.")
    if electron:
        if platform.machine().lower() not in ("amd64", "x86_64") or sys.maxsize <= 2**32:
            raise SystemExit("Build Studio with Windows x64 CPython.")
        electron_app(output)
    else:
        webview_app(output)
    copy_file(REPO / "LICENSE", output / "Memdex-LICENSE.txt")
    with zipfile.ZipFile(archive, "x", zipfile.ZIP_DEFLATED) as bundle:
        for file in sorted(output.rglob("*")):
            if file.is_file():
                bundle.write(file, file.relative_to(output.parent))
    if electron:
        from tempfile import NamedTemporaryFile
        digest = hashlib.sha256()
        with archive.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        checksum.write_text(f"{digest.hexdigest()}  {archive.name}\n", encoding="ascii")
        builds = REPO / "dist"
        builds.mkdir(exist_ok=True)
        with NamedTemporaryFile(dir=builds, mode="w", encoding="utf-8", delete=False) as receipt:
            receipt.write(str(output))
        os.replace(receipt.name, builds / "studio-build.txt")
    print(archive)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    destinations = parser.add_mutually_exclusive_group(required=True)
    destinations.add_argument("output", type=Path, nargs="?")
    destinations.add_argument("--studio-dist", type=Path, help="directory for the versioned Studio x64 app, ZIP and checksum")
    parser.add_argument("--tag", help="check that the Studio tag matches its package version")
    parser.add_argument("--electron", action="store_true", help="package the Electron studio for Windows x64")
    args = parser.parse_args()
    if args.studio_dist:
        version = json.loads((REPO / "studio/package.json").read_text(encoding="utf-8"))["version"]
        if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z]+(?:[.-][0-9A-Za-z]+)*)?", version):
            parser.error("Studio version must be a release or prerelease version.")
        if args.tag is not None and args.tag != "studio-v" + version:
            parser.error(f"expected Studio tag studio-v{version}")
        build(args.studio_dist.resolve() / f"Memdex-Studio-{version}-Windows-x64", electron=True)
    elif args.tag is not None:
        parser.error("--tag requires --studio-dist")
    else:
        build(args.output.resolve(), args.electron)
