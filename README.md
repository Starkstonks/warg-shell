# DigitalOcean Warg

This project allows you to take control of a DigitalOcean PaaS terminal from
your real terminal, making the experience a lot better than just using an
emulator from the JS side.

There is an authentication protocol to be documented.

## Usage

First, you need to authenticate against your Warg server. Do:

```bash
uvx warg-shell auth <your-domain> <your-token>
```

Then you can use any component that you want:

```bash
uvx warg-shell shell <your-domain> <your-product> <your-env> <your-component>
```

You can also download a dump of a database:

```bash
uvx warg-shell pg-dump <your-domain> <your-product> <your-env> <your-db> -o dump.sql
```

## Development

The project is managed with [uv](https://docs.astral.sh/uv/) and requires
Python 3.11 or later.

```bash
uv sync            # create the venv and install everything
make clean         # ruff format + ruff check --fix
make test          # pytest
uv build           # build the sdist and the wheel into dist/
```

### Releasing

Releases are published to PyPI by the `Release` GitHub workflow through
[trusted publishing](https://docs.pypi.org/trusted-publishers/) — no API token
is involved. To cut a release:

1. Bump the version in `pyproject.toml` (`uv version 1.2.0`) and merge to
   `master` following git-flow.
2. Tag the merge commit with the bare version (`git tag 1.2.0`) and push the
   tag. The workflow runs the CI, checks that the tag matches the project
   version, builds the sdist and wheel, and uploads them.

The workflow publishes from the `pypi` GitHub environment, which is what the
trusted publisher is registered against on PyPI.
