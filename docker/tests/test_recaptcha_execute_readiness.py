import json
import unittest
from pathlib import Path

from src import recaptcha


class _ScriptedPage:
    """A page whose evaluate() returns a scripted sequence of probe states."""

    def __init__(self, states: list) -> None:
        self.states = list(states)
        self.calls = 0

    async def evaluate(self, script: str):  # noqa: ARG002
        self.calls += 1
        if not self.states:
            return "loading"
        return self.states.pop(0)


class TestGrecaptchaReadinessProbe(unittest.IsolatedAsyncioTestCase):
    """"Library ready: True" used to mean "the namespace exists", not "execute() works"."""

    async def test_probe_waits_until_execute_is_callable(self) -> None:
        page = _ScriptedPage(["missing", "loading", "enterprise"])

        state = await recaptcha.wait_for_grecaptcha_execute(page, timeout=5.0, interval=0.01)

        self.assertEqual(state, "enterprise")
        self.assertEqual(page.calls, 3)

    async def test_probe_reports_a_half_initialised_namespace(self) -> None:
        page = _ScriptedPage(["loading"])

        state = await recaptcha.wait_for_grecaptcha_execute(page, timeout=0.0, interval=0.01)

        self.assertEqual(state, "loading")
        self.assertNotIn(state, recaptcha.GRECAPTCHA_READY_STATES)
        self.assertEqual(page.calls, 1)

    def test_probe_requires_a_callable_execute(self) -> None:
        probe = recaptcha.GRECAPTCHA_EXECUTE_PROBE
        self.assertIn("typeof ent.execute === 'function'", probe)
        self.assertIn("typeof g.execute === 'function'", probe)
        self.assertIn("return 'loading'", probe)


class TestRecaptchaMintScript(unittest.TestCase):
    """The mint script must wait for execute() instead of throwing on first look."""

    def test_sitekey_and_action_are_json_encoded(self) -> None:
        script = recaptcha.build_recaptcha_mint_script("6Lc-TEST'key", "sign'up")

        self.assertIn(json.dumps("6Lc-TEST'key"), script)
        self.assertIn(json.dumps("sign'up"), script)
        self.assertNotIn("recaptcha_sitekey", script)
        self.assertNotIn("recaptcha_action", script)

    def test_execute_is_awaited_with_a_retry_window(self) -> None:
        script = recaptcha.build_recaptcha_mint_script("k", "a", wait_ms=8000, poll_ms=250)

        self.assertIn("while (!g && Date.now() < deadline)", script)
        self.assertIn("Date.now() + 8000", script)
        self.assertIn("await sleep(250)", script)
        # The hard failure stays, but only after the wait window elapses.
        self.assertLess(
            script.index("while (!g && Date.now() < deadline)"),
            script.index("No valid grecaptcha found"),
        )

    def test_mint_script_is_used_by_the_token_flow(self) -> None:
        source = Path(recaptcha.__file__).read_text(encoding="utf-8")
        self.assertIn(
            "mint_js = build_recaptcha_mint_script(recaptcha_sitekey, recaptcha_action)",
            source,
        )
        # The readiness gate must not fall back to the old truthiness check.
        self.assertNotIn("return !!(w.grecaptcha && w.grecaptcha.enterprise)", source)


if __name__ == "__main__":
    unittest.main()
