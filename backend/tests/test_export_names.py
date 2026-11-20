"""Exported files are named after the session and the build, not a fixed "enclosure"."""

from app.export_names import export_name, rename_exports, slug


def test_files_are_named_after_the_session_and_build():
    title, stem = "Humanoid tripod robot", "humanoid-tripod-robot-build-4"
    assert export_name("enclosure.step", title, 4) == f"{stem}.step"
    assert export_name("enclosure-drawing.pdf", title, 4) == f"{stem}-drawing.pdf"
    assert export_name("enclosure.py", title, 4) == f"{stem}.py"
    assert export_name("parameters.json", title, 4) == f"{stem}-parameters.json"
    assert export_name("build-note.md", title, 4) == f"{stem}-build-note.md"


def test_titles_become_safe_file_names():
    assert slug("b-parasite case on v1.2") == "b-parasite-case-on-v1-2"
    assert slug("../../etc/passwd") == "etc-passwd"
    assert slug("Café Ünïcode") == "cafe-unicode"
    assert slug("") == slug(None) == slug("!!!") == "design"
    assert len(slug("x" * 500)) <= 60


def test_rename_moves_the_files_and_updates_their_names(tmp_path):
    (tmp_path / "enclosure.step").write_text("step")
    written = [{"format": "step", "name": "enclosure.step", "media_type": "model/step"}]
    rename_exports(tmp_path, written, "Open box", 1)
    assert written[0]["name"] == "open-box-build-1.step"
    assert (tmp_path / "open-box-build-1.step").read_text() == "step"
    assert not (tmp_path / "enclosure.step").exists()
