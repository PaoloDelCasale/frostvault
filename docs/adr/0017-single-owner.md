# One Owner per deployment, with OIDC and the Emergency Password from the environment

> **Status: accepted (Issue #412).** Supersedes ADR-0003, ADR-0010, ADR-0011 and
> ADR-0013, and amends ADR-0004.

FrostVault is a personal archive (decision map #360, #366 Q1/Q2/Q14). A
deployment has exactly one **Owner** who owns every Vault. There are no other
users, global or per-Vault roles, members, sharing, ownership transfer, user
lookup, or user administration. Every authenticated request acts as the Owner,
so authorization collapses to "signed in or not"; the Vault scope only selects
which Vault a request targets.

**Sign-in.** OIDC is configured only from `OIDC_*` environment variables; there
is no runtime-managed OIDC configuration, no `OIDC_SETTINGS_ENCRYPTION_KEY`, and
no Invite. The first successful login from the configured issuer binds its
`(issuer, subject)` Identity to the Owner; afterwards any other subject of that
issuer is refused with `403` and audited, and never replaces an active Session.
We still never trust `email` or other claims to pick the Owner. To move to a new
provider subject, the operator removes the binding from the database (or
restores a fresh deployment); there is deliberately no UI for re-binding.

**Emergency Password.** `BOOTSTRAP_ADMIN_*` are the Owner's local credentials.
They create the Owner on first start and are re-applied at every start, so a
restored metadata backup accepts the current password; a changed password bumps
the Owner's session version and revokes existing Sessions. The password is
accepted only from loopback or `BREAK_GLASS_ALLOWED_CIDRS` (the LAN), with the
backoff, generic failures, auditing and forwarded-header rules of ADR-0004 and
ADR-0005. Requests from outside those networks are refused with `403`.

**Reauthentication** is required only before downloading the Recovery Kit and
before deleting from the cloud (cloud archive/purge, accelerating a purge, and
a Vault Decommission, whose dispositions can delete cloud history). Other
mutations, including settings, need only a valid Session and CSRF token.

**Vault roots.** Source Areas are gone. The Owner may choose any directory under
a Source Volume as a new Vault root; the Source Volume Identity, the fixed
layout of ADR-0012 and the non-overlap check between Vault roots remain.
`BOOTSTRAP_VAULT_*` is removed: Vaults are created only from the UI.

## Consequences

Migration `0044_single_owner` keeps the first administrator (lowest id) as the
Owner, reassigns Identities and audit/settings authorship to it, and drops the
`invites`, `oidc_configuration`, `source_areas`, `vault_members` and
`lookup_rate_limit` tables plus `users.is_admin`, `users.active` and
`oidc_login.invite_id`. Test data has no compatibility guarantee (#362 D6), but
the migration graph stays linear and the downgrade recreates the empty schema.
Removed endpoints answer `404`. Because there is only one principal, the
last-administrator and Source Area exclusivity invariants no longer exist.
