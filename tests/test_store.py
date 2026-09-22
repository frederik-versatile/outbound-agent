from store import LocalFileStore


def test_read_missing_key_returns_none(tmp_path):
    store = LocalFileStore(tmp_path)
    assert store.read_text("secrets/acme/gmail_token.json") is None
    assert not store.exists("secrets/acme/gmail_token.json")


def test_write_then_read_roundtrips(tmp_path):
    store = LocalFileStore(tmp_path)
    store.write_text("state/acme/learning/style_notes.md", "# Style Notes\n- be concise")
    assert store.read_text("state/acme/learning/style_notes.md") == "# Style Notes\n- be concise"
    assert store.exists("state/acme/learning/style_notes.md")


def test_write_creates_nested_directories(tmp_path):
    store = LocalFileStore(tmp_path)
    store.write_text("a/b/c/d.txt", "hi")
    assert (tmp_path / "a" / "b" / "c" / "d.txt").read_text() == "hi"


def test_append_line_accumulates(tmp_path):
    store = LocalFileStore(tmp_path)
    store.append_line("log.jsonl", '{"n": 1}')
    store.append_line("log.jsonl", '{"n": 2}')
    lines = store.read_text("log.jsonl").splitlines()
    assert lines == ['{"n": 1}', '{"n": 2}']


def test_append_line_respects_max_lines(tmp_path):
    store = LocalFileStore(tmp_path)
    for n in range(5):
        store.append_line("log.jsonl", str(n), max_lines=3)
    lines = store.read_text("log.jsonl").splitlines()
    assert lines == ["2", "3", "4"]
