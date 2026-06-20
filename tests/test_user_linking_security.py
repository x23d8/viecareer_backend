"""Regression tests for secure Clerk-to-database account linking."""

import unittest
from unittest.mock import patch

from fastapi import HTTPException

from services.user_service import upsert_auth_user


class UserLinkingSecurityTests(unittest.TestCase):
    def test_existing_email_cannot_be_claimed_by_new_clerk_subject(self):
        connection = FakeConnection(
            linked_user=None,
            email_owner={
                "id": "00000000-0000-0000-0000-000000000001",
                "external_auth_id": None,
            },
        )

        with patch("services.user_service.get_connection", return_value=connection):
            with self.assertRaises(HTTPException) as raised:
                upsert_auth_user(
                    external_auth_id="user_attacker",
                    email="admin@example.com",
                    full_name="Attacker",
                )

        self.assertEqual(raised.exception.status_code, 409)
        self.assertFalse(connection.cursor_instance.insert_attempted)

    def test_existing_clerk_subject_updates_only_its_linked_row(self):
        connection = FakeConnection(
            linked_user={
                "id": "00000000-0000-0000-0000-000000000002",
                "email": "original@example.com",
            },
            email_owner=None,
        )

        with patch("services.user_service.get_connection", return_value=connection):
            result = upsert_auth_user(
                external_auth_id="user_existing",
                email="updated@example.com",
                full_name="Existing User",
            )

        self.assertEqual(result["external_auth_id"], "user_existing")
        self.assertEqual(result["email"], "original@example.com")
        self.assertFalse(connection.cursor_instance.email_lookup_attempted)
        self.assertEqual(result["role_id"], 2)


    def test_verified_email_relinks_when_previous_clerk_user_is_deleted(self):
        connection = FakeConnection(
            linked_user=None,
            email_owner={
                "id": "00000000-0000-0000-0000-000000000004",
                "external_auth_id": "user_deleted",
                "email": "owner@example.com",
            },
        )

        with (
            patch("services.user_service.get_connection", return_value=connection),
            patch("services.user_service._clerk_user_is_missing", return_value=True),
        ):
            result = upsert_auth_user(
                external_auth_id="user_recreated",
                email="owner@example.com",
                full_name="Owner",
                identity_verified=True,
            )

        self.assertEqual(result["external_auth_id"], "user_recreated")
        self.assertEqual(result["role_id"], 2)
        self.assertFalse(connection.cursor_instance.insert_attempted)
class FakeConnection:
    def __init__(self, *, linked_user, email_owner):
        self.cursor_instance = FakeCursor(
            linked_user=linked_user,
            email_owner=email_owner,
        )

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def cursor(self):
        return self.cursor_instance

    def commit(self):
        return None


class FakeCursor:
    def __init__(self, *, linked_user, email_owner):
        self.linked_user = linked_user
        self.email_owner = email_owner
        self.fetchone_results = []
        self.email_lookup_attempted = False
        self.insert_attempted = False
        self.last_params = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def execute(self, query, params=None):
        normalized = " ".join(query.split()).lower()
        self.last_params = params

        if normalized.startswith("alter table") or normalized.startswith("create unique index"):
            return None
        if "select id from roles" in normalized:
            self.fetchone_results.append({"id": 2})
            return None
        if "from users where external_auth_id" in normalized:
            self.fetchone_results.append(self.linked_user)
            return None
        if "select id, external_auth_id from users" in normalized:
            self.email_lookup_attempted = True
            self.fetchone_results.append(self.email_owner)
            return None
        if normalized.startswith("update users"):
            self.fetchone_results.append(
                {
                    "id": params[-1],
                    "external_auth_id": params[0],
                    "email": (self.linked_user or self.email_owner)["email"],
                    "role_id": 2,
                }
            )
            return None
        if normalized.startswith("insert into users"):
            self.insert_attempted = True
            self.fetchone_results.append(
                {
                    "id": "00000000-0000-0000-0000-000000000003",
                    "external_auth_id": params[0],
                    "email": params[2],
                    "role_id": params[4],
                }
            )

    def fetchone(self):
        if not self.fetchone_results:
            return None
        return self.fetchone_results.pop(0)
