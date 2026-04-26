"""Tests for the shared password strength policy."""

from __future__ import annotations

from mediaman.auth.password_policy import (
    is_strong,
    password_issues,
    policy_summary,
)


class TestCommonPasswordsDataFile:
    """H5: common passwords are loaded from the data file, not an inline tuple."""

    def test_common_passwords_is_frozenset(self):
        from mediaman.auth.password_policy import _COMMON_PASSWORDS

        assert isinstance(_COMMON_PASSWORDS, frozenset)

    def test_common_passwords_non_empty(self):
        from mediaman.auth.password_policy import _COMMON_PASSWORDS

        assert len(_COMMON_PASSWORDS) > 50

    def test_no_duplicates(self):
        """Deduplication is guaranteed because we load into a set before frozenset."""
        from mediaman.auth.password_policy import _COMMON_PASSWORDS

        # Duplicate check is trivially true for a set, but verify
        # that the canonical entries are all lowercase.
        assert all(entry == entry.lower() for entry in _COMMON_PASSWORDS)

    def test_known_entries_present(self):
        from mediaman.auth.password_policy import _COMMON_PASSWORDS

        for expected in ("password", "trustno1", "qwerty", "admin", "letmein"):
            assert expected in _COMMON_PASSWORDS

    def test_data_file_exists(self):
        from pathlib import Path

        data_file = (
            Path(__file__).parent.parent.parent.parent
            / "src"
            / "mediaman"
            / "auth"
            / "data"
            / "common_passwords.txt"
        )
        assert data_file.exists()


class TestPasswordIssues:
    def test_empty_is_rejected(self):
        issues = password_issues("")
        assert issues and "required" in issues[0].lower()

    def test_too_short(self):
        issues = password_issues("Abc123!x", username="alice")
        assert any("at least" in i for i in issues)

    def test_minimum_length_alone_not_enough(self):
        # 12 chars but only 1 class — must fail class diversity.
        assert password_issues("aaaaaaaaaaaa", username="alice")

    def test_common_password_rejected(self):
        assert password_issues("Password123!", username="alice")

    def test_contains_username(self):
        issues = password_issues("aliceWasHere99!", username="alice")
        assert any("username" in i.lower() for i in issues)

    def test_strong_password_passes(self):
        assert not password_issues("Correct-Horse-9-Battery!", username="alice")

    def test_passphrase_waives_classes(self):
        # 20+ chars, high unique count, no symbols — should pass.
        assert not password_issues(
            "correct horse battery staple echo",
            username="alice",
        )

    def test_passphrase_still_checks_username(self):
        assert password_issues(
            "alice's correct horse battery staple",
            username="alice",
        )

    def test_sequential_rejected(self):
        # Long enough, has three classes, but is overwhelmingly sequential.
        assert password_issues("Abcdefghijkl1", username="alice")

    def test_trivial_repetition_rejected(self):
        # 12 chars but <6 unique.
        assert password_issues("aaabbbaaabbb", username="alice")

    def test_is_strong_wrapper(self):
        assert is_strong("Correct-Horse-9-Battery!", username="alice")
        assert not is_strong("password", username="alice")

    def test_policy_summary_non_empty(self):
        summary = policy_summary()
        assert summary and all(isinstance(s, str) for s in summary)


class TestForceChangeFlag:
    """Integration: login with a weak plaintext should flip the flag."""

    def _conn(self, tmp_path):
        from mediaman.db import init_db

        return init_db(str(tmp_path / "mm.db"))

    def test_weak_password_flags_account(self, tmp_path):
        import bcrypt

        from mediaman.auth.session import (
            user_must_change_password,
        )

        conn = self._conn(tmp_path)

        # Manually insert a user with a weak password hash — the
        # create_user path would reject this, so we simulate a
        # legacy row that existed before the policy landed.
        weak = "password123"
        pw_hash = bcrypt.hashpw(weak.encode(), bcrypt.gensalt(rounds=4)).decode()
        conn.execute(
            "INSERT INTO admin_users (username, password_hash, created_at) "
            "VALUES (?, ?, '2026-01-01')",
            ("legacy", pw_hash),
        )
        conn.commit()

        # Simulate login_submit flipping the flag after auth success.
        from mediaman.auth.password_policy import is_strong
        from mediaman.auth.session import authenticate, set_must_change_password

        assert authenticate(conn, "legacy", weak)
        assert not is_strong(weak, username="legacy")
        set_must_change_password(conn, "legacy", True)

        assert user_must_change_password(conn, "legacy") is True

    def test_strong_password_does_not_flag(self, tmp_path):
        conn = self._conn(tmp_path)
        from mediaman.auth.session import create_user, user_must_change_password

        create_user(conn, "alice", "Correct-Horse-9-Battery!")
        assert user_must_change_password(conn, "alice") is False


