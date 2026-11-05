"""Owner-only whole-Vault HTTP contract; PostgreSQL execution belongs to PR CI."""
from dataclasses import replace
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from app import main
from app.config import settings
from app.sessions import create_session, csrf_token_for
from tests import test_vault_moves as fixture
from tests.postgres import PostgresConnection, database_dsn


class VaultMoveHttpTests(unittest.TestCase):
    def setUp(self):
        fixture.VaultMoveTests.setUp(self)
        configured=replace(settings,database_dsn=database_dsn(str(self.path)),cookie_secure=False)
        for name in ('app.main.settings','app.database.settings','app.sessions.settings'):
            patcher=patch(name,configured)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client=TestClient(main.app,client=('127.0.0.1',50000))
        with PostgresConnection(str(self.path)) as c:
            token=create_session(c,user_id=1,auth_method='oidc')
            csrf=csrf_token_for(c,token)
        self.client.cookies.set(settings.session_cookie_name,token)
        self.headers={'X-CSRF-Token':csrf}
        self.payload={'vault_id':1,'target_class':'DEEP_ARCHIVE','acknowledge_costs':True}

    def test_preview_and_whole_vault_admission_need_no_unrelated_reauthentication(self):
        response=self.client.get('/api/vault/move')
        self.assertEqual(response.status_code,200,response.text)
        targets=response.json()['targets']
        self.assertEqual([t['target_class'] for t in targets],['STANDARD_IA','GLACIER_IR','DEEP_ARCHIVE'])
        self.assertEqual(targets[-1]['minimum_days'],180)
        response=self.client.post('/api/vault/move',json=self.payload,headers=self.headers)
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()['item_count'],1)
        again=self.client.post('/api/vault/move',json=self.payload,headers=self.headers)
        self.assertEqual(again.status_code,409,again.text)

    def test_no_per_file_folder_version_warmer_or_unacknowledged_action(self):
        for extra in ({'path':'report.txt'},{'paths':['report.txt']},{'archive_version_id':self.version_id},
                      {'is_directory':True},{'whole_vault':False}):
            response=self.client.post('/api/vault/move',json={**self.payload,**extra},headers=self.headers)
            self.assertEqual(response.status_code,422,response.text)
        for payload in ({'vault_id':1,'target_class':'STANDARD','acknowledge_costs':True},
                        {'vault_id':1,'target_class':'DEEP_ARCHIVE','acknowledge_costs':False},
                        {'vault_id':2,'target_class':'DEEP_ARCHIVE','acknowledge_costs':True}):
            response=self.client.post('/api/vault/move',json=payload,headers=self.headers)
            self.assertEqual(response.status_code,409,response.text)
        self.assertNotIn('/api/storage-class',main.app.openapi()['paths'])
        with PostgresConnection(str(self.path)) as c:
            self.assertEqual(c.execute('SELECT count(*) AS n FROM jobs').fetchone()['n'],0)

    def test_authentication_and_csrf_gate_movement(self):
        response=self.client.post('/api/vault/move',json=self.payload)
        self.assertEqual(response.status_code,403)
        self.client.cookies.clear()
        response=self.client.post('/api/vault/move',json=self.payload,headers=self.headers)
        self.assertIn(response.status_code,(401,403))
