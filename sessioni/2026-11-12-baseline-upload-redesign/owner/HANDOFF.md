# Handoff — dominio Owner, ADR-0035 / #578 punto 1

## Ambito completato nel checkpoint

Solo dominio Owner e consumatori corrispondenti. Non è il completamento della nuova baseline, della consolidazione file, dei facts, della ritenzione, del publisher o del redesign upload.

- Nuova migrazione temporanea `migrations/versions/0074_owner_singleton.py`, padre esatto `0072_upload_lineage_indexes`; `app.database.HEAD_SCHEMA_REVISION` punta a `0074_owner_singleton`.
- `owner`: `id SMALLINT PRIMARY KEY CHECK (id=1)`, `display_name`, password hash nullable, session version, issuer/subject nullable e vincolo di nullabilità accoppiata. Nessuna tabella/view/alias `users` o `user_identities` nello schema risultante.
- Rimossi `sessions.user_id`, `notifications.user_id`, `jobs.requested_by/approved_by`, `bulk_upload_admissions.requested_by`, `protection_stops.requested_by`, `audit_events.actor_user_id`. `origin` e `approved_at` conservati.
- Deduplica notifiche globale per `dedupe_key`; indici globali per data e problemi aperti al posto degli indici con principal ridondante.
- Audit persistito con `actor` vincolato a `owner/emergency/system`. Emergency login e password reauthentication/backoff sono `emergency`; lavoro automatico è `system`; azioni deliberate sono `owner`. Gli esiti Job derivano il principal da `origin`, non da un richiedente inesistente.
- OIDC: lock della riga singleton per serializzare i primi login; binding della coppia issuer/subject; rifiuto di subject estraneo e di cambio issuer senza rebind esplicito. Il callback conserva il percorso esistente di rifiuto senza rotazione/sostituzione della Session attiva.
- Bootstrap: applicazione delle credenziali correnti dall'ambiente senza username persistito; password cambiata incrementa la versione di revoca. Il nome di login pubblico `/api/me.username` rimane disponibile dall'ambiente, come la verifica dell'Emergency Password. Nome ambiente assente/vuoto non abilita la via dummy.
- Session, CSRF e stream verificano `owner.session_version`; token hash, scadenze, transizioni, rete, forwarded-header trust e backoff non sono stati semplificati.
- Nessun lookup Owner per file: `OWNER_ID=1` sostituisce il vecchio helper con SELECT.
- Portati tutti i consumatori SQL runtime individuati in `app`, seed Playwright/S3 e fixture HEAD. Il worker benchmark mantiene un ramo **esplicito di fixture** per v0.5.5/schema 0072, così il confronto sul baseline fisso non perde il costo del vecchio FK. Non è un adattatore runtime di compatibilità.
- Conteggi della verifica restore metadati su `owner`, preservando il campo pubblico `user_count`. Guardrail schema/backup del checkpoint precedente non modificati (salvo nuovo HEAD).
- OpenAPI confrontato con `frontend/openapi.json`: identico. Nessuna rigenerazione API/front-end necessaria.

## Migrazione e copertura storica

Il checkpoint può attraversare il grafo storico nei test; **non** autorizza l'upgrade di vecchie installazioni. Rifiuta più principal o binding invece di sceglierne uno silenziosamente. Downgrade solo su catalogo Owner-domain vuoto; cataloghi popolati non ricostruiscono username o attribuzioni fittizie. Il downgrade vuoto ricrea la forma storica necessaria ai vecchi round-trip.

I test storici di Users/Identities e quello sul downgrade di una versione missing in `tests/test_database.py` sono mantenuti e fissati esplicitamente a `0072_upload_lineage_indexes`, dove il loro contratto ha ancora senso. Nuove garanzie singleton equivalenti sono in `tests/test_owner_singleton.py` e `tests/test_owner_singleton_postgres.py`. Le precedenti prove di rifiuto del subject estraneo e conservazione della Session attiva in `tests/test_oidc_http.py` restano presenti. I test notifiche che richiedevano un secondo principal ora provano rifiuto del principal impossibile/Session invalida e preservazione delle righe storiche solo-email: non creano un falso secondo Owner.

## Verifiche locali