class TestForcePasswordChangePage:
    """End-to-end: flagged admin is redirected to /force-password-change."""

    def _client(self, tmp_path, *, secret_key, monkeypatch):
        # Use monkeypatch so we don't pollute other tests' env.
        monkeypatch.setenv("MEDIAMAN_SECRET_KEY", secret_key)
        monkeypatch.setenv("MEDIAMAN_DATA_DIR", str(tmp_path))

        from fastapi.testclient import TestClient

        from mediaman.db import init_db, set_connection
        from mediaman.main import create_app

        conn = init_db(str(tmp_path / "mediaman.db"))
        set_connection(conn)

        app = create_app()
        app.state.config = type("C", (), {"secret_key": secret_key})()
        app.state.db = conn
        # Ensure state.templates exists even without a full lifespan run.
        from pathlib import Path

        from fastapi.templating import Jinja2Templates

        tpl_dir = (
            Path(__file__).resolve().parent.parent.parent.parent
            / "src"
            / "mediaman"
            / "web"
            / "templates"
        )
        app.state.templates = Jinja2Templates(directory=str(tpl_dir))

        return TestClient(app, follow_redirects=False), conn

    def test_flagged_admin_redirected_on_dashboard(self, tmp_path, secret_key, monkeypatch):
        client, conn = self._client(tmp_path, secret_key=secret_key, monkeypatch=monkeypatch)

        # Set up user + mark must_change_password
        from mediaman.auth.session import (
            create_session,
            create_user,
            set_must_change_password,
        )

        create_user(conn, "alice", "Correct-Horse-9-Battery!")
        set_must_change_password(conn, "alice", True)
        # Match what fastapi TestClient sends so the fingerprint binding
        # added to every page route doesn't reject the test session.
        token = create_session(
            conn,
            "alice",
            user_agent="testclient",
            client_ip="testclient",
        )

        client.cookies.set("session_token", token)
        # Any protected URL — middleware should redirect to /force-password-change
        resp = client.get("/library")
        assert resp.status_code == 302
        assert resp.headers["location"] == "/force-password-change"

    def test_force_change_page_renders(self, tmp_path, secret_key, monkeypatch):
        client, conn = self._client(tmp_path, secret_key=secret_key, monkeypatch=monkeypatch)

        from mediaman.auth.session import (
            create_session,
            create_user,
            set_must_change_password,
        )

        create_user(conn, "alice", "Correct-Horse-9-Battery!")
        set_must_change_password(conn, "alice", True)
        # Match what fastapi TestClient sends so the fingerprint binding
        # added to every page route doesn't reject the test session.
        token = create_session(
            conn,
            "alice",
            user_agent="testclient",
            client_ip="testclient",
        )

        client.cookies.set("session_token", token)
        resp = client.get("/force-password-change")
        assert resp.status_code == 200
        assert "Update password" in resp.text or "stronger password" in resp.text

    def test_force_change_rejects_weak_new_password(self, tmp_path, secret_key, monkeypatch):
        client, conn = self._client(tmp_path, secret_key=secret_key, monkeypatch=monkeypatch)

        from mediaman.auth.session import (
            create_session,
            create_user,
            set_must_change_password,
        )

        old = "Correct-Horse-9-Battery!"
        create_user(conn, "alice", old)
        set_must_change_password(conn, "alice", True)
        # Match what fastapi TestClient sends so the fingerprint binding
        # added to every page route doesn't reject the test session.
        token = create_session(
            conn,
            "alice",
            user_agent="testclient",
            client_ip="testclient",
        )

        client.cookies.set("session_token", token)
        resp = client.post(
            "/force-password-change",
            data={
                "old_password": old,
                "new_password": "password",
                "confirm_password": "password",
            },
            headers={"Origin": "http://testserver"},
        )
        # Should render the form again with an issue list — 200 and still
        # on the same page.
        assert resp.status_code == 200
        assert "policy-issues" in resp.text or "strength" in resp.text.lower()

    def test_force_change_accepts_strong_new_password(self, tmp_path, secret_key, monkeypatch):
        client, conn = self._client(tmp_path, secret_key=secret_key, monkeypatch=monkeypatch)

        from mediaman.auth.session import (
            create_session,
            create_user,
            set_must_change_password,
            user_must_change_password,
        )

        old = "Correct-Horse-9-Battery!"
        new = "Zeppelin-9000-Antelope-Parade!"
        create_user(conn, "alice", old)
        set_must_change_password(conn, "alice", True)
        # Match what fastapi TestClient sends so the fingerprint binding
        # added to every page route doesn't reject the test session.
        token = create_session(
            conn,
            "alice",
            user_agent="testclient",
            client_ip="testclient",
        )

        client.cookies.set("session_token", token)
        resp = client.post(
            "/force-password-change",
            data={
                "old_password": old,
                "new_password": new,
                "confirm_password": new,
            },
            headers={"Origin": "http://testserver"},
        )
        assert resp.status_code == 302
        assert resp.headers["location"] == "/"
        # Flag cleared
        assert user_must_change_password(conn, "alice") is False
