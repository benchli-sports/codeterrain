from __future__ import annotations

import json

import pytest
from codeterrain_terraform import language, read

from codeterrain import languages
from codeterrain.cli import main

MAIN = '''\
# storage
variable "region" {
  description = "Where everything runs"
  default     = "eu-central-1"
}

locals {
  tags = { Team = "core", Region = var.region }
  name = "logs-${var.region}"
}

resource "aws_s3_bucket" "logs" {
  bucket = local.name
  tags   = local.tags
}

data "aws_iam_policy_document" "read" {
  statement {
    resources = ["${aws_s3_bucket.logs.arn}/*"]
  }
}

module "vpc" {
  source = "./modules/vpc"
  region = var.region
}

output "bucket" {
  value = aws_s3_bucket.logs.id
}
'''


def _names(facts, key):
    return [(f["name"], f["line"]) for f in facts[key]]


def test_definitions_are_named_the_way_terraform_refers_to_them():
    facts = read(MAIN)
    assert [(d["name"], d["kind"], d["line"]) for d in facts["defs"]] == [
        ("var.region", "variable", 2),
        ("local.tags", "local", 8),
        ("local.name", "local", 9),
        ("aws_s3_bucket.logs", "resource", 12),
        ("data.aws_iam_policy_document.read", "data", 17),
        ("module.vpc", "module", 23),
        ("output.bucket", "output", 28),
    ]
    assert facts["defs"][0]["doc"] == "Where everything runs"


@pytest.mark.parametrize("opener", ["<<-", "<<"])
def test_a_heredoc_description_is_the_definition_doc(opener):
    text = (
        'variable "env" {\n'
        f'  description = {opener}EOT\n'
        '    The environment to deploy to.\n'
        '    One of dev or prod.\n'
        '  EOT\n'
        '  default = var.fallback\n'
        '}\n'
    )
    facts = read(text)
    assert facts["defs"][0]["doc"] == "The environment to deploy to.\nOne of dev or prod."
    assert _names(facts, "refs") == [("var.fallback", 6)]


@pytest.mark.parametrize(("written", "doc"), [
    (r"First line\nSecond line", "First line\nSecond line"),
    (r"a\tb \"quoted\" back\\slash", 'a\tb "quoted" back\\slash'),
    (r"café \U0001F600", "café \U0001F600"),
    (r"short \u00e and \q", "short u00e and q"),
    (r"surrogate \uD800", "surrogate uD800"),
])
def test_a_quoted_description_decodes_its_escapes(written, doc):
    text = f'variable "env" {{\n  description = "{written}"\n}}\n'
    assert read(text)["defs"][0]["doc"] == doc


def test_references_carry_the_definition_they_sit_in():
    facts = read(MAIN)
    assert [(r["name"], r["line"], r["scope"]) for r in facts["refs"]] == [
        ("var.region", 8, "local.tags"),
        ("var.region", 9, "local.name"),
        ("local.name", 13, "aws_s3_bucket.logs"),
        ("local.tags", 14, "aws_s3_bucket.logs"),
        ("aws_s3_bucket.logs", 19, "data.aws_iam_policy_document.read"),
        ("var.region", 25, "module.vpc"),
        ("aws_s3_bucket.logs", 29, "output.bucket"),
    ]


@pytest.mark.parametrize("text", [
    'value = "var.region is not read here"\n',
    "# value = var.region\n",
    "// value = var.region\n",
    "/* value =\n var.region */\n",
    'value = "$${var.region}"\n',
    "value = each.value\nother = count.index\nmore = self.id\nlast = path.module\n",
])
def test_text_comments_escapes_and_builtins_are_not_references(text):
    assert read(f'resource "null_resource" "x" {{\n{text}}}\n')["refs"] == []


def test_heredoc_and_nested_templates_are_read():
    text = '''\
resource "aws_instance" "web" {
  user_data = <<-EOT
    region=${var.region}
    %{ if var.debug }debug=on%{ endif }
  EOT
  name = "${upper("${var.prefix}-web")}"
  ami  = data.aws_ami.base.id
  subnet_id = module.vpc.subnets[0]
  depends_on = [aws_iam_role.web]
}
'''
    assert _names(read(text), "refs") == [
        ("var.region", 3), ("var.debug", 4), ("var.prefix", 6), ("data.aws_ami.base", 7),
        ("module.vpc", 8), ("aws_iam_role.web", 9),
    ]


def test_a_dynamic_block_iterator_is_not_a_reference():
    text = """resource "aws_lambda_alias" "live" {
  dynamic "routing_config" {
    for_each = var.weights
    content {
      weight = routing_config.value
    }
  }
  dynamic "ingress_rule" {
    for_each = var.rules
    iterator = rule_item
    content {
      port = rule_item.value.port
      key  = rule_item.key
    }
  }
}
"""
    assert _names(read(text), "refs") == [("var.weights", 3), ("var.rules", 9)]


