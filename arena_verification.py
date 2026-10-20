"""Remove only the plugin-owned verification UI from version 0.7.11."""
from typing import Any

CLEANUP_JS = """
(() => {
  const old = window.__arenaManualVerification;
  if (old) { old.token = ''; old.state = 'cancelled'; }
  delete window.__arenaManualVerification;
  const overlay = document.getElementById('arena-plugin-manual-verification');
  if (overlay) overlay.remove();
  return {removed: !!overlay};
})()
"""


async def cleanup(page: Any) -> None:
    # No navigation, CAPTCHA API calls, cookie writes or native-widget changes.
    await page.evaluate(CLEANUP_JS, timeout=15)
