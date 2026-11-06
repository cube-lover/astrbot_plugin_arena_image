"""Read-only source contracts for the dedicated server browser; no CDP/input."""
from pathlib import Path
import unittest

DOCKER = Path(__file__).resolve().parents[1]


class ServerBrowserMaintenanceTest(unittest.TestCase):
    def test_signed_vendor_chrome_and_chinese_fonts_are_installed(self):
        source = (DOCKER / 'Dockerfile.arena-browser').read_text(encoding='utf-8')
        for fragment in (
            'https://dl.google.com/linux/linux_signing_key.pub',
            'signed-by=/etc/apt/keyrings/google-chrome.gpg',
            'https://dl.google.com/linux/chrome/deb/',
            'google-chrome-stable', 'fonts-noto-cjk', 'fonts-noto-color-emoji',
            'locale-gen', 'CHROME_BIN=/usr/bin/google-chrome-stable', 'LANG=zh_CN.UTF-8',
        ):
            self.assertIn(fragment, source)
        self.assertNotIn('apt-key add', source)
        self.assertNotIn('trusted=yes', source)

    def test_browser_keeps_profile_and_native_network_shared_memory_defaults(self):
        source = (DOCKER / 'arena-browser-entrypoint.sh').read_text(encoding='utf-8')
        for fragment in (
            'STATE_DIR=/data/browser', 'PROFILE="$STATE_DIR/chrome-profile"',
            'command -v google-chrome-stable', '--lang=zh-CN',
            '--remote-debugging-address=127.0.0.1',
            '--remote-allow-origins=http://127.0.0.1:9222,http://localhost:9222',
            '--restore-last-session', 'https://arena.ai/?mode=direct',
            'asyncio.start_server(handler, "0.0.0.0", 9223)',
        ):
            self.assertIn(fragment, source)
        for removed in ('--disable-dev-shm-usage', '--disable-background-networking',
                        'AutomationControlled', '--remote-allow-origins=*'):
            self.assertNotIn(removed, source)
        self.assertNotIn('echo "$PROXY_URL"', source)
        self.assertIn('echo "arena browser proxy configured"', source)
        self.assertIn('python3 /app/proxy_relay.py', source)
        self.assertIn('CHROME_PROXY_URL="http://127.0.0.1:18080"', source)
        self.assertIn('--proxy-server="$CHROME_PROXY_URL"', source)
        dockerfile = (DOCKER / 'Dockerfile.arena-browser').read_text(encoding='utf-8')
        self.assertIn('COPY proxy_relay.py /app/proxy_relay.py', dockerfile)

    def test_bridge_reaper_does_not_change_other_services_or_publish_cdp(self):
        source = (DOCKER / 'docker-compose.arena.yml').read_text(encoding='utf-8')
        bridge = source.split('  arena-bridge:', 1)[1].split('\n  arena-browser:', 1)[0]
        browser = source.split('  arena-browser:', 1)[1].split('\nnetworks:', 1)[0]
        self.assertIn('    init: true', bridge)
        self.assertEqual(source.count('    init: true'), 1)
        self.assertIn('./arena-data:/data', bridge)
        self.assertIn('./arena-browser-data:/data/browser', browser)
        self.assertIn('shm_size: "1gb"', browser)
        self.assertIn('${ARENA_CDP_BIND:-127.0.0.1}:9223:9223', browser)
        self.assertNotIn('"0.0.0.0:9223:9223"', browser)
        self.assertIn('restart: unless-stopped', bridge)
        self.assertIn('restart: unless-stopped', browser)

    def test_shell_file_has_lf_and_no_bom(self):
        data = (DOCKER / 'arena-browser-entrypoint.sh').read_bytes()
        self.assertTrue(data.startswith(b'#!/usr/bin/env bash\n'))
        self.assertNotIn(b'\r\n', data)
