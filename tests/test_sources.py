"""Tests for providermap.sources - discovery strategies against a fake fetcher.

Currently covers only SitemapSource, added specifically as a regression test
for a real bug found against AdventHealth: a sitemap index whose children are
named generically (``?page=N``) rather than with a "doctor"/"physician"
keyword, where only one of many children actually contains provider profile
URLs. A previous keyword/child-count heuristic in ``_expand()`` never
recursed into that child, so the source silently reported zero discoverable
URLs from a sitemap that in fact had the entire directory in it.
"""

from __future__ import annotations

from providermap.sources import SitemapSource


class _FakeFetcher:
    """Minimal AsyncFetcher stand-in: a fixed URL -> body map, no network."""

    def __init__(self, pages: dict[str, str]):
        self._pages = pages
        self.stats = {"requests": 0, "cache_hits": 0, "errors": 0, "robots_blocked": 0}
        self.nppes_limiter = None

    async def __aenter__(self) -> _FakeFetcher:
        return self

    async def __aexit__(self, *exc: object) -> None:
        pass

    async def get(self, url: str, limiter=None, use_cache: bool = True) -> str | None:
        self.stats["requests"] += 1
        return self._pages.get(url)

    def allowed(self, url: str) -> bool:
        return True


def _sitemap_index(children: list[str]) -> str:
    locs = "".join(f"<loc>{c}</loc>" for c in children)
    return f'<?xml version="1.0" encoding="UTF-8"?><sitemapindex>{locs}</sitemapindex>'


def _urlset(urls: list[str]) -> str:
    locs = "".join(f"<loc>{u}</loc>" for u in urls)
    return f'<?xml version="1.0" encoding="UTF-8"?><urlset>{locs}</urlset>'


class TestSitemapIndexTraversal:
    async def test_finds_profile_urls_in_a_generically_named_middle_child(self, adapter, config):
        base = config.site.base_url
        children = [f"{base}/sitemap.xml?page={i}" for i in range(1, 16)]
        pages = {f"{base}/sitemap.xml": _sitemap_index(children)}
        # Only child #8 actually has doctor profile URLs - the other 14 are
        # unrelated site content, matching the real site's structure.
        for i, child in enumerate(children, start=1):
            if i == 8:
                pages[child] = _urlset(
                    [
                        f"{base}/doctors/peter-bridge-md-1245295609",
                        f"{base}/doctors/nausheen-hasan-md-1245321207",
                    ]
                )
            else:
                pages[child] = _urlset([f"{base}/central-florida-community-benefit/blogs/x{i}"])

        fetcher = _FakeFetcher(pages)
        source = SitemapSource(fetcher, config, adapter)

        assert await source.available() is True

        found = [u async for u in source.urls()]
        assert found == [
            f"{base}/doctors/peter-bridge-md-1245295609",
            f"{base}/doctors/nausheen-hasan-md-1245321207",
        ]

    async def test_does_not_select_a_sitemap_with_no_matching_children(self, adapter, config):
        base = config.site.base_url
        children = [f"{base}/sitemap.xml?page={i}" for i in range(1, 4)]
        pages = {f"{base}/sitemap.xml": _sitemap_index(children)}
        for child in children:
            pages[child] = _urlset([f"{base}/news/some-article"])

        fetcher = _FakeFetcher(pages)
        source = SitemapSource(fetcher, config, adapter)

        assert await source.available() is False

    async def test_max_sitemap_files_bounds_a_pathological_index(
        self, adapter, config, monkeypatch
    ):
        import providermap.sources as sources_mod

        monkeypatch.setattr(sources_mod, "_MAX_SITEMAP_FILES", 5)

        base = config.site.base_url
        # More children than the (patched) cap - the matching one is beyond it.
        children = [f"{base}/sitemap.xml?page={i}" for i in range(1, 11)]
        pages = {f"{base}/sitemap.xml": _sitemap_index(children)}
        for i, child in enumerate(children, start=1):
            if i == 9:
                pages[child] = _urlset([f"{base}/doctors/peter-bridge-md-1245295609"])
            else:
                pages[child] = _urlset([f"{base}/news/some-article-{i}"])

        fetcher = _FakeFetcher(pages)
        source = SitemapSource(fetcher, config, adapter)

        # The matching child is past the cap, so it's never reached.
        assert await source.available() is False
