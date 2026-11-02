# codeterrain-terraform

Terraform for [codeterrain](https://github.com/benchli-sports/codeterrain): the definitions and
references of `.tf` files, in `codeterrain def` and `codeterrain refs`.

```
uv tool install "codeterrain[terraform]"
codeterrain def logs                    # resource aws_s3_bucket.logs  infra/storage.tf:12
codeterrain refs var.region             # every expression that reads it, and the block it sits in
codeterrain def --kind module           # every module call, with its file, source and version
codeterrain def --kind required_provider  # every pinned provider and its version constraint
codeterrain refs provider.aws.east      # the resources and module calls that select that provider
```

The `terraform` extra installs this pack beside codeterrain; the command is also installed as
`terrain`. `codeterrain doctor` lists the installed packs (`language packs: terraform 0.2.1`).

Names follow Terraform's own references: `aws_s3_bucket.logs`, `data.aws_ami.base`, `module.vpc`,
`var.region`, `local.tags`, `output.url`, and `provider.aws` for both an entry of
`required_providers` (kind `required_provider`) and a `provider "aws"` block (kind `provider`;
`provider.aws.east` with `alias = "east"`). A module call's doc is its `source` and `version`, as
is a required provider's. A resource's `provider = aws.east` and a module's
`providers = { aws = aws.east }` are references to `provider.aws.east`. References are found in attribute values and inside string
and heredoc templates. The reader is a tokenizer, not an evaluator: it follows no module source,
expands no `count` or `for_each` and reads no `.tfvars`. A resource that selects no provider uses
its default one implicitly; that is not listed as a reference. `.tf.json` files are not read.
