"""Regression: a prior rejection must not lock out future user requests."""
import asyncio
import json
import shutil
import subprocess
from unittest.mock import AsyncMock

from astrbot_plugin_arena_image import arena_direct as direct, arena_verification
from tests.test_arena_direct_transport import (
    DirectTransportCase, FakePage, _chunked_cookies, _session_token,
    _filler_models, _image_model, _uuid7,
)
from tests.test_arena_image_hardening import _make_plugin, FakeEvent, _collect

MODEL = "image-fixture"
SUCCESS = {"status": 200, "text": 'a2:[{"type":"image","image":"https://fixture.invalid/result.png"}]'}
REJECT = {"status": 403, "text": '{"error":"recaptcha validation failed"}'}


class VerificationRegressionTests(DirectTransportCase):
    def page(self, **kwargs):
        return FakePage(
            cookies=_chunked_cookies(_session_token()),
            models=_filler_models()+[_image_model(MODEL, _uuid7(1700000000))],
            **kwargs,
        )

    def test_stale_persisted_gate_does_not_prevent_generation(self):
        (self.data_dir/"arena_generation_verification.json").write_text(
            json.dumps({"required": True, "endpoint": "old", "updated_at": 1})
        )
        page = self.page(response=SUCCESS)
        result = self.run_async(self.client(page).complete(model=MODEL, prompt="an apple"))
        self.assertTrue(result)
        self.assertEqual(len(page.requests), 1)
        self.assertNotIn("recaptchaV2Token", json.loads(page.requests[0]["body"]))

    def test_explicit_rejection_does_not_persist_a_gate_or_automatically_retry(self):
        page = self.page(responses=[REJECT, SUCCESS])
        client = self.client(page)
        with self.assertRaises(direct.BridgeError) as error:
            self.run_async(client.complete(model=MODEL, prompt="an apple"))
        self.assertEqual(error.exception.code, "arena_session_rejected")
        self.assertEqual(len(page.requests), 1)
        self.assertFalse((self.data_dir/"arena_generation_verification.json").exists())
        # A separate explicit user command is allowed to reach the site.
        self.assertTrue(self.run_async(client.complete(model=MODEL, prompt="an apple")))
        self.assertEqual(len(page.requests), 2)

    def test_login_status_does_not_read_stale_local_gate_or_claim_generation_success(self):
        page = self.page()
        client = self.client(page)
        (self.data_dir/"arena_generation_verification.json").write_text('{"required":true}')
        state = self.run_async(client.latest_interactive_auth_status())
        self.assertTrue(state["has_logged_in"])
        self.assertFalse(state.get("generation_verification_required", False))
        self.assertIn("实际生成请求", state["message"])
        _, plugin = _make_plugin(self)
        text = plugin._format_verification_status(state)
        self.assertIn("只确认登录", text)

    def test_private_command_gives_link_without_forcing_a_custom_captcha(self):
        _, plugin = _make_plugin(self)
        plugin._client = lambda: type("Client", (), {"start_interactive_auth": AsyncMock(return_value={
            "status": "verified", "has_logged_in": True, "browser_url": "https://fixture.invalid/verify",
        })})()
        results = _collect(plugin.interactive_verify(FakeEvent(private=True)))
        self.assertIn("fixture.invalid/verify", results[0].text)
        self.assertIn("无需重新登录", results[0].text)
        self.assertIn("不会额外插入验证码", results[0].text)

    def test_group_command_still_does_not_expose_browser(self):
        _, plugin = _make_plugin(self)
        plugin._client = lambda: self.fail("Group must not open browser access")
        self.assertIn("私聊", _collect(plugin.interactive_verify(FakeEvent(private=False)))[0].text)

    def test_cleanup_invalidates_old_callbacks_and_preserves_official_widget(self):
        node = shutil.which("node")
        self.assertIsNotNone(node)
        script = r"""
const assert=require('node:assert/strict');
const fs=require('node:fs');
const code=JSON.parse(fs.readFileSync(0,'utf8'));
const nodes=new Map();
nodes.set('recaptcha-v2-container',{official:true});
nodes.set('arena-plugin-manual-verification',{remove(){nodes.delete('arena-plugin-manual-verification')}});
global.document={getElementById:id=>nodes.get(id)};
const previous={state:'waiting',token:'FIXTURE_TOKEN'};
global.window={__arenaManualVerification:previous};
assert.equal(eval(code).removed,true);
assert.equal(previous.token,'');
assert.equal(previous.state,'cancelled');
assert.equal(window.__arenaManualVerification,undefined);
assert.equal(nodes.get('recaptcha-v2-container').official,true);
assert.equal(eval(code).removed,false);
process.stdout.write('ok');
"""
        result = subprocess.run(
            [node, "-e", script], input=json.dumps(arena_verification.CLEANUP_JS),
            text=True, capture_output=True, timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "ok")
