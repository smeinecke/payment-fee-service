# Releasing

All libraries in this repository share the same major and minor version for coordinated contract releases. Language-specific patch releases may differ only for implementation fixes that do not change observable results.

## Version policy

* `payment-fee` Python
* `smeinecke/payment-fee` PHP
* `@smeinecke/payment-fee` TypeScript
* `payment-fee-service`

Same major/minor version means the same public JSON contract and intended calculation behavior.

## Release process

1. Update all package versions to the same new version.
2. Update `contracts/dataset-support.json` if new dataset schema versions are supported.
3. Run `make validate` and `make test` for all languages.
4. Run `make test-conformance`.
5. Commit and tag.
6. Publish Python wheels, PHP package, and npm tarball.

## Dataset versioning

Provider data revisions are pinned in `contracts/data-revisions.json`. Update this file when the conformance cases or contract audit are validated against new data commits. The `resolve-data-revisions` CI job reads this file and checks out the exact SHAs for reproducible builds.
