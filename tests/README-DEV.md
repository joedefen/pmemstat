# pmemstat: working on the source tree

This is the developer companion to [`README.md`](../README.md), which documents
the tool for its users. Here we document the two things a maintainer needs and a
user does not: the **test gate** and the **release gate** (`./bless`).

This file lives in `tests/`, so its links point one directory up. The commands
assume the repository root, which is where the gate has to be run.

## Layout and single sources of truth

| Path | Owns |
| --- | --- |
| [`pmemstat/__init__.py`](../pmemstat/__init__.py) | `__version__`, and nothing else. The flit build backend reads it, `pmemstat --version` prints it, and `bless` reads the line without importing the package. |
| [`pyproject.toml`](../pyproject.toml) | Name, description, keywords, `requires-python`, runtime dependencies, the `dev` extra, and the pytest, coverage, ruff and `[tool.bless]` configuration. |
| [`bless`](../bless) | The release gate, emitted verbatim from [`../../blessd/bless`](../../blessd/bless) by that repo's `build.py`. It holds no copy of this project's identity, and it is never edited here. |
| [`blessd/docs/release-strategy.md`](../../blessd/docs/release-strategy.md) | The release strategy every project shares: the branch model, the stages, the rehearsal, the credentials. This file documents only what is particular to `pmemstat`. |
| [`tests/inspect_dist.py`](inspect_dist.py) | The distribution inspector. `bless` refuses to upload a wheel and sdist it has not inspected with this file. |
| [`runner`](../runner) | Runs the tree from source (`python -m pmemstat.main`) under `.venv/bin/python`. It installs nothing and never writes to `~/.local/bin`, so it cannot displace a released `pmemstat`. |
| [`.gitignore`](../.gitignore) | Keeps gate artifacts (`build/`, `dist/`, `.coverage*`, caches, `*.whl`, `*.tar.gz`) out of `git status`, because the gate refuses a dirty tree. |

The version lives in exactly one place. `pyproject.toml` declares
`dynamic = ["version"]`, so flit reads `pmemstat/__init__.py`; a `X.Y.Z.99.N`
rehearsal rewrites that line only inside a throwaway copy.

