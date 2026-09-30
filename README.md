# Nota plugin registry

The list of open-source plugins that [Nota](https://github.com/nota-daw/nota) can download and install
from **Preferences → Get Plug-ins**. Each plugin is one JSON manifest in [`plugins/`](plugins). CI turns them
into a single `index.json`, published on GitHub Pages:

    https://nota-daw.github.io/nota-plugins-registry/index.json

Nota downloads only this file, not the GitHub API, so there are no rate limits. It then fetches the
chosen release asset straight from the plugin's own GitHub release and checks its size and sha256
against the manifest before unpacking it.

## What gets in

- **Open source.** An OSI-approved license (see `LICENSES` in [`scripts/registry.py`](scripts/registry.py)) and a public GitHub repository.
- **VST3.** Nota hosts VST3 on every platform (AU on macOS comes from the system, not from here).
- **An archive, not an installer.** Nota unpacks `.zip`, `.tar.*`, `.dmg`, `.pkg` (macOS, payload only via
  `pkgutil --expand-full`, so install scripts never run) and `.deb`. It never runs `.exe`/`.msi` installers. A
  platform that only ships an installer is left out of that plugin's `assets`.
- **Pinned.** Every asset has its exact `size` and `sha256`. A re-uploaded asset fails verification until the
  manifest is updated.
- **Stable release tags.** Rolling "Nightly" releases replace their assets, so they are not accepted.

## Manifest

```jsonc
{
  "id": "dexed",                         // = file name; lowercase, digits, dashes
  "name": "Dexed",
  "developer": "Digital Suburban",
  "description": "…",                    // ≤ 200 chars, shown in Nota
  "homepage": "https://…",               // optional
  "repo": "https://github.com/asb2m10/dexed",   // source code
  "releases": "owner/repo",              // optional: repo that publishes the binaries, if different
  "license": "GPL-3.0",                  // SPDX id
  "kind": "instrument",                  // instrument | effect | midi | bundle
  "tags": ["synth", "fm"],
  "notes": "…",                          // optional caveat shown in Nota (≤ 200 chars)
  "provides": [                          // plugin names exactly as Nota's plugin scan reports them;
    { "format": "VST3", "name": "Dexed" } //   used to offer the install when a project needs one
  ],
  "versions": [                          // newest first; Nota installs versions[0]
    {
      "version": "1.0.1",
      "assets": {                        // macos-universal | macos-arm64 | macos-x64 |
        "macos-universal": {             // windows-x64 | windows-arm64 | linux-x64 | linux-arm64
          "url": "https://github.com/asb2m10/dexed/releases/download/v1.0.1/Dexed-1.0.1-macOS.zip",
          "sha256": "…",
          "size": 16939688,
          "archive": "zip",              // optional; inferred from the URL's extension
          "inner": "Foo.pkg",            // optional: nested archive to unpack too (a .pkg inside a .dmg)
          "bundles": ["Dexed.vst3"]      // .vst3 paths inside the (inner) archive
        }
      }
    }
  ]
}
```

## Adding a plugin or a version

`inspect` downloads an asset and prints its size, sha256 and the `.vst3` bundles inside. If it finds none, it
lists nested archives to retry with `--inner`:

```sh
python3 scripts/registry.py inspect https://github.com/…/releases/download/v1.0/Foo-macOS.dmg
python3 scripts/registry.py inspect https://github.com/…/Foo-macOS.dmg --inner Foo.pkg
```

Write the manifest, then:

```sh
python3 scripts/registry.py validate      # static checks
python3 scripts/registry.py verify foo    # download, hash, unpack, check the bundles (macOS for .dmg/.pkg)
```

To get the exact `provides` names, load the bundle in Nota, or run Nota's scan worker on it:
`nota-scanworker VST3 /path/to/Foo.vst3` prints `<PLUGIN name="…">`.

Open a PR. CI validates everything and verifies the plugins you touched. Merging to `main` republishes
`index.json`. A weekly job re-verifies every asset and reports plugins with a newer upstream release
(`python3 scripts/registry.py outdated`).

## Testing Nota against a local index

```sh
python3 scripts/registry.py build --out dist
NOTA_PLUGIN_REGISTRY="$PWD/dist/index.json" <run Nota>
```
