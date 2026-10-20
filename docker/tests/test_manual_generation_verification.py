"""Real human verification is distinct from login and ordinary HTTP errors."""
import asyncio
import json
import shutil
import subprocess
from unittest.mock import AsyncMock, patch

from astrbot_plugin_arena_image import arena_direct as direct, arena_verification as manual
from tests.test_arena_direct_transport import (
    DirectTransportCase, FakePage, _chunked_cookies, _session_token,
    _filler_models, _image_model, _uuid7,
)
from tests.test_arena_image_hardening import _make_plugin, FakeEvent, _collect

MODEL = "image-fixture"
SUCCESS = {"status": 200, "text": 'a2:[{"type":"image","image":"https://fixture.invalid/result.png"}]\nad:{"finishReason":"stop"}'}
REJECT = {"status": 403, "text": '{"error":"recaptcha validation failed"}'}


class ManualPage(FakePage):
    def __init__(self, **kwargs):
        super().__init__(
            cookies=_chunked_cookies(_session_token()),
            models=_filler_models()+[_image_model(MODEL, _uuid7(1700000000))],
            **kwargs,
        )
        self.ready = False
        self.takes = 0

    async def evaluate(self, expression, **kwargs):
        if expression == manual.SNAPSHOT_JS:
            return {"state": "ready" if self.ready else "waiting", "token_ready": self.ready}
        if expression == manual.TAKE_JS:
            self.takes += 1
            if self.ready:
                self.ready = False
                return "ONE_USE_HUMAN_TOKEN"
            return ""
        raise AssertionError("Unexpected expression")