def test_an_object_literal_inside_an_interpolation_does_not_end_the_string():
    text = (
        'locals { msg = "${lookup({ a = "ok" }, "a")}-suffix" }\n'
        'output "env" { value = var.env }\n'
    )
    facts = read(text)
    assert [d["name"] for d in facts["defs"]] == ["local.msg", "output.env"]
    assert [(r["name"], r["scope"]) for r in facts["refs"]] == [("var.env", "output.env")]


def test_an_attribute_named_iterator_outside_a_dynamic_block_hides_nothing():
    text = '''\
variable "value" {}
locals {
  iterator = var.value
}
output "value" {
  value = var.value
}
'''
    assert _names(read(text), "refs") == [("var.value", 3), ("var.value", 6)]


def test_a_backslash_in_a_heredoc_does_not_escape_an_interpolation():
    text = r'''output "env" {
  value = <<EOT
C:\${var.env}
EOT
}
'''
    assert _names(read(text), "refs") == [("var.env", 3)]


def test_a_comparison_in_an_expression_is_not_a_local():
    text = '''\
variable "names" {}
locals {
  selected = [for name in var.names : name if name == "prod"]
  other    = var.names != null ? (count >= 1) : {}
}
'''
    facts = read(text)
    assert [d["name"] for d in facts["defs"]] == ["var.names", "local.selected", "local.other"]
    assert [(r["name"], r["scope"]) for r in facts["refs"]] == [
        ("var.names", "local.selected"), ("var.names", "local.other"),
    ]


def test_a_key_inside_brackets_or_after_another_token_is_not_a_local():
    text = '''\
locals {
  tags = merge(var.base, { owner = var.owner })
  list = [{ name = var.first }]
}
'''
    assert [d["name"] for d in read(text)["defs"]] == ["local.tags", "local.list"]


@pytest.mark.parametrize("escaped", ["$${", "%%{"])
def test_an_escaped_template_opener_does_not_hide_the_closing_quote(escaped):
    text = f'locals {{ marker = "{escaped}" }}\noutput "env" {{ value = "prod" }}\n'
    assert [d["name"] for d in read(text)["defs"]] == ["local.marker", "output.env"]


def test_unicode_identifiers_keep_their_whole_names():
    text = '''\
variable "région" {}
locals {
  café = var.région
}
output "region" {
  value = var.région
}
'''
    facts = read(text)
    assert [d["name"] for d in facts["defs"]] == ["var.région", "local.café", "output.region"]
    assert _names(facts, "refs") == [("var.région", 3), ("var.région", 6)]


def test_a_for_binding_is_not_a_reference_to_the_configuration():
    text = '''\
variable "items" {}
locals {
  name = "global"
}
output "names" {
  value = [for local in var.items : local.name]
}
output "pairs" {
  value = {for var, module in local.map : var.id => module.vpc}
}
output "after" {
  value = [local.name, var.items]
}
'''
    assert _names(read(text), "refs") == [
        ("var.items", 6), ("local.map", 9), ("local.name", 12), ("var.items", 12),
    ]


@pytest.mark.parametrize("value", [
    "[for local in [for x in var.items : x] : local.name]",
    "[for local in (var.on ? var.items : []) : local.name]",
    "[for local in var.on ? var.items : [] : local.name]",
    "{for local in {for k, v in var.map : k => v} : local.name => 1}",
])
def test_a_for_inside_another_for_collection_keeps_the_outer_binding(value):
    text = f'output "names" {{\n  value = {value}\n}}\n'
    refs = [r["name"] for r in read(text)["refs"]]
    assert "local.name" not in refs
    assert "var.items" in refs or "var.map" in refs


def test_a_template_for_directive_over_a_comprehension_binds_its_names():
    text = '''\
output "list" {
  value = <<EOT
%{ for local in [for x in var.items : x] }${local.name}%{ endfor }
EOT
}
'''
    assert _names(read(text), "refs") == [("var.items", 3)]


@pytest.mark.parametrize("body", ["${local.endfor}${local.name}", "${local.for}${local.name}"])
def test_only_a_directive_opens_or_closes_a_template_binding(body):
    text = f'output "list" {{\n  value = "%{{ for local in var.items }}{body}%{{ endfor }}"\n}}\n'
    assert [r["name"] for r in read(text)["refs"]] == ["var.items"]


@pytest.mark.parametrize("loop", [
    "%{ for local in var.items }${local.name}%{~ endfor }",
    "%{~ for local in var.items ~}${local.name}%{~ endfor ~}",
    "%{~for local in var.items~}${~local.name~}%{~endfor~}",
])
def test_strip_markers_do_not_hide_a_directive(loop):
    text = f'output "names" {{\n  value = "{loop}${{local.name}}"\n}}\n'
    assert [r["name"] for r in read(text)["refs"]] == ["var.items", "local.name"]


