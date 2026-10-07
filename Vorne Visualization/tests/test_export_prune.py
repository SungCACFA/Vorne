"""Only one export at a time: earlier snapshots beside a new one are deleted
unless --keep, so an old one cannot be the file attached to an email. Kept out
of test_export.py, which the mutation runner skips for speed."""

import pytest


def test_prune_deletes_earlier_exports_only(tmp_path):
    from webapp.export import prune

    for name in ("danbro_2026-10-06_1918.html", "danbro_2026-10-06_2140.html", "danbro_2026-10-07_0719.html",
                 "notes.html", "danbro_draft.html", "danbro_2026-10-06_1918.html.bak"):
        (tmp_path / name).write_text("x", encoding="utf-8")
    new = tmp_path / "danbro_2026-10-07_0719.html"
    gone = prune(new, log=lambda *_: None)
    assert sorted(f.name for f in gone) == ["danbro_2026-10-06_1918.html", "danbro_2026-10-06_2140.html"]
    assert sorted(f.name for f in tmp_path.iterdir()) == [
        "danbro_2026-10-06_1918.html.bak", "danbro_2026-10-07_0719.html", "danbro_draft.html", "notes.html"]


def test_build_prunes_by_default_and_keep_keeps(monkeypatch, tmp_path):
    """build() prunes once the new file is written; --keep reaches it."""
    import runpy
    import sys

    from webapp import export

    monkeypatch.setattr(sys, "argv", ["webapp", "export", "--keep", "--out", str(tmp_path / "x.html")])
    seen = {}
    monkeypatch.setattr(export, "build", lambda out=None, keep=False: seen.update(out=out, keep=keep))
    with pytest.raises(SystemExit):
        runpy.run_module("webapp.__main__", run_name="__main__")
    assert seen == {"out": str(tmp_path / "x.html"), "keep": True}
    monkeypatch.setattr(sys, "argv", ["webapp", "export"])
    with pytest.raises(SystemExit):
        runpy.run_module("webapp.__main__", run_name="__main__")
    assert seen == {"out": None, "keep": False}