class ManualGenerationTests(DirectTransportCase):
    def setUp(self):
        super().setUp()
        manual._REQUIRED.clear()
        self.addCleanup(manual._REQUIRED.clear)

    def test_exact_rejection_sets_persistent_gate_without_a_second_post(self):
        page = ManualPage(response=REJECT)
        client = self.client(page)
        with self.assertRaises(direct.BridgeError) as caught:
            self.run_async(client.complete(model=MODEL, prompt="an apple"))
        self.assertEqual(caught.exception.code, "arena_generation_verification_required")
        self.assertTrue(caught.exception.requires_interactive_auth)
        self.assertEqual(len(page.requests), 1)
        manual._REQUIRED.clear()
        self.assertTrue(client._needs_manual_verification())
        with self.assertRaises(direct.BridgeError):
            self.run_async(client.complete(model=MODEL, prompt="an apple"))
        self.assertEqual(len(page.requests), 1)

    def test_human_token_uses_v2_only_and_success_clears_gate(self):
        page = ManualPage(response=SUCCESS)
        client = self.client(page)
        manual.set_required(client.cdp_url, self.data_dir, True)
        page.ready = True
        result = self.run_async(client.complete(model=MODEL, prompt="an apple"))
        self.assertTrue(result)
        body = json.loads(page.requests[0]["body"])
        self.assertEqual(body["recaptchaV2Token"], "ONE_USE_HUMAN_TOKEN")
        self.assertNotIn("recaptchaV3Token", body)
        self.assertEqual(page.takes, 1)
        self.assertEqual(page.actions, [])
        self.assertFalse(client._needs_manual_verification())
        saved = (self.data_dir/manual._NAME).read_text()
        self.assertNotIn("ONE_USE_HUMAN_TOKEN", saved)

    def test_failed_v2_is_not_retried_and_still_requires_manual_input(self):
        page = ManualPage(response=REJECT)
        client = self.client(page)
        manual.set_required(client.cdp_url, self.data_dir, True)
        page.ready = True
        with self.assertRaises(direct.BridgeError):
            self.run_async(client.complete(model=MODEL, prompt="an apple"))
        self.assertEqual(len(page.requests), 1)
        self.assertTrue(client._needs_manual_verification())
        self.assertFalse(page.ready)

    def test_generation_failure_after_accepted_verification_does_not_rechallenge(self):
        page = ManualPage(response={"status": 200, "text": 'a3:"The generation failed"'})
        client = self.client(page)
        manual.set_required(client.cdp_url, self.data_dir, True)
        page.ready = True
        with self.assertRaises(direct.BridgeError) as caught:
            self.run_async(client.complete(model=MODEL, prompt="an apple"))
        self.assertEqual(caught.exception.code, "stream_generation_failed")
        self.assertFalse(client._needs_manual_verification())
        self.assertEqual(len(page.requests), 1)
    def test_token_expired_during_reference_upload_stops_before_generation(self):
        page = ManualPage(response=SUCCESS)
        client = self.client(page)
        manual.set_required(client.cdp_url, self.data_dir, True)
        page.ready = True
        async def upload(*args):
            page.ready = False
            return {"url": "https://fixture.invalid/reference"}
        client._upload_reference = upload
        with self.assertRaises(direct.BridgeError):
            self.run_async(client.complete(model=MODEL, prompt="an apple", images=["fixture"]))
        self.assertEqual(page.requests, [])

    def test_status_never_claims_verified_from_login_or_ready_token(self):
        page = ManualPage()
        client = self.client(page)
        manual.set_required(client.cdp_url, self.data_dir, True)
        for ready in (False, True):
            page.ready = ready
            state = self.run_async(client.latest_interactive_auth_status())
            self.assertTrue(state["has_logged_in"])
            self.assertFalse(state["verified"])
            self.assertEqual(state["status"], "waiting")
            self.assertEqual(state["generation_token_ready"], ready)

    def test_unknown_403_does_not_create_manual_challenge(self):
        page = ManualPage(response={"status": 403, "text": '{"error":"forbidden"}'})
        client = self.client(page)
        with self.assertRaises(direct.BridgeError) as caught:
            self.run_async(client.complete(model=MODEL, prompt="an apple"))
        self.assertEqual(caught.exception.code, "arena_session_rejected")
        self.assertFalse(client._needs_manual_verification())

    def test_private_command_shows_existing_browser_link_and_manual_instructions(self):
        _, plugin = _make_plugin(self)
        payload = {
            "status": "waiting", "has_logged_in": True,
            "generation_verification_required": True,
            "browser_url": "https://fixture.invalid/verify",
        }
        plugin._client = lambda: type("Client", (), {"start_interactive_auth": AsyncMock(return_value=payload)})()
        results = _collect(plugin.interactive_verify(FakeEvent(private=True)))
        text = results[0].text
        self.assertIn("fixture.invalid/verify", text)
        self.assertIn("Google", text)
        self.assertIn("两分钟", text)
        self.assertIn("只用一次", text)

    def test_group_command_does_not_expose_link_or_render_widget(self):
        _, plugin = _make_plugin(self)
        plugin._client = lambda: self.fail("Group must not render verification or mint links")
        results = _collect(plugin.interactive_verify(FakeEvent(private=False)))
        self.assertIn("私聊", results[0].text)

    def test_requirement_is_scoped_by_endpoint_and_directory(self):
        manual.set_required("endpoint-a", self.data_dir, True)
        self.assertTrue(manual.required("endpoint-a", self.data_dir))
        self.assertFalse(manual.required("endpoint-b", self.data_dir))
        self.assertFalse(manual.required("endpoint-a", self.data_dir/"other"))

    def test_only_exact_recaptcha_error_qualifies(self):
        self.assertTrue(manual.is_rejected(403, REJECT["text"]))
        for code, body in [
            (429, REJECT["text"]), (200, REJECT["text"]), (403, '{"error":"forbidden"}'),
            (403, "recaptcha validation failed"), (403, '{"error":"account banned"}'),
        ]:
            self.assertFalse(manual.is_rejected(code, body))

    def test_widget_single_use_expiry_cancel_and_stale_callback_in_javascript(self):
        node = shutil.which("node")
        self.assertIsNotNone(node, "Node is required to test the browser state machine")
        script = r"""
const assert=require('node:assert/strict');
const fs=require('node:fs');
const code=JSON.parse(fs.readFileSync(0,'utf8'));
const elements=new Map(); let options; let renders=0; let clock=0;
Date.now=()=>clock;
class Element {
  constructor(){this.style={};this.children=[];}
  append(...children){this.children.push(...children);}
  remove(){elements.delete(this.id);}
}
global.document={
  createElement:()=>new Element(),
  getElementById:id=>elements.get(id),
  body:{append(el){elements.set(el.id,el);}}
};
global.window={grecaptcha:{enterprise:{
  ready:fn=>fn(),render:(el,opts)=>{options=opts;renders++;}
}}};
(async()=>{
  assert.equal((await eval(code.render)).state,'waiting');
  assert.equal((await eval(code.render)).state,'waiting');
  assert.equal(renders,1);
  assert.equal(eval(code.snapshot).token_ready,false);
  options.callback('FIXTURE_ONLY_HUMAN_TOKEN');
  assert.equal(eval(code.snapshot).token_ready,true);
  assert.equal(eval(code.take),'FIXTURE_ONLY_HUMAN_TOKEN');
  assert.equal(eval(code.take),'');
  assert.equal(eval(code.snapshot).state,'consumed');
  await eval(code.render); options.callback('EXPIRED_FIXTURE');
  clock=111000;
  assert.equal(eval(code.take),'');
  assert.equal(eval(code.snapshot).token_ready,false);
  await eval(code.render); const old=options.callback;
  elements.get('arena-plugin-manual-verification').children.at(-1).onclick();
  old('CANCELLED_FIXTURE'); assert.equal(eval(code.take),'');
  await eval(code.render); old('LATE_OLD_FIXTURE');
  assert.equal(eval(code.snapshot).token_ready,false);
  options.callback('CURRENT_FIXTURE');
  assert.equal(eval(code.take),'CURRENT_FIXTURE');
  process.stdout.write('ok');
})().catch(e=>{console.error(e);process.exit(1)});
"""
        result = subprocess.run(
            [node, "-e", script], input=json.dumps({
                "render": manual.render_js("PUBLIC_SITEKEY_FIXTURE"),
                "snapshot": manual.SNAPSHOT_JS, "take": manual.TAKE_JS,
            }), text=True, capture_output=True, timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "ok")

    def test_persistence_failure_does_not_lose_current_gate_or_success(self):
        with patch.object(manual.Path, "mkdir", side_effect=OSError("read-only")):
            manual.set_required("fixture", self.data_dir, True)
            self.assertTrue(manual.required("fixture", self.data_dir))
            manual.set_required("fixture", self.data_dir, False)
            self.assertFalse(manual.required("fixture", self.data_dir))