Python 3.12, `.venv` locale e dipendenze `requirements.txt`; `pglast` aggiunto **solo alla venv** per analisi SQL, nessuna dipendenza di progetto cambiata.

**65 test mirati senza DB superati:**

```text
.venv/bin/python -m unittest \
  tests.test_owner_singleton tests.test_backup_compatibility \
  tests.test_bulk_upload_admission tests.test_audit \
  tests.test_essential_interface tests.test_forwarded_proxy \
  tests.test_upload_benchmark tests.test_schema_state.SchemaStateTests \
  tests.test_metadata_backups.TempDatabaseVerifyProvenCountsTests \
  tests.test_storage_cleanup.StorageCleanupTests.test_cancelling_automatic_cleanup_is_audited_but_not_notified \
  tests.test_storage_cleanup.StorageCleanupTests.test_vault_scan_queues_due_local_cleanup_through_policy_scheduler \
  tests.test_storage_cleanup.StorageCleanupTests.test_all_operation_groups_can_be_cancelled -v
```

Ulteriori verifiche:

- `alembic heads`: unico head `0074_owner_singleton`.
- `compileall` di app, tests, scripts, seed Playwright e nuova migrazione: OK.
- `git diff --check`: OK.
- 1543 statement SQL letterali dei file modificati analizzati con parser PostgreSQL; cardinalità placeholder/tuple controllata dove statica: zero errori. Non è una verifica semantica del catalogo né copre ogni f-string.
- SQL DDL emesso da Alembic per upgrade (17 statement) e downgrade (26) analizzato senza connessione DB: OK.
- `app.openapi() == frontend/openapi.json`: vero.

Log/script/SQL temporanei sono nella cartella di sessione del worktree sorgente:
`/mnt/cache/appdata/t3code-native/worktrees/frostvault/t3-6f012fcd-3e54e5996de9c4f6/sessioni/2026-11-12-baseline-upload-redesign/owner/`.
Solo questo handoff viene versionato; gli altri scratch/log restano ignorati.

## Non verificato / responsabilità di integrazione del parent

1. PostgreSQL 16 assente: nessuna esecuzione reale DDL, FK/index reflection, upgrade/downgrade, concorrenza OIDC/notifiche, restore metadati, password/session revocation su DB reale. Il modulo nuovo PostgreSQL contiene sette test per singleton/assenza campi multiutente, coppia OIDC, race primo binding, revoca Session/CSRF/stream, riapplicazione password senza rebind, deduplica globale/audit, rifiuto downgrade popolato e preservazione checkpoint.
2. Eseguire in PR CI tutti i test PostgreSQL interessati, particolarmente `test_owner_singleton_postgres`, `test_sessions`, `test_oidc_http`, `test_breakglass_http`, `test_reauth_backoff_http`, `test_notifications`, `test_audit_events`, `test_metadata_backups`, `test_metadata_backups_http`, `test_database`, `test_migrations_postgresql`, stream HTTP e test worker/admission/restore-approval/stop/reconnect che usano le fixture portate.
3. Un tentativo iniziale su tutto `tests.test_storage_cleanup` ha prodotto 18 errori per `TEST_POSTGRES_URL` assente e due aspettative mock del vecchio principal; queste ultime sono corrette e i tre test senza DB pertinenti ripassati. Non dichiarare quel modulo interamente verde.
4. Il parent deve ricatenare `0074` dopo il lavoro simultaneo `0073`, poi creare la baseline finale e portare i test storici alle prove equivalenti prima di eliminare il vecchio grafo. Questo branch conserva volutamente il grafo e non modifica i guardrail del precedente checkpoint: alcuni vecchi test di adozione/catalogo non versionato contraddicono già il nuovo rifiuto e richiedono l'integrazione finale del parent, non l'indebolimento delle protezioni.
5. Nessuna misura PostgreSQL/AWS/prestazioni, suite completa, build UI/container o Playwright eseguita qui. Il ramo fixture v0.5.5 del worker benchmark è conservato ma il confronto reale rimane da eseguire in CI.

Nessun servizio avviato/riavviato, nessun DB reale/Docker/AWS, nessuna configurazione di produzione, push/merge/release o mutazione issue. Nessun restart/reload richiesto dal lavoro repository.
