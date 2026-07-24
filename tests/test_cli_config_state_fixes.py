from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from types import SimpleNamespace

import pytest

import pyneolink.cli as cli_module
import pyneolink.core.state as state_module
from pyneolink.cli import CLI
from pyneolink.config import config_from_dict, load_config
from pyneolink.core.const.payloads import Raw, XmlTemplate
from pyneolink.core.state import ConnectionState
from pyneolink.core.xmlutil import xml_to_dict


# ---------------------------------------------------------------------------
# CLI: --camera before/after the subcommand (FIX 1)
# ---------------------------------------------------------------------------


def test_camera_before_subcommand_is_kept():
    args = CLI().parse_args(["--camera", "front", "status"])
    assert getattr(args, "camera", None) == "front"
    assert args.command == "status"


def test_camera_after_subcommand_is_kept():
    args = CLI().parse_args(["status", "--camera", "front"])
    assert getattr(args, "camera", None) == "front"


def test_camera_after_subcommand_wins_over_before():
    args = CLI().parse_args(["--camera", "front", "status", "--camera", "back"])
    assert getattr(args, "camera", None) == "back"


def test_camera_defaults_to_none():
    args = CLI().parse_args(["status"])
    assert getattr(args, "camera", None) is None


@pytest.mark.parametrize(
    "argv",
    [
        ["--camera", "front", "battery"],
        ["--camera", "front", "led"],
        ["--camera", "front", "events"],
        ["--camera", "front", "motion"],
        ["--camera", "front", "voice"],
        ["--camera", "front", "pir", "status"],
        ["--camera", "front", "ir", "status"],
        ["--camera", "front", "snapshot", "--out", "x.jpg"],
        ["--camera", "front", "record", "--out", "x.ts"],
        ["--camera", "front", "raw-stream", "--output", "x.h264"],
        ["--camera", "front", "info"],
        ["--camera", "front", "uid"],
        ["--camera", "front", "reboot"],
    ],
)
def test_camera_before_every_camera_subcommand(argv):
    args = CLI().parse_args(argv)
    assert getattr(args, "camera", None) == "front"


# ---------------------------------------------------------------------------
# CLI: --info combined with a subcommand (FIX 2)
# ---------------------------------------------------------------------------


def test_info_flag_alone_selects_info_command():
    args = CLI().parse_args(["--info"])
    assert args.command == "info"


def test_info_flag_does_not_override_explicit_subcommand(capsys):
    args = CLI().parse_args(["--info", "status"])
    assert args.command == "status"
    assert "--info" in capsys.readouterr().err


def test_subcommand_without_info_flag_untouched(capsys):
    args = CLI().parse_args(["status"])
    assert args.command == "status"
    assert capsys.readouterr().err == ""


# ---------------------------------------------------------------------------
# CLI: discover without --uid must not hit the remote relay servers (FIX 3)
# ---------------------------------------------------------------------------


def _hit(uid=None, source="local"):
    return SimpleNamespace(uid=uid, address=("192.168.1.10", 2000), source=source)


def test_discover_without_uid_skips_remote_lookup(monkeypatch, capsys):
    remote_calls = []
    monkeypatch.setattr(cli_module, "local_discover", lambda uid, timeout=None: [])
    monkeypatch.setattr(cli_module, "remote_uid_lookup", lambda uid, timeout=None: remote_calls.append(uid) or [])
    cli = CLI()
    args = cli.parse_args(["discover"])
    assert cli.run_discover(args) == 1
    assert remote_calls == []


def test_discover_without_uid_still_reports_local_hits(monkeypatch, capsys):
    remote_calls = []
    monkeypatch.setattr(cli_module, "local_discover", lambda uid, timeout=None: [_hit()])
    monkeypatch.setattr(cli_module, "remote_uid_lookup", lambda uid, timeout=None: remote_calls.append(uid) or [])
    cli = CLI()
    args = cli.parse_args(["discover"])
    assert cli.run_discover(args) == 0
    assert remote_calls == []
    assert "192.168.1.10:2000" in capsys.readouterr().out


