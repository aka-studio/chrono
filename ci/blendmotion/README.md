# BlendMotion PyChrono runtime builds

This branch (`blendmotion-ci`) carries ONLY the CI machinery that builds
self-contained PyChrono runtime zips for the
[BlendMotion](https://github.com/edccorp/BlendMotion) Blender addon.
Chrono sources are checked out from **upstream** `projectchrono/chrono` at
build time, so this branch never needs syncing with upstream.

## What gets built

6 zips: `{win_amd64, linux_x86_64, macos_arm64} x {cp311, cp313}`

- cp311 → Blender 4.1–5.0 (Python 3.11)
- cp313 → Blender 5.1+ (Python 3.13; Blender skipped 3.12)

Each zip contains a flat `pychrono/` package (wrappers + statically-linked
`_core`/`_vehicle`/`_fea`/`_robot` native modules + `demos/` + Chrono LICENSE),
matching the layout BlendMotion's installer (`install.py::_extract_runtime`)
expects. Naming contract (parsed by `install.py::_find_runtime_zip`):

```
pychrono_runtime_<os>_<arch>_<cpXY>.zip
os   ∈ {win, linux, macos}
arch ∈ {amd64, x86_64, arm64}
```

## How to run a build

Actions → **Build PyChrono runtimes** → Run workflow (on `blendmotion-ci`):

- `chrono_ref` — upstream Chrono tag/sha to build (pin policy: latest stable
  release tag; currently `10.0.0`).
- `release_tag` — leave EMPTY for a trial build (zips land as workflow
  artifacts only). Set e.g. `chrono-10.0.0-bm1` to publish a GitHub release
  with the zips attached. Bump the `-bmN` suffix when repackaging the same
  Chrono version.

After publishing a release, dispatch **Update PyChrono runtimes** in the
BlendMotion repo with that tag — it downloads the assets into
`BlendMotion/assets/backends/chrono/install/runtime` and opens a PR.

## Build notes / troubleshooting

- SWIG is pinned to 4.3.1 (matches the original Windows build). Fallback pin
  if wrapper codegen breaks: `swig==4.2.1`.
- Windows/Linux: `BUILD_SHARED_LIBS=OFF` + PIC produces self-contained
  extension modules (no separate Chrono DLLs/sos), matching the original
  BlendMotion Windows build.
- macOS builds Chrono SHARED: with static libs each of the 4 extension
  modules embeds its own copy of Chrono's C++ globals and (under the
  -flat_namespace link Chrono uses on Apple) their destructors collide,
  segfaulting at interpreter shutdown. The packaging script bundles the
  `libChrono_*.dylib`s inside `pychrono/` and rewrites load commands to
  `@loader_path/<name>`.
- The hard libpython dependency is removed from all Linux/macOS binaries
  (patchelf / LIEF): Blender resolves Python symbols from its own process,
  and end-user machines need not have a system Python installed. The
  packaging script FAILS if any libpython/Python.framework reference
  survives (the CI runner has Python installed, so the smoke test alone
  cannot catch this).
- macOS natives are ad-hoc codesigned AFTER stripping/editing (both
  invalidate the signature; Apple Silicon kills invalid binaries at dlopen).
- Linux builds on ubuntu-22.04 → glibc ≥ 2.35 required at runtime. If older
  distro support is ever needed, rebuild in a manylinux_2_28 container.
- Each job smoke-imports the packaged runtime before uploading, so a red job
  means nothing broken was published.
