# Keep Vault File identity stable across renames

A Vault File keeps one stable identity while Path History records confirmed
renames and Archive Versions retain the cloud key used at upload time. S3 has no
native rename and versions belong to keys. Stable identity avoids duplicate
files after a safe move while retaining internal immutable operation targets;
it does not expose recoverable cloud history.

## Consequences

Rename synchronization must create and verify the exact bytes/VersionId at the
new key before permanently removing the old key's versions. Automatic rename detection is safe only for an unambiguous
plaintext digest match within the scan window. Ambiguous matches are new Vault
Files: no identity merge or cloud deletion is inferred. There is no manual
rename confirmation or Path History UI (#417, decision #366 Q8); internal Path
History remains necessary to reconcile immutable cloud keys safely.

Current Snapshot is the only behavior (#362, #418): the old key is purged
(every VersionId deleted), never hidden. The keeper must be verified, available
and present in a successful cloud listing. Missing destinations, stale metadata,
unknown cloud content and failed verification/listing cannot authorize deletion.
See ADR-0016.