def test_discover_with_uid_falls_back_to_remote_lookup(monkeypatch, capsys):
    remote_calls = []

    def fake_remote(uid, timeout=None):
        remote_calls.append(uid)
        return [_hit(uid=uid, source="remote")]

    monkeypatch.setattr(cli_module, "local_discover", lambda uid, timeout=None: [])
    monkeypatch.setattr(cli_module, "remote_uid_lookup", fake_remote)
    cli = CLI()
    args = cli.parse_args(["discover", "--uid", "ABCDEF0123456789"])
    assert cli.run_discover(args) == 0
    assert remote_calls == ["ABCDEF0123456789"]


# ---------------------------------------------------------------------------
# ConnectionState: merging, atomic writes, corruption tolerance (FIX 4)
# ---------------------------------------------------------------------------


def test_two_instances_updating_different_cameras_both_survive(tmp_path):
    path = tmp_path / "state.json"
    first = ConnectionState(path)
    second = ConnectionState(path)
    first.update_address("cam-a", "10.0.0.1:9000")
    second.update_address("cam-b", "10.0.0.2:9000")

    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["cameras"]["cam-a"]["address"] == "10.0.0.1:9000"
    assert stored["cameras"]["cam-b"]["address"] == "10.0.0.2:9000"

    fresh = ConnectionState(path)
    assert fresh.get_address("cam-a") == "10.0.0.1:9000"
    assert fresh.get_address("cam-b") == "10.0.0.2:9000"


def test_stale_snapshot_does_not_clobber_other_cameras_update(tmp_path):
    path = tmp_path / "state.json"
    seed = ConnectionState(path)
    seed.update_address("cam-b", "10.0.0.2:9000")

    stale = ConnectionState(path)  # snapshot now contains the old cam-b entry
    other = ConnectionState(path)
    other.update_address("cam-b", "10.9.9.9:9000")
    stale.update_address("cam-a", "10.0.0.1:9000")

    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["cameras"]["cam-a"]["address"] == "10.0.0.1:9000"
    assert stored["cameras"]["cam-b"]["address"] == "10.9.9.9:9000"


