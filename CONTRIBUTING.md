# Contributing to ProviderMap

Thanks for considering it. This document covers dev setup, running the test
suite, code style, and how to send a pull request. For adding a new site
adapter specifically, see "Writing a new adapter" below - that's the most
common and most welcome kind of contribution.

## Development setup

```bash
git clone https://github.com/glassface2002-jpg/providermap.git
cd providermap
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

Verify the install with the offline self-test - no network, no config needed:

```bash
providermap test
# or: python run.py test
```

You should see `Pipeline is healthy.` If you don't, something about the
environment is broken before you've touched any code; fix that first.

## Running the test suite

```bash
pytest                          # everything
pytest tests/test_validator.py  # one file
pytest -k "dedupe"              # by name
pytest -v                       # verbose
```

The suite is fully offline (see `tests/conftest.py` and
`adapters/adventhealth/fixtures.py`) - it never makes a network request, so
there's no reason a test should be flaky or slow. If you write a test that
does either, that's a bug in the test.

## Code style

Formatting and linting are enforced in CI and are not a matter of taste:

```bash
black .              # format
ruff check .         # lint (includes import sorting)
ruff check . --fix   # auto-fix what can be auto-fixed
mypy providermap adapters   # type check
```

Run all four before opening a PR - `black --check .` and `ruff check .`
failing is the fastest way to get a review request bounced back to you.

A few conventions worth knowing:

- **Type hints everywhere** in `providermap/` and `adapters/` (not required
  in `tests/`, and not enforced in `adapters/*/fixtures.py`, which
  intentionally models loosely-structured raw site data).
- **Dataclasses over dicts** for anything that crosses a module boundary -
  see `providermap/models.py`. A `dict[str, Any]` passed between functions is
  a sign something should be a dataclass instead.
- **Docstrings explain *why*, not *what*.** The code already says what it
  does; a docstring earns its place by explaining a decision, a constraint,
  or a gotcha a future reader would otherwise have to rediscover the hard way.
- **No bare `except Exception`.** Catch what you expect to happen and let
  everything else propagate - a silent broad catch is how bugs hide.

## Writing a new adapter

This is the change most likely to be genuinely useful to the next person.
Supporting a second health system's provider directory should never require
touching `providermap/` - if it does, that's a bug in the framework, please
file it as such rather than working around it in your adapter.

1. Create `adapters/<yoursite>/adapter.py` implementing
   `adapters.base.SiteAdapter`. Read `adapters/adventhealth/adapter.py`
   first - its docstring explains what was reverse-engineered and why each
   method exists.
2. Register it in `adapters/__init__.py`'s `ADAPTERS` dict.
3. Write `tests/test_<yoursite>_adapter.py` covering URL recognition, vCard
   (or equivalent) parsing, and HTML parsing - see
   `tests/test_adventhealth_adapter.py` for the shape these take.
4. If your site has enough quirks to be worth a full offline pipeline test
   (most will), add `adapters/<yoursite>/fixtures.py` following the pattern
   in `adapters/adventhealth/fixtures.py` - deliberately adversarial test
   data, not just happy-path records.
5. Ship a `config.example.yaml`-equivalent `site:` block documenting your
   site's `base_url`, `listing_path`, and `vcard_path` (or note that your
   site has no structured contact-card endpoint, if so).

You do not need to implement `jsonapi_type_candidates()` or
`organization_name_patterns()` if they don't apply to your site - the base
class's defaults (empty list) are fine.

## Pull request guidelines

- **One logical change per PR.** A bug fix and a refactor in the same PR
  makes both harder to review; split them.
- **Include a test** that would have failed before your change and passes
  after it, for anything that isn't pure documentation or formatting.
- **Update the README** if you're changing user-facing behavior (a new CLI
  flag, a config option, a changed default).
- **Add a CHANGELOG.md entry** under `[Unreleased]`.
- Keep the PR description focused on *why*, not a restatement of the diff -
  the diff already shows what changed.

## Branch naming

Not strictly enforced, but appreciated:

```
fix/<short-description>       # bug fixes
feat/<short-description>      # new functionality (e.g. a new adapter)
docs/<short-description>      # documentation only
refactor/<short-description>  # no behavior change
```

## Questions

Open an issue. If it's a question about extending the adapter framework for
a specific health system's site, include what you found when you looked at
its directory's network requests (or a link to `investigate`'s report) -
that's usually most of the work already.
