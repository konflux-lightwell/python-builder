# Package reversioning

The builder changes the upstream package version before building its wheel.
For example, `1.2.3` becomes `1.2.3+rhlw.0` in a Scratch build.

| Build type | Version |
| --- | --- |
| Validated | Original version, unchanged |
| Scratch | `1.2.3+rhlw.0` |
| Backport | `1.2.3+rhlw.B` |
| Novel | `1.2.3+rhlw.B.n.N` (omit `.n.N` when N is zero) |

`B` and `N` are commit counts calculated from the package's Git history.

## Automatic methods

No package override is needed for these forms. The existing value must match
**the exact upstream version**. Examples below assume version `1.2.3`.

| File | Recognized version |
| --- | --- |
| `PKG-INFO` or `METADATA`, anywhere in the source | `Version: 1.2.3` in the metadata header |
| Root `pyproject.toml` | `version = "1.2.3"` in `[project]` or `[tool.poetry]` |
| Root `setup.cfg` | `version = 1.2.3` in `[metadata]` |
| Any Python file | `__version__ = "1.2.3"` |
| A file named exactly `VERSION`, at the root or inside a package | Only `1.2.3`, with optional surrounding whitespace |

The builder also recognizes these forms in root `setup.py`:

```python
from setuptools import setup
setup(version="1.2.3")
```

```python
from setuptools import setup
PACKAGE_VERSION = "1.2.3"
setup(version=PACKAGE_VERSION)
```

Setuptools and `distutils.core` imports, including aliases, are supported.
A version variable must have one module-level literal assignment before the
call. Reassigned, imported, conditional, and computed values are not followed.
The builder parses the script without executing it.

For `VERSION` files, surrounding whitespace and line endings are preserved.
Comments, additional text, and different versions are left alone.

## Custom files or expressions

Add `version_rewrite.files` to the matching `version_configs` entry in
`packages/<name>.json` in calunga-python. For example:

```json
{
  "version_configs": {
    "1.2.3": {
      "version_rewrite": {
        "files": [
          {
            "path": "release.txt",
            "pattern": "^1\\.2\\.3$",
            "replacement": "{COMPUTED_VERSION}"
          },
          {
            "path": "PKG-INFO",
            "pattern": "^Version: 1\\.2\\.3$",
            "replacement": "Version: {COMPUTED_VERSION}"
          }
        ]
      }
    }
  }
}
```

This is a config fragment. Paths are relative to the package's `source/` directory;
do not include `source/` in them. Patterns are Python regexes applied with
`re.MULTILINE`. Every rule must match and write the computed version.

**Custom rules replace all automatic methods.** Include all required version
locations, including nested egg-info metadata when present. Unlike the automatic
methods, a custom regex controls which old values match.

## Dynamic backend versions

For a backend that supports an environment override, configure `env` in the
matching version entry. For setuptools-scm, for example:

```json
{
  "version_configs": {
    "1.2.3": {
      "env": {
        "SETUPTOOLS_SCM_PRETEND_VERSION": "{COMPUTED_VERSION}"
      }
    }
  }
}
```

Use a variable supported by that package's backend. This `env` is separate from
`fromager_settings.env` and can be combined with custom rewrite rules.
For canonical Git sources, the builder also tags the checkout with the computed
version for VCS-based version providers.

Replacement and environment values support `{COMPUTED_VERSION}`, `{VERSION}`
(the upstream version), and `{PACKAGE}`. Regex patterns do not expand these tokens.

The final wheel filename and its metadata must contain the computed version.
Fromager's numeric wheel build tag is separate from this package version.