def test_save_uses_temp_file_and_atomic_replace(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    replaces = []
    real_replace = state_module.os.replace

    def spy_replace(src, dst, *args, **kwargs):
        replaces.append((str(src), str(dst)))
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(state_module.os, "replace", spy_replace)
    ConnectionState(path).update_address("cam-a", "10.0.0.1:9000")

    assert len(replaces) == 1
    src, dst = replaces[0]
    assert dst == str(path)
    assert src != str(path)
    assert src.startswith(str(tmp_path))


def test_failed_write_leaves_previous_state_intact(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    ConnectionState(path).update_address("cam-a", "10.0.0.1:9000")
    before = path.read_text(encoding="utf-8")

    real_replace = state_module.os.replace

    def failing_replace(src, dst, *args, **kwargs):
        if str(dst) == str(path):
            raise OSError("disk full")
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(state_module.os, "replace", failing_replace)
    with pytest.raises(OSError):
        ConnectionState(path).update_address("cam-a", "10.9.9.9:9000")

    assert path.read_text(encoding="utf-8") == before
    leftovers = [p for p in tmp_path.iterdir() if p.name != "state.json"]
    assert leftovers == []


@pytest.mark.parametrize(
    "content",
    [
        "null",
        "[]",
        '"x"',
        "123",
        "true",
        '{"cameras": null}',
        '{"cameras": [1, 2]}',
        '{"cameras": "oops"}',
        '{"cameras": {"cam-a"',  # truncated JSON
        "not json at all",
        "",
    ],
)
def test_corrupt_or_non_dict_state_degrades_gracefully(tmp_path, content):
    path = tmp_path / "state.json"
    path.write_text(content, encoding="utf-8")

    state = ConnectionState(path)
    assert state.get_address("cam-a") is None

    state.update_address("cam-a", "10.0.0.1:9000")
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(stored, dict)
    assert isinstance(stored["cameras"], dict)
    assert stored["cameras"]["cam-a"]["address"] == "10.0.0.1:9000"
    assert ConnectionState(path).get_address("cam-a") == "10.0.0.1:9000"


def test_missing_state_file_degrades_gracefully(tmp_path):
    path = tmp_path / "state.json"
    state = ConnectionState(path)
    assert state.get_address("cam-a") is None
    state.update_address("cam-a", "10.0.0.1:9000")
    assert ConnectionState(path).get_address("cam-a") == "10.0.0.1:9000"


def test_unreadable_state_path_degrades_gracefully(tmp_path):
    directory = tmp_path / "state.json"
    directory.mkdir()  # read_text raises IsADirectoryError (an OSError)
    state = ConnectionState(directory)
    assert state.get_address("cam-a") is None


def test_get_address_transport_filter_still_works(tmp_path):
    path = tmp_path / "state.json"
    state = ConnectionState(path)
    state.update_address("cam-a", "10.0.0.1:9000", transport="udp-relay")
    assert state.get_address("cam-a") == "10.0.0.1:9000"
    assert state.get_address("cam-a", transport="udp-relay") == "10.0.0.1:9000"
    assert state.get_address("cam-a", transport="tcp") is None


# ---------------------------------------------------------------------------
# Config: suffix dispatch (FIX 5) and missing camera name (FIX 6)
# ---------------------------------------------------------------------------

JSON_CONFIG = '{"bind": "127.0.0.1", "bind_port": 9000, "cameras": [{"name": "cam1"}]}\n'
TOML_CONFIG = 'bind = "127.0.0.1"\nbind_port = 9000\n\n[[cameras]]\nname = "cam1"\n'
GARBAGE_CONFIG = "::: neither json nor toml [[[ = \n"


def test_load_config_json_suffix(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(JSON_CONFIG, encoding="utf-8")
    cfg = load_config(path)
    assert cfg.bind == "127.0.0.1"
    assert cfg.bind_port == 9000
    assert cfg.cameras[0].name == "cam1"


def test_load_config_json_suffix_does_not_fall_back_to_toml(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(TOML_CONFIG, encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        load_config(path)


@pytest.mark.parametrize("name", ["config.toml", "config.tml", "CONFIG.TOML"])
def test_load_config_toml_suffixes(tmp_path, name):
    path = tmp_path / name
    path.write_text(TOML_CONFIG, encoding="utf-8")
    cfg = load_config(path)
    assert cfg.bind == "127.0.0.1"
    assert cfg.cameras[0].name == "cam1"


def test_load_config_extensionless_json(tmp_path):
    path = tmp_path / "config"
    path.write_text(JSON_CONFIG, encoding="utf-8")
    cfg = load_config(path)
    assert cfg.cameras[0].name == "cam1"


def test_load_config_extensionless_toml(tmp_path):
    path = tmp_path / "config"
    path.write_text(TOML_CONFIG, encoding="utf-8")
    cfg = load_config(path)
    assert cfg.cameras[0].name == "cam1"


def test_load_config_unparseable_raises_clear_valueerror(tmp_path):
    path = tmp_path / "config.cfg"
    path.write_text(GARBAGE_CONFIG, encoding="utf-8")
    with pytest.raises(ValueError) as excinfo:
        load_config(path)
    message = str(excinfo.value)
    assert "config.cfg" in message
    assert "JSON" in message
    assert "TOML" in message


def test_config_from_dict_missing_name_raises_valueerror():
    with pytest.raises(ValueError) as excinfo:
        config_from_dict({"cameras": [{"username": "admin"}]})
    assert not isinstance(excinfo.value, KeyError)
    assert "name" in str(excinfo.value)


def test_config_from_dict_with_name_still_works():
    cfg = config_from_dict({"cameras": [{"name": "cam1", "address": "10.0.0.5"}]})
    assert cfg.cameras[0].name == "cam1"
    assert cfg.cameras[0].address == "10.0.0.5"


# ---------------------------------------------------------------------------
# XmlTemplate: escaping, Raw passthrough, Raw attribute access (FIX 7)
# ---------------------------------------------------------------------------

SPECIALS = "<>&\"'"
ESCAPED = "&lt;&gt;&amp;&quot;&apos;"


def _template(template):
    return XmlTemplate(template, document=False, binary=False)


def test_format_escapes_special_characters():
    assert _template("<a>{v}</a>").format(v=SPECIALS) == f"<a>{ESCAPED}</a>"
    assert _template("<a>{0}</a>").format(SPECIALS) == f"<a>{ESCAPED}</a>"


def test_format_raw_passes_through_unescaped():
    assert _template("<a>{v}</a>").format(v=Raw("<x/>")) == "<a><x/></a>"
    assert _template("<a>{0}</a>").format(Raw("<x/>")) == "<a><x/></a>"


def test_mod_escapes_special_characters():
    assert _template("<a>%s</a>") % SPECIALS == f"<a>{ESCAPED}</a>"
    assert _template("<a>%s</a><b>%s</b>") % (SPECIALS, "ok") == f"<a>{ESCAPED}</a><b>ok</b>"
    assert _template("<a>%(v)s</a>") % {"v": SPECIALS} == f"<a>{ESCAPED}</a>"


def test_mod_raw_passes_through_unescaped():
    assert _template("<a>%s</a>") % Raw("<x/>") == "<a><x/></a>"
    assert _template("<a>%s</a><b>%s</b>") % (Raw("<x/>"), SPECIALS) == f"<a><x/></a><b>{ESCAPED}</b>"
    assert _template("<a>%(v)s</a>") % {"v": Raw("<x/>")} == "<a><x/></a>"


def test_raw_attribute_access_preserves_rawness():
    holder = SimpleNamespace(markup="<b>&</b>")
    template = _template("<a>{v.markup}</a>")
    assert template.format(v=holder) == "<a>&lt;b&gt;&amp;&lt;/b&gt;</a>"
    assert template.format(v=Raw(holder)) == "<a><b>&</b></a>"


def test_raw_nested_attribute_access_preserves_rawness():
    inner = SimpleNamespace(markup="<b>&</b>")
    outer = SimpleNamespace(inner=inner)
    template = _template("<a>{v.inner.markup}</a>")
    assert template.format(v=outer) == "<a>&lt;b&gt;&amp;&lt;/b&gt;</a>"
    assert template.format(v=Raw(outer)) == "<a><b>&</b></a>"


def test_document_template_still_wraps_and_encodes():
    template = XmlTemplate("<a>{v}</a>")
    payload = template.format(v="<hi>")
    assert isinstance(payload, bytes)
    assert b"<body>" in payload
    assert b"&lt;hi&gt;" in payload


# ---------------------------------------------------------------------------
# xmlutil.xml_to_dict behaviors (read-only checks; xmlutil.py not modified)
# ---------------------------------------------------------------------------


def test_xmlutil_repeated_sibling_tags_become_list():
    assert xml_to_dict("<body><a>1</a><a>2</a><a>3</a></body>") == {"body": {"a": ["1", "2", "3"]}}


def test_xmlutil_single_child_stays_scalar():
    assert xml_to_dict("<body><a>1</a></body>") == {"body": {"a": "1"}}


def test_xmlutil_attributes_get_at_prefix():
    assert xml_to_dict('<a id="1" x="y"/>') == {"a": {"@id": "1", "@x": "y"}}


def test_xmlutil_leaf_with_attributes_and_text():
    assert xml_to_dict('<a id="1">hello</a>') == {"a": {"@id": "1", "#text": "hello"}}


def test_xmlutil_plain_leaf_text():
    assert xml_to_dict("<a>hello</a>") == {"a": "hello"}
    assert xml_to_dict("<a/>") == {"a": ""}


def test_xmlutil_mixed_text_and_children_use_text_key():
    assert xml_to_dict("<a>hi<b>1</b></a>") == {"a": {"b": "1", "#text": "hi"}}


def test_xmlutil_parent_attributes_merge_with_children():
    assert xml_to_dict('<a id="1"><b>x</b></a>') == {"a": {"@id": "1", "b": "x"}}


def test_xmlutil_bytes_input_is_decoded():
    assert xml_to_dict(b"<a>x</a>") == {"a": "x"}


def test_xmlutil_empty_inputs_return_empty_dict():
    assert xml_to_dict(None) == {}
    assert xml_to_dict("") == {}
    assert xml_to_dict(b"") == {}


def test_xmlutil_malformed_xml_raises_parse_error():
    # Current (documented) behavior: malformed XML propagates ET.ParseError.
    with pytest.raises(ET.ParseError):
        xml_to_dict("<a><b></a>")