Almost nothing needs a `[tool.bless]` table: `NAME = pmemstat`,
`MODULE = pmemstat`, the console script and the inspector are all derived from
what is already there. The one setting written out is `branches`, which names
the branches `publish` releases from -- see
[the branch model](#the-branch-model-is-a-setting-not-a-convention) below.

## Where the package lives

This tree follows the per-project environment model: the package lives in the
checkout, not in a shared site directory. Nothing here installs with
`pip --user` any more, so no project can shadow another's dependency.

| Thing | Where | Made by |
| --- | --- | --- |
| Editable package + this project's `dev` extra | `.venv/` in this tree | `bt penv` |
| The runnable console script | `~/.local/bin/pmemstat` | `bt edit` (a symlink into `.venv/bin`) or `bt test` / `bt prod` / `pipx install` (pipx's own venv) |
| A released artifact, isolated per app | `~/.local/share/pipx/venvs/pmemstat` | `bt test` / `bt prod` |

Two invariants:

1. `.venv` is the **editable/debug** environment: the interpreter VS Code
   selects and the one `./bless`, pytest and a debugger run under, so
   breakpoints land in the tree. It holds an editable install
   (`-e '.[dev]'`), never a built copy.
2. Exactly **one owner** of `~/.local/bin/pmemstat` at a time -- `bt edit`
   links it into `.venv/bin`, `bt test`/`bt prod` let pipx own it -- and no
   command claims it with `pip --user`.

Run the source without owning the entry point:

    ./runner                      # re-execs into .venv/bin/python, elevates
    ./runner -U                   # the same, but stay the invoking user

`runner` installs nothing and never writes to `~/.local/bin`, so a source run
and a released install cannot collide. If a `console-window` working tree sits
beside this checkout at `../console-window`, both `runner` and the test package
put it ahead of whatever release is installed in `.venv`; a local change to the
library is therefore exercised by `./runner` and `pytest` immediately, with no
reinstall step. When the command on `PATH` should follow the tree instead, hand
the entry point to it:

    bt edit        # ~/.local/bin/pmemstat -> .venv/bin/pmemstat

`bt edit` and `bt test`/`bt prod` are a switch, not two installs: the last one
run owns the link. Do not `pipx install --editable` for this project; that
would create a second editable install `bt edit` does not manage. `bt status`
prints the owner, pipx's own record, the editable `.venv` and the VS Code
interpreter, plus any leftover copy from the old model that `bt clean` removes.

VS Code selects `.venv` (that is the `Editable .venv` path `bt status` prints);
to debug a released artifact instead, select
`~/.local/share/pipx/venvs/pmemstat/bin/python`.

## The test gate

Build what the gate needs with `bt`, which puts the editable package plus the
`dev` extra into this tree's `.venv` (and the gate's tool floors with it):

    bt penv        # from the project root; `bt penv pmemstat` from elsewhere

`bless` runs every stage with `sys.executable`, so it has to be *that*
interpreter -- the one holding `console-window` and the dev extra. Activate the
virtualenv first, or start the gate by hand:

    .venv/bin/python ./bless

Then the commands `bless` runs, in the same order, on the same interpreter:

    python -m ruff check .
    python -m compileall -q pmemstat tests
    python -m pytest -q --cov=pmemstat --cov-report=term-missing

The coverage floor (70%) is enforced through
`[tool.coverage.report] fail_under`. It was set just below the measured number
when the gate was adopted; raise it when the number rises, never lower it.

The ruff selection is committed rather than inherited
(`[tool.ruff.lint] select = ["E4", "E7", "E9", "F", "I", "UP", "B"]`,
`target-version = "py310"`) so that no user or global configuration can change
what the gate enforces.

`pmemstat --version` is answered by argparse before the curses screen is
touched or root elevation is attempted, because the smoke stage of the release
gate runs it as an ordinary user on a machine with no saved state.

## The release gate: `./bless`

`./bless` takes no arguments and has no flag that skips a stage. It always runs
everything, then prints what it has in hand and asks one question:

    ====> Enter test or publish or quit:

Only `test` and `publish` are accepted, spelled in full; anything else is asked
again. Enter, `quit` or Ctrl-C stops with nothing tagged, uploaded or pushed.

**Stages, in order**

1. **preflight** -- clean tree, the `vX.Y.Z` tag absent, `twine>=7` and
   `packaging>=26` present.
2. **index check** -- refuses if `__version__` is already on PyPI, printing
   every published version, before anything is built or uploaded.
3. **gates** -- the three commands above.
4. **build** -- into `build/bless-<version>/`, wiped first, so no older
   artifact can be sent by mistake.
5. **inspect** -- `tests/inspect_dist.py` against the wheel and the sdist.
6. **check** -- `twine check --strict` on both files.
7. **smoke** -- installs the built wheel into a throwaway venv and runs
   `pmemstat --version`, which must print the version being released.
8. **branch plan** -- refuses a detached HEAD, a branch that is not one of the
   release branches, a release branch missing locally or on origin, two
   branches that have diverged, or a push that would not fast-forward.

**`test`** is the rehearsal and never spends the working version. It copies the
tree to `build/bless-test-<version>/src`, rewrites `__version__` to
`X.Y.Z.99.N` (99 keeps it clear of real releases; `N` is one past the highest
suffix TestPyPI already holds for that base), builds, inspects and smoke-tests
the copy, and uploads only to `test.pypi.org`. No tag, no branch, no push. It
prints the install line, which needs `python -m pip cache purge` first and
`--extra-index-url https://pypi.org/simple/`, because TestPyPI carries no
dependencies.

**`publish`** fast-forwards the release branches to one commit, tags `vX.Y.Z`,
uploads the two inspected files to PyPI, verifies the index, and pushes those
branches and the tag.

**Credentials** are resolved per index -- `TWINE_PASSWORD` for PyPI,
`TWINE_TESTPYPI_PASSWORD` for TestPyPI, each overruling the `~/.pypirc` section
whose `repository` matches that index's upload URL -- so a PyPI token is never
spent on a rehearsal. A missing credential is a refusal before `twine` starts.

**Verification** reads the index back after an upload. The read is cache-busted
and patient (up to a quarter of an hour), because both indexes list new releases
through a CDN that lags; a release that is not listed yet is reported as lag
with the project page to check, never as a failed upload, since an accepted
upload cannot be withdrawn.

There is no CI publish job, on purpose: the upload is irreversible and is run by
the owner from the development tree, so the safety lives in one inspectable
command. CI stays a gate.

## The branch model is a setting, not a convention

`bless` publishes only from a branch it was told about, and pushes every branch
it was told about. Which ones those are is `[tool.bless] branches` in
[`pyproject.toml`](../pyproject.toml), and there are two shapes:

* `branches = ["main"]` -- one release branch. Nothing is aligned, and `publish`
  pushes `main` and the tag. This is what this project uses, so a small change
  can be published straight from the trunk.
* `branches = ["main", "dev"]` -- the default when the setting is absent. While
  you are on `main`, `dev` does not have to exist yet: the trunk alone satisfies
  the model, so `publish` releases from `main` and pushes only what exists. Once
  `dev` has been created it also has to exist as `origin/dev`, and whichever of
  the two lags is fast-forwarded onto the other before the tag, so a release
  always leaves them at one commit.

Either way the gate refuses a branch it was not told about, a branch it has to
push that is missing on origin, two branches that have diverged, and a push that
would not fast-forward. What a single branch cannot do is notice that a
development branch was left behind: merging `dev` into `main` stays your job,
and the pair exists to make it the gate's job instead.

This repository still has a fully merged `origin/dev` from its earlier history.
Switching back to the pair is one line in `pyproject.toml` plus a local branch:

    git branch dev
    git push -u origin dev

## What the inspector asserts

[`tests/inspect_dist.py`](inspect_dist.py) is the one implementation, used by
`bless` and by CI; expectations are read from `pyproject.toml` and the package,
never from the artifact under test.

* **wheel** -- `Name`, `Version`, `Summary`, `Requires-Python`, `Keywords` and
  the *runtime* `Requires-Dist` names match `pyproject.toml`; every
  `pmemstat/*.py` is present; `LICENSE.txt` is carried and listed as a
  `License-File`; no `tests/`, no `.venv`, no `__pycache__`, no `.pyc`.
* **sdist** -- carries `pyproject.toml`, `README.md`, `LICENSE.txt`,
  `pmemstat/__init__.py` and `pmemstat/main.py`; the `__version__` inside the
  sdist equals the artifact version; no venv or bytecode. The sdist is
  deliberately lean: flit's default payload is enough and `tests/` need not
  ride along.

Run it by hand on any built pair:

    python tests/inspect_dist.py dist/pmemstat-4.1.1-py3-none-any.whl \
                                dist/pmemstat-4.1.1.tar.gz

## Releasing, in three steps

1. Bump `__version__` in [`pmemstat/__init__.py`](../pmemstat/__init__.py) and
   commit.
2. Run `./bless`. The gate runs in full.
3. Answer `test` to rehearse on TestPyPI, then run `./bless` again and answer
   `publish` -- or go straight to `publish`.

`[tool.bless] branches` names what `publish` pushes. The trunk in that list must
exist and track origin. `publish` leaves every branch it pushes at the tagged
commit: one branch with `["main"]`, the pair with `["main", "dev"]`. Everything
else about the release is one of the stages above, and the shared half lives in
[`blessd/docs/release-strategy.md`](../../blessd/docs/release-strategy.md).

## Notes on this adoption

* This section is one run of
  [`../../blessd/docs/adopting.md`](../../blessd/docs/adopting.md), which is the
  procedure written for whoever converts the next project. If the two ever
  disagree, that document is the procedure and this is the record of one run.
* [`bless`](../bless) is emitted, byte for byte, from
  [`../../blessd/bless`](../../blessd/bless) -- SHA-256
  `3ef60e9e806ca58a3d900dd6622bb8be0052210da1ef26f9918bf0a1deb1e3b2` when this
  was written. It is not edited here: a change is made in `blessd` and re-emitted
  with `python3 ../../blessd/build.py --into .`, which replaces the file and
  prints the new hash.
* That build also gives the gate a read-only query mode,
  `./bless --branch-plan`, which prints this tree's branch plan as JSON without
  running a single gate. `bt` asks with it, so the branch note `bt` prints and
  the plan `publish` obeys are the same answer from the same code.
* The environment now follows the per-project model: `bt penv` rebuilt `.venv`
  around the tree (editable `pmemstat` plus the `dev` extra) instead of leaving
  it as a tools-only environment, and [`runner`](../runner) re-execs into
  `.venv/bin/python` so a source run uses the pinned `console-window` rather
  than whatever a shared site directory happens to hold.
* `pmemstat` needed five things for the gate: one version location
  (`__version__` plus `dynamic = ["version"]`), an inspector, the `dev` extra
  with `ruff`, `pytest`, `pytest-cov`, `build`, `twine>=7` and `packaging>=26`,
  a console script that answers `--version`, and a branch model -- here
  `branches = ["main"]`.
* The ruff rule set arrived with the gate, so the whole tree was brought to a
  clean `ruff check .` once. [`tests/test_bless.py`](test_bless.py) pins the
  identity this tree derives and the inspector it uploads behind; what the gate
  itself does -- branch plan, refusals, prompt, index check, emitter -- is
  tested once, in [`../../blessd/tests/test_bless.py`](../../blessd/tests/test_bless.py).
* CI runs the same three gate commands and the same inspector, never an
  equivalent, so a green CI result and a green `./bless` mean the same thing.
