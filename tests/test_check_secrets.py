from scripts.check_secrets import scan


def test_detects_anthropic_key(tmp_path):
    f = tmp_path / "leak.py"
    f.write_text('KEY = "sk-ant-' + "a" * 40 + '"\n')
    assert any("Anthropic" in p for p in scan([str(f)]))


def test_flags_dotenv_file(tmp_path):
    f = tmp_path / ".env"
    f.write_text("X=1\n")
    assert scan([str(f)])


def test_clean_file_passes(tmp_path):
    f = tmp_path / "ok.py"
    f.write_text("PAYPAL_CLIENT_ID = settings.paypal_client_id\n")
    assert scan([str(f)]) == []
