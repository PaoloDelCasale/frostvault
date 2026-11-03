# Deployment-owned configuration

Issue #416 (decision #366 Q11) supersedes runtime-managed configuration.
FrostVault reads configuration only from environment variables at process
startup. There is no settings administration API, override catalog, revision
protocol or `system_settings` table. Operators change the deployment and
restart the application; database contents cannot redirect runtime settings.

Migration `0048_essential_administration` removes stored overrides. Before
upgrading, move any required non-default values into the deployment environment.
The downgrade recreates empty tables, not previous overrides.

Metadata backup manifests retain an explicit allow-list of non-secret deployment
values as reconstruction guidance. Credentials and recovery material remain
structurally excluded, including from caller-supplied snapshots. OIDC and trust
boundaries remain governed by ADR-0017 and ADR-0005.
