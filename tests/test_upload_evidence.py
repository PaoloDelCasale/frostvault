"""Native DB evidence contracts; not AWS/provider or runtime-cutover acceptance."""
import base64
from dataclasses import replace
import hashlib

import psycopg

from app.file_state import FileStateStore
from app.services.upload_snapshot import DEFAULT_PART_SIZE, UploadSnapshot, UploadProofMismatch
from app.services.vault_relocation import enroll_vault_root_identity
from app.upload_evidence import EvidenceConflict, UploadEvidenceStore, append_receipt, decode_snapshot
from tests.test_file_state import AT, LATER, MTIME, SHA, FileStateFixture


def expected(digest=SHA, size=10, mtime=MTIME):
    checksum = base64.b64encode(digest).decode('ascii')
    return UploadSnapshot(size,digest.hex(),'FULL_OBJECT',checksum,DEFAULT_PART_SIZE,(checksum,),mtime)


class UploadEvidenceTests(FileStateFixture):
    def setUp(self):
        super().setUp()
        source = self.identity.parent / 'source'
        source.mkdir()
        with self.db() as connection:
            connection.execute('UPDATE vaults SET source_root=%s WHERE id=2', (str(source),))
            enroll_vault_root_identity(connection,2,str(source))

    def prepared(self, *, start=True):
        with self.db() as connection:
            store = UploadEvidenceStore(connection)
            file = self.observe(store.files)
            snapshot = store.freeze(vault_id=2,file_id=file['id'],snapshot=expected(),at=AT)
            attempt = store.prepare(vault_id=2,file_id=file['id'],snapshot_id=snapshot['id'],
                provider='aws-s3',verification_mode='read_back',storage_class='STANDARD',at=AT)
            if start:
                self.assertIsNotNone(store.start_send(vault_id=2,file_id=file['id'],attempt_id=attempt['id'],at=AT))
        return file,snapshot,attempt

    def candidate(self):
        file,snapshot,attempt = self.prepared()
        receipt = append_receipt(self.db,attempt_id=attempt['id'],response={'VersionId':'native-v1'},at=AT)
        with self.db() as connection:
            candidate = UploadEvidenceStore(connection).materialize(vault_id=2,file_id=file['id'],attempt_id=attempt['id'])
        return file,snapshot,attempt,receipt,candidate

    def published(self):
        file,snapshot,attempt,receipt,candidate = self.candidate()
        with self.db() as connection:
            result = UploadEvidenceStore(connection).publish_verified(vault_id=2,file_id=file['id'],object_id=candidate['id'],
                digest=SHA,method='read_back',read_back_size=10,at=LATER,observation_generation=file['observation_generation'])
        return result,snapshot,attempt,receipt,candidate

    def test_native_expected_evidence_stored_once_and_not_synthesized_from_mtime(self):
        with self.db() as connection:
            store = UploadEvidenceStore(connection)
            file = self.observe(store.files)
            first = store.freeze(vault_id=2,file_id=file['id'],snapshot=expected(),at=AT)
            again = store.freeze(vault_id=2,file_id=file['id'],snapshot=expected(),at=LATER)
            self.assertEqual(first,again)
            self.assertEqual(first['plaintext_sha256'],SHA)
            self.assertEqual(first['part_checksums'],[SHA])
            self.assertEqual(decode_snapshot(first),expected())
            self.assertEqual(first['mtime_ns'],MTIME)
            with self.assertRaises(ValueError):
                store.freeze(vault_id=2,file_id=file['id'],snapshot=replace(expected(),mtime_ns=None),at=LATER)
            self.assertEqual(connection.execute('SELECT count(*) n FROM upload_snapshots').fetchone()['n'],1)

    def test_started_attempt_is_not_another_send_admission_or_mutable_target(self):
        file,snapshot,attempt = self.prepared()
        with self.db() as connection:
            store = UploadEvidenceStore(connection)
            self.assertIsNone(store.start_send(vault_id=2,file_id=file['id'],attempt_id=attempt['id'],at=LATER))
            again = store.prepare(vault_id=2,file_id=file['id'],snapshot_id=snapshot['id'],provider='aws-s3',
                verification_mode='read_back',storage_class='STANDARD',at=LATER)
            self.assertEqual(again['id'],attempt['id'])
            with self.assertRaises(EvidenceConflict):
                store.prepare(vault_id=2,file_id=file['id'],snapshot_id=snapshot['id'],provider='different-namespace',
                    verification_mode='read_back',storage_class='STANDARD',at=LATER)
        with self.assertRaises(psycopg.errors.CheckViolation):
            with self.db() as connection:
                connection.execute("UPDATE upload_attempts SET object_key='changed' WHERE id=%s", (attempt['id'],))
        with self.assertRaises(psycopg.errors.CheckViolation):
            with self.db() as connection:
                connection.execute('DELETE FROM upload_attempts WHERE id=%s', (attempt['id'],))

    def test_independent_receipt_survives_candidate_transaction_rollback(self):
        file,_,attempt = self.prepared()
        fact = append_receipt(self.db,attempt_id=attempt['id'],response={'VersionId':'native-v1'},at=AT)
        with self.assertRaisesRegex(RuntimeError,'abort phase'):
            with self.db() as connection:
                UploadEvidenceStore(connection).materialize(vault_id=2,file_id=file['id'],attempt_id=attempt['id'])
                raise RuntimeError('abort phase')
        with self.db() as connection:
            self.assertEqual(connection.execute('SELECT id FROM upload_receipt_facts').fetchone()['id'],fact['id'])
            self.assertEqual(connection.execute('SELECT count(*) n FROM archive_objects').fetchone()['n'],0)
            self.assertIsNone(FileStateStore(connection).get(vault_id=2,file_id=file['id'])['pending_snapshot_id'])

    def test_receipt_allows_late_and_malformed_ack_without_live_job_or_source(self):
        _,_,attempt = self.prepared()
        with self.db() as connection:
            connection.execute('UPDATE vaults SET enabled=FALSE WHERE id=2')
            connection.execute('UPDATE upload_attempts SET settled_at=%s WHERE id=%s', (LATER,attempt['id']))
        fact = append_receipt(self.db,attempt_id=attempt['id'],response={'ETag':'not-ownership',
            'ResponseMetadata':{'HTTPHeaders':{'Authorization':'must-not-be-stored'}}},at=LATER)
        self.assertIsNone(fact['provider_version_id'])
        self.assertEqual(fact['response'],{'ETag':'not-ownership'})
        with self.db() as connection:
            self.assertEqual(connection.execute('SELECT count(*) n FROM jobs').fetchone()['n'],0)
            self.assertEqual(connection.execute('SELECT count(*) n FROM upload_receipt_facts').fetchone()['n'],1)

    def test_duplicate_facts_deduplicate_but_contradictions_are_preserved_not_elected(self):
        file,_,attempt = self.prepared()
        first = append_receipt(self.db,attempt_id=attempt['id'],response={'VersionId':'v1'},at=AT)
        again = append_receipt(self.db,attempt_id=attempt['id'],response={'VersionId':'v1'},at=LATER)
        self.assertEqual(first['id'],again['id'])
        append_receipt(self.db,attempt_id=attempt['id'],response={'VersionId':'v2'},at=LATER)
        with self.db() as connection:
            with self.assertRaises(EvidenceConflict):
                UploadEvidenceStore(connection).materialize(vault_id=2,file_id=file['id'],attempt_id=attempt['id'])
            self.assertEqual(connection.execute('SELECT count(*) n FROM upload_receipt_facts').fetchone()['n'],2)
            self.assertIsNone(UploadEvidenceStore(connection).current(vault_id=2,file_id=file['id']))

    def test_unverified_or_merely_verified_available_row_never_self_elects(self):
        file,_,_,_,candidate = self.candidate()
        with self.db() as connection:
            self.assertIsNone(UploadEvidenceStore(connection).current(vault_id=2,file_id=file['id']))
        with self.assertRaises(psycopg.errors.CheckViolation):
            with self.db() as connection:
                connection.execute('UPDATE file_state SET current_snapshot_id=%s,pending_snapshot_id=NULL WHERE id=%s',
                                   (candidate['id'],file['id']))
        with self.db() as connection:
            connection.execute("""UPDATE archive_objects SET integrity='verified',verification_method='read_back',
                verified_sha256=%s,verified_at=%s WHERE id=%s""", (SHA,LATER,candidate['id']))
            self.assertIsNone(UploadEvidenceStore(connection).current(vault_id=2,file_id=file['id'],recoverable_only=True))

    def test_atomic_verification_pointer_and_exact_local_generation(self):
        file,_,_,_,candidate = self.published()
        self.assertEqual(file['current_snapshot_id'],candidate['id'])
        self.assertIsNone(file['pending_snapshot_id'])
        self.assertEqual(file['matched_snapshot_id'],candidate['id'])
        with self.db() as connection:
            current = UploadEvidenceStore(connection).current(vault_id=2,file_id=file['id'],recoverable_only=True)
            self.assertEqual(current['id'],candidate['id'])
            self.assertEqual(current['mtime_ns'],MTIME)
            self.assertEqual(current['plaintext_sha256'],SHA)
        with self.assertRaises(psycopg.errors.CheckViolation):
            with self.db() as connection:
                connection.execute("UPDATE archive_objects SET availability='purged' WHERE id=%s", (candidate['id'],))

    def test_pointer_failure_rolls_back_verification_but_not_independent_receipt(self):
        file,_,_,fact,candidate = self.candidate()
        with self.db() as connection:
            connection.execute("""CREATE FUNCTION test_reject_pointer() RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN RAISE EXCEPTION 'injected pointer rejection' USING ERRCODE='23514'; END $$;
                CREATE TRIGGER test_reject_pointer BEFORE UPDATE OF current_snapshot_id ON file_state
                    FOR EACH ROW EXECUTE FUNCTION test_reject_pointer();""")
        with self.db() as connection:
            with self.assertRaises(psycopg.errors.CheckViolation):
                UploadEvidenceStore(connection).publish_verified(vault_id=2,file_id=file['id'],object_id=candidate['id'],
                    digest=SHA,method='read_back',read_back_size=10,at=LATER)
            self.assertEqual(connection.execute('SELECT integrity FROM archive_objects').fetchone()['integrity'],'unverified')
            self.assertEqual(connection.execute('SELECT id FROM upload_receipt_facts').fetchone()['id'],fact['id'])
            self.assertIsNone(FileStateStore(connection).get(vault_id=2,file_id=file['id'])['current_snapshot_id'])

    def test_newer_local_generation_is_not_overwritten_at_publication(self):
        file,_,_,_,candidate = self.candidate()
        with self.db() as connection:
            store = UploadEvidenceStore(connection)
            changed = self.observe(store.files,size=99,mtime_ns=MTIME+1,at=LATER)
            current = store.publish_verified(vault_id=2,file_id=file['id'],object_id=candidate['id'],digest=SHA,
                method='read_back',read_back_size=10,at=LATER,observation_generation=file['observation_generation'])
            self.assertEqual(current['observation_generation'],changed['observation_generation'])
            self.assertEqual(current['local_size'],99)
            self.assertIsNone(current['local_sha256'])
            self.assertIsNone(current['matched_snapshot_id'])
            self.assertEqual(current['current_snapshot_id'],candidate['id'])

    def test_reappearance_hold_uses_frozen_digest_not_cached_same_stat_hint(self):
        file,_,_,_,candidate = self.published()
        different = hashlib.sha256(b'differents').digest()
        with self.db() as connection:
            store = UploadEvidenceStore(connection)
            missing = store.files.mark_missing(vault_id=2,file_id=file['id'],path_generation=file['path_generation'],
                observation_generation=file['observation_generation'],at=LATER)
            self.assertIsNotNone(missing)
            appeared = self.observe(store.files,at=LATER)  # Identical size/ns does NOT prove identical bytes.
            self.assertEqual(appeared['local_sha256'],SHA)
            self.assertEqual(appeared['held_snapshot_id'],candidate['id'])
            frozen = store.freeze(vault_id=2,file_id=file['id'],snapshot=expected(digest=different),at=LATER)
            with self.assertRaisesRegex(EvidenceConflict,'Changed reappearance'):
                store.prepare(vault_id=2,file_id=file['id'],snapshot_id=frozen['id'],provider='aws-s3',
                    verification_mode='read_back',storage_class='STANDARD',at=LATER)

    def test_current_proof_does_not_retain_a_chain_of_obsolete_keeper_rows(self):
        file,old_snapshot,old_attempt,old_receipt,old = self.published()
        digest = hashlib.sha256(b'new').digest()
        # DB reference-graph contract, not cleanup/GC authorization: simulate
        # already-closed obligations; no provider data is touched by this test.
        with self.db() as connection:
            store = UploadEvidenceStore(connection)
            connection.execute('UPDATE upload_attempts SET settled_at=%s WHERE id=%s', (LATER,old_attempt['id']))
            observed = self.observe(store.files,size=3,mtime_ns=MTIME+1,at=LATER)
            frozen = store.freeze(vault_id=2,file_id=file['id'],snapshot=expected(digest,3,MTIME+1),at=LATER)
            attempt = store.prepare(vault_id=2,file_id=file['id'],snapshot_id=frozen['id'],provider='aws-s3',
                verification_mode='read_back',storage_class='STANDARD',at=LATER)
            self.assertEqual(attempt['expected_current_id'],old['id'])
            self.assertIsNotNone(store.start_send(vault_id=2,file_id=file['id'],attempt_id=attempt['id'],at=LATER))
        append_receipt(self.db,attempt_id=attempt['id'],response={'VersionId':'replacement-v2'},at=LATER)
        with self.db() as connection:
            store = UploadEvidenceStore(connection)
            candidate = store.materialize(vault_id=2,file_id=file['id'],attempt_id=attempt['id'])
            store.publish_verified(vault_id=2,file_id=file['id'],object_id=candidate['id'],digest=digest,
                method='read_back',read_back_size=3,at=LATER,observation_generation=observed['observation_generation'])
            connection.execute("UPDATE archive_objects SET availability='purged' WHERE id=%s", (old['id'],))
            connection.execute('DELETE FROM archive_objects WHERE id=%s', (old['id'],))
            connection.execute('DELETE FROM upload_receipt_facts WHERE id=%s', (old_receipt['id'],))
            connection.execute('DELETE FROM upload_attempts WHERE id=%s', (old_attempt['id'],))
            connection.execute('DELETE FROM upload_snapshots WHERE id=%s', (old_snapshot['id'],))
            self.assertEqual(store.current(vault_id=2,file_id=file['id'],recoverable_only=True)['id'],candidate['id'])
            self.assertEqual(connection.execute('SELECT count(*) n FROM archive_objects').fetchone()['n'],1)

    def test_exact_published_receipt_is_idempotent_without_rewriting_proof(self):
        file,_,attempt,_,candidate = self.published()
        with self.db() as connection:
            store = UploadEvidenceStore(connection)
            before = store.current(vault_id=2,file_id=file['id'])
            same = store.materialize(vault_id=2,file_id=file['id'],attempt_id=attempt['id'])
            self.assertEqual(same['id'],candidate['id'])
            repeated = store.publish_verified(vault_id=2,file_id=file['id'],object_id=candidate['id'],
                digest=SHA,method='read_back',read_back_size=10,at=LATER)
            self.assertEqual(repeated,file)
            self.assertEqual(store.current(vault_id=2,file_id=file['id']),before)
            self.assertEqual(connection.execute('SELECT count(*) n FROM archive_objects').fetchone()['n'],1)

    def test_scope_revocation_rejects_unsent_attempt_without_rebasing_it(self):
        file,_,attempt = self.prepared(start=False)
        with self.db() as connection:
            store = UploadEvidenceStore(connection)
            connection.execute("UPDATE vaults SET root_identity='different-root' WHERE id=2")
            self.assertIsNone(store.start_send(vault_id=2,file_id=file['id'],attempt_id=attempt['id'],at=LATER))
            self.assertIsNone(connection.execute('SELECT send_started_at FROM upload_attempts').fetchone()['send_started_at'])
            connection.execute('UPDATE vaults SET root_identity=%s WHERE id=2', (attempt['root_identity'],))
            self.assertIsNotNone(store.start_send(vault_id=2,file_id=file['id'],attempt_id=attempt['id'],at=LATER))

    def test_contradictory_ack_cannot_be_overridden_by_readback_or_head(self):
        file,_,attempt = self.prepared()
        append_receipt(self.db,attempt_id=attempt['id'],response={'VersionId':'v1',
            'ChecksumSHA256':base64.b64encode(bytes(32)).decode('ascii')},at=AT)
        with self.db() as connection:
            store = UploadEvidenceStore(connection)
            candidate = store.materialize(vault_id=2,file_id=file['id'],attempt_id=attempt['id'])
            with self.assertRaises(UploadProofMismatch):
                store.publish_verified(vault_id=2,file_id=file['id'],object_id=candidate['id'],
                    digest=SHA,method='read_back',read_back_size=10,at=LATER,
                    observed_proof={'VersionId':'v1','ChecksumSHA256':expected().checksum_sha256,'ChecksumType':'FULL_OBJECT'})
            self.assertEqual(connection.execute('SELECT integrity FROM archive_objects').fetchone()['integrity'],'unverified')
            self.assertIsNone(store.current(vault_id=2,file_id=file['id']))

    def test_late_contradiction_disables_recovery_without_history_fallback(self):
        file,_,attempt,_,candidate = self.published()
        append_receipt(self.db,attempt_id=attempt['id'],response={'VersionId':'contradictory-v2'},at=LATER)
        with self.db() as connection:
            store = UploadEvidenceStore(connection)
            self.assertEqual(store.current(vault_id=2,file_id=file['id'])['id'],candidate['id'])
            self.assertIsNone(store.current(vault_id=2,file_id=file['id'],recoverable_only=True))
