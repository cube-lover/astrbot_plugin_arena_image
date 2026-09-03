import ast
import json
import unittest
from pathlib import Path


class _FakePage:
    def __init__(self) -> None:
        self.scripts: list[str] = []

    async def evaluate(self, script: str):
        self.scripts.append(script)
        return True


class TestRecaptchaInjectionScript(unittest.IsolatedAsyncioTestCase):
    """The library-injection branch used to crash before it could inject anything."""

    def test_sitekey_is_baked_into_the_injected_source(self) -> None:
        from src.recaptcha import build_recaptcha_injection_script

        script = build_recaptcha_injection_script("6Lc-TEST_sitekey")

        self.assertIn("recaptcha/enterprise.js?render=6Lc-TEST_sitekey", script)
        self.assertIn("recaptcha/api.js?render=6Lc-TEST_sitekey", script)
        # A bare Python identifier would be undefined in page scope.
        self.assertNotIn("recaptcha_sitekey", script)

    def test_injected_urls_are_a_valid_json_array(self) -> None:
        from src.recaptcha import build_recaptcha_injection_script

        script = build_recaptcha_injection_script("key/with space")
        start = script.index("const urls = ") + len("const urls = ")
        urls = json.loads(script[start : script.index(";", start)])

        self.assertEqual(len(urls), 2)
        for url in urls:
            self.assertIn("render=key%2Fwith%20space", url)

    async def test_injection_matches_safe_page_evaluate_signature(self) -> None:
        from src import main
        from src.recaptcha import build_recaptcha_injection_script

        page = _FakePage()
        result = await main.safe_page_evaluate(
            page,
            build_recaptcha_injection_script("6Lc-TEST_sitekey"),
        )

        self.assertTrue(result)
        self.assertEqual(len(page.scripts), 1)
        self.assertIn("__LM_BRIDGE_RECAPTCHA_INJECTED", page.scripts[0])

    def test_no_call_site_passes_page_arguments_to_safe_page_evaluate(self) -> None:
        """safe_page_evaluate(page, script, retries=3) forwards no evaluate() arguments."""
        from src import recaptcha

        offenders: list[str] = []
        for path in sorted(Path(recaptcha.__file__).resolve().parent.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                if name != "safe_page_evaluate":
                    continue
                offenders.extend(
                    f"{path.name}:{node.lineno} {keyword.arg}"
                    for keyword in node.keywords
                    if keyword.arg != "retries"
                )

        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