def test_a_directive_inside_a_template_for_collection_does_not_start_its_binding():
    # The inner `if` directive's end began the outer loop's binding early.
    text = (
        'output "names" {\n'
        '  value = "%{ for local in ["%{ if var.on }${local.name}%{ endif }"] }'
        '${local.name}%{ endfor }"\n'
        '}\n'
    )
    assert [r["name"] for r in read(text)["refs"]] == ["var.on", "local.name"]


def test_a_template_for_directive_binds_its_names_until_endfor():
    text = '''\
output "list" {
  value = <<EOT
%{ for local in var.items }${local.name}%{ endfor }
${local.name}
EOT
}
'''
    assert _names(read(text), "refs") == [("var.items", 3), ("local.name", 4)]


@pytest.mark.parametrize("inside", [
    '/* quote: " */ var.env',
    'var.env # quote: "\n',
    'var.env // quote: "\n',
    'lookup({ a = "}" }, "a", var.env)',
    'join("", [<<EOT\n"\nEOT\n, var.env])',
])
def test_what_an_interpolation_contains_does_not_end_its_string(inside):
    text = (
        'variable "env" {}\n'
        f'locals {{ msg = "${{ {inside} }}-suffix" }}\n'
        'output "env" { value = var.env }\n'
    )
    facts = read(text)
    assert [d["name"] for d in facts["defs"]] == ["var.env", "local.msg", "output.env"]
    assert [(r["name"], r["scope"]) for r in facts["refs"]] == [
        ("var.env", "local.msg"), ("var.env", "output.env"),
    ]


@pytest.mark.parametrize("value", ['"${var.domain}."', '"${var.domain}{"', '"${var.domain}="'])
def test_string_text_that_reads_like_punctuation_is_text(value):
    text = f'''\
variable "domain" {{}}
locals {{
  fqdn  = {value}
  other = var.domain
}}
'''
    facts = read(text)
    assert [d["name"] for d in facts["defs"]] == ["var.domain", "local.fqdn", "local.other"]
    assert [(r["name"], r["scope"]) for r in facts["refs"]] == [
        ("var.domain", "local.fqdn"), ("var.domain", "local.other"),
    ]


def test_an_attribute_after_an_index_is_not_a_new_reference():
    text = 'output "first" {\n  value = aws_instance.web[0].private_ip\n}\n'
    assert _names(read(text), "refs") == [("aws_instance.web", 2)]


def test_templates_nested_too_deeply_are_refused():
    deep = "x = " + '"${' * 100 + "1" + '}"' * 100 + "\n"
    with pytest.raises(ValueError, match="nested too deeply"):
        read(deep)


PROVIDERS = '''\
terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    random = "~> 3.0"
  }
}

provider "aws" {
  region = var.region
}

provider "aws" {
  region = var.east
  alias  = "east"
}

module "network" {
  source  = "terraform-aws-modules/vpc/aws"
  version = "5.1.0"
  providers = {
    aws = aws.east
  }
  name = var.name
}

module "iam" {
  source = "../../modules/iam"
}

resource "aws_s3_bucket" "logs" {
  provider = aws.east
  version  = "not a module's"
  bucket   = local.name
}

data "aws_ami" "base" {
  provider = aws
}
'''


def test_providers_are_definitions_and_module_calls_carry_their_source():
    facts = read(PROVIDERS)
    assert [(d["name"], d["kind"], d["line"], d["doc"]) for d in facts["defs"]] == [
        ("provider.aws", "required_provider", 3, "source hashicorp/aws, version ~> 5.0"),
        ("provider.random", "required_provider", 7, "version ~> 3.0"),
        ("provider.aws", "provider", 11, ""),
        ("provider.aws.east", "provider", 15, ""),
        ("module.network", "module", 20, "source terraform-aws-modules/vpc/aws, version 5.1.0"),
        ("module.iam", "module", 29, "source ../../modules/iam"),
        ("aws_s3_bucket.logs", "resource", 33, ""),
        ("data.aws_ami.base", "data", 39, ""),
    ]


def test_a_provider_alias_names_its_block_and_what_selects_it():
    # An alias set after the block's first attribute still renames the references read before it.
    refs = [(r["name"], r["line"], r["scope"]) for r in read(PROVIDERS)["refs"]]
    assert refs == [
        ("var.region", 12, "provider.aws"),
        ("var.east", 16, "provider.aws.east"),
        ("provider.aws.east", 24, "module.network"),
        ("var.name", 26, "module.network"),
        ("provider.aws.east", 34, "aws_s3_bucket.logs"),
        ("local.name", 36, "aws_s3_bucket.logs"),
        ("provider.aws", 40, "data.aws_ami.base"),
    ]


