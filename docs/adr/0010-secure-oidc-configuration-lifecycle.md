# Store managed OIDC secrets with a dedicated deployment key

FrostVault manages one generic OIDC provider through a validated lifecycle
module. Environment values remain the initial active fallback. Administrators
use a dedicated interface to save a draft, validate discovery and JWKS, activate
the unchanged validated draft, disable OIDC, or rotate the client secret of an
active managed configuration.

The client secret is encrypted with Fernet before database persistence. The
deployment provides `OIDC_SETTINGS_ENCRYPTION_KEY`; this key is independent of
`ARCHIVE_MASTER_KEY` and is never persisted. The administration interface
returns only `client_secret_configured`, and audit events record only that a
secret was replaced. Metadata database dumps omit the entire
`oidc_configuration` row, so disaster recovery requires re-entering and
validating OIDC configuration.

Activation is one database transaction. It copies the validated draft to the
active fields only when the validated version still matches, appends the audit
event, and removes every in-progress OIDC login transaction. Disabling or
rotating the secret also removes in-progress login transactions. Existing
authenticated Sessions and immutable `(issuer, subject)` Identity bindings are
not changed. Disabling the initial environment fallback persists a disabled
managed row without copying its secret, so emergency disablement does not
depend on the database encryption key.

Validation requires HTTPS, rejects credentials and non-global resolved
addresses, uses bounded HTTP timeouts without redirects, verifies exact issuer
metadata, and parses a non-empty JWKS. For both validation and login, the HTTP
transport connects directly to a resolved global address while retaining the
original hostname for HTTP Host and TLS SNI/certificate verification. DNS
rebinding therefore cannot redirect the actual socket to a private or local
network.

Activation additionally requires a configured Break-glass Login network and at
least one active administrator with a password. Invite-only Identity binding,
Authorization Code + PKCE, and explicit issuer/subject matching remain
unchanged.
