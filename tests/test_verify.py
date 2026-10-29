"""Declarative checks run when the human marks an Action done. Declarative on purpose:
the check comes from an agent, and running agent-supplied shell as a 'check' would
be the same untrusted-text-becomes-execution problem that retired the dispatcher."""
import subprocess

from apo import verify


def test_file_check_is_relative_to_the_repo(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "prod.toml").write_text("x")

    assert verify.run_check({"type": "file", "path": "config/prod.toml"}, repo_path=str(tmp_path)).ok
    missing = verify.run_check({"type": "file", "path": "config/nope.toml"}, repo_path=str(tmp_path))
    assert not missing.ok and "config/nope.toml" in missing.detail


def test_file_check_cannot_escape_the_repo(tmp_path):
    result = verify.run_check({"type": "file", "path": "../../etc/passwd"}, repo_path=str(tmp_path))
    assert not result.ok and "outside" in result.detail


def test_env_check_reads_the_repos_dot_env(tmp_path):
    (tmp_path / ".env").write_text("STRIPE_WEBHOOK_SECRET=whsec_123\n# OTHER=1\nEMPTY=\n")

    assert verify.run_check({"type": "env", "name": "STRIPE_WEBHOOK_SECRET"}, repo_path=str(tmp_path)).ok
    assert not verify.run_check({"type": "env", "name": "OTHER"}, repo_path=str(tmp_path)).ok
    assert not verify.run_check({"type": "env", "name": "EMPTY"}, repo_path=str(tmp_path)).ok


def test_dns_check_looks_for_the_expected_value(monkeypatch):
    def fake_run(cmd, **kwargs):
        assert cmd[:3] == ["dig", "+short", "TXT"] and cmd[3] == "resend._domainkey.news.example.com"
        return subprocess.CompletedProcess(cmd, 0, stdout='"p=MIGfMA0GCSq"\n', stderr="")

    monkeypatch.setattr(verify.subprocess, "run", fake_run)
    check = {"type": "dns", "name": "resend._domainkey.news.example.com", "record_type": "TXT", "contains": "p=MIGf"}

    assert verify.run_check(check).ok
    assert not verify.run_check({**check, "contains": "something-else"}).ok


def test_dns_check_with_no_record_yet(monkeypatch):
    monkeypatch.setattr(verify.subprocess, "run",
                        lambda cmd, **k: subprocess.CompletedProcess(cmd, 0, stdout="", stderr=""))
    result = verify.run_check({"type": "dns", "name": "x.example.com", "record_type": "A"})
    assert not result.ok and "No A record" in result.detail


def test_http_check_status_and_body(monkeypatch):
    class Response:
        status = 200

        def read(self, n=-1):
            return b"<title>Initech</title>"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(verify, "_open_url", lambda url, timeout: Response())

    assert verify.run_check({"type": "http", "url": "https://www.example.com/", "contains": "Initech"}).ok
    assert not verify.run_check({"type": "http", "url": "https://www.example.com/", "status": 404}).ok


def test_pr_merged_check(monkeypatch):
    monkeypatch.setattr(verify.subprocess, "run",
                        lambda cmd, **k: subprocess.CompletedProcess(cmd, 0, stdout='{"state":"MERGED"}', stderr=""))
    assert verify.run_check({"type": "pr_merged", "url": "https://github.com/o/r/pull/1"}).ok

    monkeypatch.setattr(verify.subprocess, "run",
                        lambda cmd, **k: subprocess.CompletedProcess(cmd, 0, stdout='{"state":"OPEN"}', stderr=""))
    assert not verify.run_check({"type": "pr_merged", "url": "https://github.com/o/r/pull/1"}).ok


def test_unknown_or_malformed_checks_fail_safely():
    assert not verify.run_check({"type": "shell", "command": "rm -rf /"}).ok
    assert not verify.run_check({}).ok
    assert not verify.run_check({"type": "dns"}).ok
