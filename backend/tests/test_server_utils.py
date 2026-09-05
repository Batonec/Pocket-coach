from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from support import load_server_module


class ServerUtilsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "trainer.db"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def load_module(
        self,
        *,
        session_secret: str = "server-utils-session-secret",
    ):
        return load_server_module(
            db_path=self.db_path,
            session_secret=session_secret,
        )

    def test_make_session_value_roundtrips_back_to_user_id(self) -> None:
        module = self.load_module(session_secret="roundtrip-secret")

        cookie_value = module.make_session_value(42)

        self.assertEqual(module.read_session_user_id(cookie_value), 42)

    def test_read_session_user_id_rejects_tampered_signature(self) -> None:
        module = self.load_module(session_secret="roundtrip-secret")
        cookie_value = module.make_session_value(42)
        tampered_cookie = f"{cookie_value[:-1]}0"

        self.assertIsNone(module.read_session_user_id(tampered_cookie))

    def test_read_session_user_id_rejects_invalid_cookie_shape(self) -> None:
        module = self.load_module()

        self.assertIsNone(module.read_session_user_id(""))
        self.assertIsNone(module.read_session_user_id("malformed-cookie"))
        self.assertIsNone(module.read_session_user_id("not-an-int.signature"))


if __name__ == "__main__":
    unittest.main()
