# Keep Vault File identity stable across renames

A Vault File keeps one stable identity while Path History records confirmed
renames and Archive Versions retain the cloud key used at upload time. S3 has no
native rename and versions belong to keys, but users expect one continuous file
history rather than duplicate files after a move; the application therefore
unifies versions across old and new keys without rewriting immutable history.

## Consequences

Rename synchronization must create and verify content at the new key before
hiding the old key. Automatic rename detection is safe only for an unambiguous
plaintext digest match within the scan window. Ambiguous matches are new Vault
Files: no identity merge or cloud deletion is inferred. There is no manual
rename confirmation or Path History UI (#417, decision #366 Q8); internal Path
History remains necessary to reconcile immutable cloud keys safely.

Under Current Snapshot, the old key is purged (every VersionId deleted) rather
than hidden, so only one recoverable Archive Version remains. See ADR-0016.
