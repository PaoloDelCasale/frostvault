# GitHub OIDC CI role

Provisions a prefix-scoped IAM role that GitHub Actions assumes through OIDC for
the weekly/manual AWS integrity workflow (issue #13).

## Usage

1. Ensure the target bucket already exists and has versioning enabled.
2. From this directory:

```bash
terraform init
terraform apply \
  -var="github_org=example-org" \
  -var="github_repo=frostvault" \
  -var="bucket_name=example-ci-bucket" \
  -var="ci_prefix=ci/github"
```

3. Copy `github_actions_vars` into the GitHub Actions environment `aws-ci`
   (`AWS_CI_ROLE_ARN`, `AWS_CI_TEST_BUCKET`, `AWS_CI_TEST_PREFIX`, `AWS_CI_REGION`).

The attached policy is
[`../archive-bucket/iam/github-oidc-ci-policy.json`](../archive-bucket/iam/github-oidc-ci-policy.json):
list/head on the bucket and read/write/delete only under the CI prefix.
