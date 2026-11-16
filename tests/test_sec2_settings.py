import asyncio

import pytest

import app.settings as s


@pytest.fixture
def st(tmp_path, monkeypatch):
    monkeypatch.setattr(s, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(s, "load_config", lambda: {})
    return s


def test_valid_host():
    for ok in ("192.168.1.20", "hue-bridge.lan", "fd00::1", "bridge"):
        assert s.valid_host(ok), ok
    for bad in ("evil.com/x", "a@b", "host:443", "http://x", "x y", "", "../etc", "-x", "a" * 254, None, 5):
        assert not s.valid_host(bad), bad


def test_host_fields_validated(st):
    for section, values in (("hue", {"host": "evil.com/steal?"}), ("hue", {"host": "user@10.0.0.1"}),
                            ("awair", {"hosts": "192.168.1.5, evil/x"}),
                            ("dyson", {"hosts": ["TP7-US-ABC1234A=10.0.0.5/x"]}),
                            ("dyson", {"hosts": ["bad serial!=10.0.0.5"]})):
        with pytest.raises(ValueError, match="not valid"):
            st.save_section(section, True, values)
    st.save_section("hue", True, {"host": " 192.168.1.20 "})
    st.save_section("awair", True, {"hosts": "192.168.1.5, awair.lan"})
    st.save_section("dyson", True, {"hosts": ["TP7-US-ABC1234A=192.168.1.60"]})
    saved = st.load_saved()
    assert saved["hue"]["host"] == "192.168.1.20"
    assert saved["awair"]["hosts"] == ["192.168.1.5", "awair.lan"]


def test_password_fields_must_be_strings(st):
    for bad in (["a"], {"x": 1}):
        with pytest.raises(ValueError, match="must be text"):
            st.save_section("mqtt", True, {"host": "b", "password": bad})
        with pytest.raises(ValueError, match="must be text"):
            st.save_section("mqtt", True, {"host": bad})
    with pytest.raises(ValueError, match="too long"):
        st.save_section("mqtt", True, {"host": "b", "password": "x" * 5000})
    st.save_section("mqtt", True, {"host": "b", "password": 12345})
    assert st.load_saved()["mqtt"]["password"] == "12345"


def test_adapters_reject_bad_hosts_defensively():
    from app.adapters import dyson, hue
    from app.adapters.awair import AwairAdapter
    from app.hub import Hub
    with pytest.raises(ValueError):
        asyncio.run(hue.pair("evil.com/x#"))
    assert hue.check_host("fd00::2") == "[fd00::2]"
    assert dyson.parse_hosts(["A=1.2.3.4", "B=evil/x", "C=h@x"]) == {"A": "1.2.3.4"}
    assert AwairAdapter(Hub(), {"hosts": ["10.0.0.2", "x/y"]}).hosts == ["10.0.0.2"]
