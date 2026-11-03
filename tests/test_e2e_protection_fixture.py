"""Native UI fixtures must satisfy the real Source Volume protection gate."""
from __future__ import annotations

import importlib.util
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from app.services import source_layout
from app import storage

_SPEC = importlib.util.spec_from_file_location('e2e_seed', Path(__file__).resolve().parents[1] / 'frontend/e2e/scripts/seed_and_serve.py')
seed = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(seed)


class E2EProtectionFixtureTests(unittest.TestCase):
    def test_seed_roots_are_real_managed_uuid_directories(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(seed, 'RUNTIME', Path(directory)):
            paths = seed._ensure_runtime()
            for key in ('source_primary', 'source_secondary'):
                self.assertEqual(paths[key].parent, Path(directory) / 'sources' / 'managed')
                uuid.UUID(paths[key].name)
                self.assertTrue(paths[key].is_dir())

    def test_native_mount_adapter_preserves_real_access_gate_and_scheduler(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(seed, 'RUNTIME', Path(directory)):
            paths = seed._ensure_runtime()
            sources = Path(directory) / 'sources'
            with patch.dict(os.environ, {'FROSTVAULT_TEST_SOURCES_ROOT': str(sources)}), patch.object(storage, 'persisted_maintenance_ages', return_value={'scan': None, 'audit': None, 'backup': 7, 'backup_verify': 8}):
                try:
                    with seed._native_fixture_runtime():
                        source_layout.prepare_sources_layout()
                        self.assertTrue(source_layout.vault_local_access(paths['source_primary']).local_operations_allowed)
                        self.assertFalse(source_layout.path_is_mount(sources / 'not-a-mount'))
                        self.assertEqual(storage.persisted_maintenance_ages(), {'scan': 0.0, 'audit': 0.0, 'backup': 7, 'backup_verify': 8})
                        # This remains a real failure, not a bypass for arbitrary paths.
                        self.assertFalse(source_layout.vault_local_access(sources / 'absent' / 'root').local_operations_allowed)
                finally:
                    source_layout.reset_sources_root_override()