def test_object_entries_separated_by_a_colon_are_read():
    facts = read(
        'terraform {\n  required_providers {\n'
        '    aws = { source: "hashicorp/aws", version: "5.0" }\n  }\n}\n'
        'module "m" {\n  source = "./m"\n  providers = { aws: aws.east }\n}\n'
    )
    assert facts["defs"][0]["doc"] == "source hashicorp/aws, version 5.0"
    assert [(r["name"], r["scope"]) for r in facts["refs"]] == [("provider.aws.east", "module.m")]


def test_terrain_def_kind_filters_before_the_match_limit(tmp_path, capsys):
    # More same-named definitions of other kinds than the match limit never hide the one asked.
    root = tmp_path / "repo"
    root.mkdir()
    blocks = [f'variable "v{n}" {{}}\noutput "x{n}" {{}}\n' for n in range(120)]
    text = "".join(f'resource "aws_s3_bucket" "logs{n}" {{}}\n' for n in range(120))
    (root / "a.tf").write_text(
        text.replace('"logs0"', '"logs"') + "".join(blocks), encoding="utf-8"
    )
    (root / "b.tf").write_text(
        "".join(f'resource "aws_instance{n}" "logs" {{}}\n' for n in range(120)), encoding="utf-8"
    )
    (root / "c.tf").write_text('module "logs" {}\n', encoding="utf-8")
    argv = ["--root", str(root), "--cache-dir", str(tmp_path / "cache")]
    assert main(["def", "logs", "--kind", "module", "--json", *argv]) == 0
    rows = json.loads(capsys.readouterr().out)["rows"]
    assert [r["name"] for r in rows] == ["module.logs"]


def test_a_module_description_is_kept_over_its_source():
    facts = read('module "m" {\n  description = "shared"\n  source = "./m"\n}\n')
    assert facts["defs"][0]["doc"] == "shared"


def test_terrain_def_lists_every_definition_of_a_kind(tmp_path, capsys):
    root = tmp_path / "repo"
    (root / "envs" / "dev").mkdir(parents=True)
    (root / "envs" / "dev" / "main.tf").write_text(PROVIDERS, encoding="utf-8")
    (root / "app.py").write_text("class Thing:\n    pass\n", encoding="utf-8")
    argv = ["--root", str(root), "--cache-dir", str(tmp_path / "cache")]
    assert main(["def", "--kind", "module", "--json", *argv]) == 0
    rows = json.loads(capsys.readouterr().out)["rows"]
    assert [(r["name"], r["file"], r["doc"]) for r in rows] == [
        ("module.network", "envs/dev/main.tf",
         "source terraform-aws-modules/vpc/aws, version 5.1.0"),
        ("module.iam", "envs/dev/main.tf", "source ../../modules/iam"),
    ]
    assert main(["def", "aws", "--kind", "provider", "--json", *argv]) == 0
    rows = json.loads(capsys.readouterr().out)["rows"]
    assert [(r["name"], r["line"]) for r in rows] == [("provider.aws", 11)]
    assert main(["def", "--kind", "class", "--json", *argv]) == 0
    assert [r["name"] for r in json.loads(capsys.readouterr().out)["rows"]] == ["app.Thing"]
    assert main(["def", "--kind", "nope", *argv]) == 1
    err = capsys.readouterr().err
    assert "no definition of kind nope; kinds here: class, data, module, provider," in err
    assert main(["def", *argv]) == 2
    assert main(["refs", "provider.aws.east", *argv]) == 0
    assert "2 references" in capsys.readouterr().out


def test_the_pack_answers_def_and_refs_through_terrain(tmp_path, capsys):
    found, problems = languages.discover()
    assert "terraform" in [lang.name for lang in found], problems
    root = tmp_path / "repo"
    (root / "infra").mkdir(parents=True)
    (root / "infra" / "main.tf").write_text(MAIN, encoding="utf-8")
    argv = ["--root", str(root), "--cache-dir", str(tmp_path / "cache")]
    assert main(["def", "logs", "--json", *argv]) == 0
    rows = json.loads(capsys.readouterr().out)["rows"]
    assert [(r["name"], r["file"], r["line"], r["language"]) for r in rows] == [
        ("aws_s3_bucket.logs", "infra/main.tf", 12, "terraform"),
    ]
    assert main(["refs", "var.region", *argv]) == 0
    out = capsys.readouterr().out
    assert "variable var.region  infra/main.tf:2" in out
    assert "3 references" in out


def test_the_language_object_matches_the_interface():
    assert language.interface == languages.INTERFACE
    assert language.suffixes == (".tf",)
