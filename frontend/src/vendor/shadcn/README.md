# Vendored shadcn stylesheet

`tailwind.css` is an **unmodified, byte-for-byte** copy of
`shadcn@4.22.1/dist/tailwind.css`, imported by `src/index.css`. It retains the
accordion keyframes, data variants, scrollbar, scroll-fade and shimmer utilities
without installing the shadcn **CLI** and its unused JavaScript dependency tree
(including vulnerable deepmerge). The vendored UI components are unchanged.
See [issue #405](https://github.com/PaoloDelCasale/frostvault/issues/405) and
[ADR 0008](../../../../docs/adr/0008-tailwind-shadcn-design-system.md).

## Provenance and license

- npm package: [`shadcn@4.22.1`](https://www.npmjs.com/package/shadcn/v/4.22.1)
- Tarball: <https://registry.npmjs.org/shadcn/-/shadcn-4.22.1.tgz>
- npm `dist.integrity`:
  `sha512-LbB4Lro+b0qSVki/tgL5edtKylEnXe1ppxMJC12d7NHPgaT+eSgJGMQULwP72V7X9tbiUq7xfx3VDAfB7jdGoA==`
- Upstream tag: `shadcn@4.22.1`; commit:
  `daaf156ee00ed91cc7e6bdd937c1b4973152d141`
- [Original CSS at that commit](https://github.com/shadcn-ui/ui/blob/daaf156ee00ed91cc7e6bdd937c1b4973152d141/packages/shadcn/src/tailwind.css)
  matches the published CSS exactly: 16,340 bytes, SHA-256
  `4c371f7a1ff5d219ae2f7ff28bd256b4346fd546fe46fbae22092e57db2f0fae`.
- License: **MIT**, Copyright (c) 2023 shadcn. The npm package declares MIT but
  omits the license file; adjacent `LICENSE.md` is copied verbatim from the
  [upstream repository at the same commit](https://github.com/shadcn-ui/ui/blob/daaf156ee00ed91cc7e6bdd937c1b4973152d141/LICENSE.md).
  Its SHA-256 is `1564074e13439397221ffd522e2e504d56561994a23d371aa5e3ad43e4f5423f`.

## Contributor workflow

Compose existing vendored components under `src/components/ui/`; the app does
not import the CLI's JavaScript. Do not re-add `shadcn` as an installed dependency
just for its CSS. If using the upstream CLI to generate a component, run an
explicitly reviewed version in a disposable directory outside this project,
review the generated source and dependencies, and bring in only what is needed.

To refresh this stylesheet, obtain an explicitly reviewed upstream release,
verify npm integrity and the matching Git source, preserve the complete MIT
notice, replace the stylesheet as a unit, and update this provenance and the
vendoring regression hashes together. No network downloads occur in tests.
Run the targeted dependency/style tests, clean full/runtime audits and strict
audit gate, then obtain build/browser/full test evidence from PR CI.
