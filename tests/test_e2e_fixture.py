"""DB-free boundary regression for the CI-only legacy Worker fixture."""
import importlib.util
import os
from dataclasses import replace
from pathlib import Path
import unittest
from unittest.mock import patch
from starlette.responses import Response
from starlette.testclient import TestClient


class LegacyWorkerFixtureTests(unittest.TestCase):
    def test_seeded_emergency_credentials_pass_real_startup_validation(self):
        from app import config
        path = Path(__file__).resolve().parents[1] / 'frontend/e2e/scripts/seed_and_serve.py'
        spec = importlib.util.spec_from_file_location('e2e_seed_credentials', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with patch.dict(os.environ):
            module._apply_env({})
            configured = replace(config.settings,
                oidc_enabled=False, break_glass_allowed_cidrs='',
                bootstrap_admin_username=os.environ['BOOTSTRAP_ADMIN_USERNAME'],
                bootstrap_admin_password=os.environ['BOOTSTRAP_ADMIN_PASSWORD'],
                bootstrap_admin_display_name=os.environ['BOOTSTRAP_ADMIN_DISPLAY_NAME'])
            with patch.object(config, 'settings', configured):
                config.validate_settings()
            self.assertEqual(configured.bootstrap_admin_username, module.OWNER_USERNAME)
            self.assertEqual(configured.bootstrap_admin_password, module.PASSWORD)
            self.assertEqual(configured.bootstrap_admin_display_name, 'E2E Owner')

    def test_only_exact_get_fixture_is_intercepted_not_production_or_lifespan(self):
        path = Path(__file__).resolve().parents[1] / 'frontend/e2e/scripts/seed_and_serve.py'
        spec = importlib.util.spec_from_file_location('e2e_seed_fixture', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        forwarded = []

        async def original(scope, receive, send):
            forwarded.append((scope['type'], scope.get('path')))
            if scope['type'] == 'lifespan':
                await receive()
                await send({'type': 'lifespan.startup.complete'})
                await receive()
                await send({'type': 'lifespan.shutdown.complete'})
            else:
                await Response('production')(scope, receive, send)

        with TestClient(module._legacy_worker_fixture(original)) as client:
            script = client.get('/sw.js?legacy-test')
            self.assertEqual(script.status_code, 200)
            self.assertIn('legacy-worker-fixture', script.text)
            self.assertIn('clients.claim()', script.text)
            self.assertNotIn('unregister', script.text)
            self.assertEqual(script.headers['cache-control'], 'no-store')
            self.assertTrue(script.headers['content-type'].startswith('application/javascript'))
            for method, url in [('GET', '/sw.js'), ('GET', '/sw.js?other'), ('POST', '/sw.js?legacy-test'), ('GET', '/api/me')]:
                self.assertEqual(client.request(method, url).text, 'production')
        self.assertIn(('lifespan', None), forwarded)
        self.assertEqual(len(forwarded), 5)
