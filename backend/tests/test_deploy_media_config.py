"""Guards on the deploy files that put media in MinIO behind Caddy's /media.

These files are not Python, so nothing else would notice a regression until a
deploy: a MinIO port published to the internet, the backend signing with the
root account, a minio-init policy JSON that no longer parses, or the two
Caddyfiles drifting apart so the local test proves nothing about production.
The runtime behaviour itself (presigned GET through /media, anonymous 403/404s,
blocked response-* overrides) is covered by tests/test_minio_integration.py
against a real MinIO + Caddy; this file only reads the files.

Dependency-light: no docker, no network. Needs PyYAML (skipped without it).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent

MINIO_IMAGE_RE = re.compile(r"^pgsty/minio:RELEASE\.\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}Z$")
BUCKET_VARS = ("PRODUCER_ASSETS_BUCKET", "PRODUCER_MEDIA_BUCKET", "BRAND_DOCS_BUCKET",
               "APPROVED_MEDIA_BUCKET")


def _read(rel: str) -> str:
    return (_REPO_ROOT / rel).read_text(encoding="utf-8")


class _ComposeLoader(yaml.SafeLoader):
    """SafeLoader that reads compose's merge tags (!override, !reset) as plain values."""


def _plain(loader, node):
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node)
    return loader.construct_scalar(node)


for _tag in ("!override", "!reset"):
    _ComposeLoader.add_constructor(_tag, _plain)


def _compose(rel: str = "docker-compose.yml") -> dict:
    return yaml.load(_read(rel), Loader=_ComposeLoader)


def _services() -> dict:
    return _compose()["services"]


# --- docker-compose.yml ----------------------------------------------------------

def test_minio_is_pinned_private_and_healthchecked():
    minio = _services()["minio"]
    assert MINIO_IMAGE_RE.match(minio["image"]), minio["image"]
    assert minio["command"] == ["server", "/data"]
    # Browsers reach objects only through Caddy's /media route.
    assert "ports" not in minio
    assert "minio" not in (_compose("docker-compose.local.yml").get("services") or {})
    env = minio["environment"]
    assert env["MINIO_BROWSER"] == "off"
    assert "MINIO_DOMAIN" not in env  # virtual-host style would break path-style signing
    test = minio["healthcheck"]["test"]
    assert test[:2] == ["CMD", "curl"] and test[-1] == "http://127.0.0.1:9000/minio/health/ready"
    assert minio["volumes"] == ["studio-minio:/data"]
    assert _compose()["volumes"]["studio-minio"]["name"] == "studio42_minio"


def test_backend_signs_with_the_app_user_against_minio():
    backend = _services()["backend"]
    env = backend["environment"]
    assert env["STORAGE_BACKEND"] == "s3"
    assert env["S3_ENDPOINT"] == "http://minio:9000"
    assert env["S3_PUBLIC_BASE_URL"] == "${PUBLIC_URL}/media"
    # Never the root account: its key would be in every presigned URL.
    assert env["S3_ACCESS_KEY"].startswith("${S3_ACCESS_KEY:?")
    assert env["S3_SECRET_KEY"].startswith("${S3_SECRET_KEY:?")
    assert "MINIO_ROOT" not in env["S3_ACCESS_KEY"] + env["S3_SECRET_KEY"]
    # A presigned URL for another region is refused by MinIO.
    assert env["S3_REGION"] == _services()["minio"]["environment"]["MINIO_SITE_REGION"]
    deps = {name: d["condition"] for name, d in backend["depends_on"].items()}
    assert deps["minio"] == "service_healthy"
    assert deps["minio-init"] == "service_completed_successfully"


def test_backend_never_gets_the_minio_root_account():
    """env_file hands the backend the whole .env, which holds MinIO's root
    pair for minio and minio-init. `environment:` wins over env_file, so the
    backend blanks both names; the local override must not bring them back."""
    backend = _services()["backend"]
    assert ".env" in backend["env_file"]
    env = backend["environment"]
    assert env["MINIO_ROOT_USER"] == "" and env["MINIO_ROOT_PASSWORD"] == ""
    assert not any("MINIO_ROOT" in str(v) for v in env.values())
    local_env = _compose("docker-compose.local.yml")["services"]["backend"].get("environment") or {}
    assert not any(k.startswith("MINIO_ROOT") for k in local_env)
    # Only the two MinIO services get the root pair.
    holders = sorted(name for name, svc in _services().items()
                     if (svc.get("environment") or {}).get("MINIO_ROOT_PASSWORD"))
    assert holders == ["minio", "minio-init"]


def test_local_override_points_media_at_the_local_caddy():
    local = _compose("docker-compose.local.yml")["services"]
    assert local["backend"]["environment"]["S3_PUBLIC_BASE_URL"] == "http://localhost:8088/media"
    assert "./web/Caddyfile.local:/etc/caddy/Caddyfile:ro" in local["web"]["volumes"]


def test_minio_init_is_a_one_shot_on_the_same_image():
    services = _services()
    init = services["minio-init"]
    assert init["image"] == services["minio"]["image"]
    assert init["restart"] == "no"
    assert init["depends_on"]["minio"]["condition"] == "service_healthy"
    assert "ports" not in init
    # The image's VOLUME /data would otherwise leave an anonymous volume per run.
    assert init["tmpfs"] == ["/data"]


def test_minio_init_bucket_defaults_match_the_backend_config():
    config_src = _read("backend/app/config.py")
    env = _services()["minio-init"]["environment"]
    for var in BUCKET_VARS:
        default = re.search(rf'os\.getenv\("{var}", "([^"]+)"\)', config_src).group(1)
        assert env[var] == f"${{{var}:-{default}}}", var


def _init_script() -> str:
    # Compose turns $$ into a literal $ before the shell sees the script.
    return _services()["minio-init"]["command"][0].replace("$$", "$")


def _heredoc_json(script: str, target: str, subst: dict[str, str]) -> dict:
    m = re.search(rf"cat > {re.escape(target)} <<EOF\n(.*?)\nEOF\n", script, re.S)
    assert m, f"no heredoc for {target}"
    body = m.group(1)
    for name, value in subst.items():
        body = body.replace(name, value)
    assert "$" not in body, f"unexpanded shell variable left in {target}: {body}"
    return json.loads(body)


def test_minio_init_public_policy_is_get_object_only():
    script = _init_script()
    code = [ln.strip() for ln in script.splitlines() if not ln.lstrip().startswith("#")]
    # A canned policy like `download` also grants ListBucket (keys enumerable).
    # The three private buckets are reset to private on every run, so an
    # anonymous policy set on one does not survive the next deploy.
    assert [ln for ln in code if "mc anonymous" in ln] == [
        'mc anonymous set-json /tmp/public.json "studio/$APPROVED_MEDIA_BUCKET"',
        'mc anonymous set none "studio/$b"',
    ]
    loop = code[code.index('mc anonymous set none "studio/$b"') - 1]
    assert loop == "for b in $PRODUCER_ASSETS_BUCKET $PRODUCER_MEDIA_BUCKET $BRAND_DOCS_BUCKET; do"
    policy = _heredoc_json(script, "/tmp/public.json",
                           {"$APPROVED_MEDIA_BUCKET": "approved-instagram"})
    (stmt,) = policy["Statement"]
    assert stmt["Effect"] == "Allow" and stmt["Principal"] == {"AWS": ["*"]}
    assert stmt["Action"] == ["s3:GetObject"]
    assert stmt["Resource"] == ["arn:aws:s3:::approved-instagram/*"]


def test_minio_init_app_policy_covers_exactly_the_four_buckets():
    script = _init_script()
    buckets = ["producer-assets", "producer-media", "brand-docs", "approved-instagram"]
    # What the shell loop builds, before ${resources%,} drops the last comma.
    resources = "".join(f'"arn:aws:s3:::{b}","arn:aws:s3:::{b}/*",' for b in buckets)
    policy = _heredoc_json(script, "/tmp/studio-app.json", {
        "${resources%,}": resources[:-1], "$APPROVED_MEDIA_BUCKET": "approved-instagram",
    })
    objects, bucket_policy, deny = policy["Statement"]
    assert objects["Effect"] == "Allow"
    assert set(objects["Action"]) == {
        "s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:ListBucket",
        "s3:GetBucketLocation", "s3:CreateBucket", "s3:AbortMultipartUpload",
        "s3:ListMultipartUploadParts",
    }
    assert objects["Resource"] == [
        r for b in buckets for r in (f"arn:aws:s3:::{b}", f"arn:aws:s3:::{b}/*")]
    # Bucket policies only on the Instagram bucket: with them on a private
    # bucket a leaked app secret could make it anonymous-readable.
    assert bucket_policy == {"Effect": "Allow",
                             "Action": ["s3:PutBucketPolicy", "s3:GetBucketPolicy"],
                             "Resource": ["arn:aws:s3:::approved-instagram"]}
    # MinIO lets any user mint access keys for itself unless denied, and such
    # a key outlives a rotation of S3_SECRET_KEY.
    assert deny == {"Effect": "Deny", "Action": ["admin:*"]}
    for stmt in (objects, bucket_policy):
        assert not any(a.startswith("admin:") or a == "s3:*" for a in stmt["Action"])
    for var in BUCKET_VARS:
        assert f"${var}" in script.split("buckets=", 1)[1].split("\n", 1)[0], var


def test_minio_init_refuses_the_root_account_as_app_user():
    script = _init_script()
    assert 'if [ "$S3_ACCESS_KEY" = "$MINIO_ROOT_USER" ]; then' in script
    assert script.index("exit 1") < script.index("mc alias set")
    assert 'mc admin user add studio "$S3_ACCESS_KEY" "$S3_SECRET_KEY"' in script
    assert 'mc admin policy attach studio studio-app --user "$S3_ACCESS_KEY"' in script


def test_every_required_compose_variable_is_in_the_deploy_example():
    required = set(re.findall(r"\$\{([A-Z][A-Z0-9_]*):\?", _read("docker-compose.yml")))
    assert {"MINIO_ROOT_USER", "MINIO_ROOT_PASSWORD", "S3_ACCESS_KEY", "S3_SECRET_KEY"} <= required
    example = _read(".env.deploy.example")
    missing = sorted(v for v in required if not re.search(rf"^{v}=", example, re.M))
    assert not missing, f"docker-compose.yml requires these but .env.deploy.example lacks them: {missing}"


# --- web/Caddyfile + web/Caddyfile.local -------------------------------------------

def _media_block(rel: str) -> list[str]:
    """The `handle /media/* { ... }` block, comments and indentation dropped."""
    text = _read(rel)
    start = text.index("handle /media/* {")
    # Placeholders like {upstream_hostport} are balanced, so plain counting works.
    depth, end = 0, None
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                end = i
                break
    assert end is not None, f"unbalanced /media block in {rel}"
    lines = [ln.strip() for ln in text[start : end + 1].splitlines()]
    return [ln for ln in lines if ln and not ln.startswith("#")]


def test_both_caddyfiles_share_the_same_media_block():
    assert _media_block("web/Caddyfile") == _media_block("web/Caddyfile.local")


@pytest.mark.parametrize("rel", ["web/Caddyfile", "web/Caddyfile.local"])
def test_media_block_rules(rel):
    block = "\n".join(_media_block(rel))
    # Only object GET/HEAD reach MinIO; its admin API never does.
    assert "method GET HEAD" in block
    assert "path_regexp ^/media/[a-z0-9][a-z0-9.-]+/.+" in block
    assert "not path /media/minio/*" in block
    # The Host the URL was presigned for; the app's credentials stay at the edge.
    assert "uri strip_prefix /media" in block
    assert "reverse_proxy minio:9000 {" in block
    assert "header_up Host {upstream_hostport}" in block
    assert "header_up -Cookie" in block and "header_up -Authorization" in block
    # A7: MinIO counts a request as presigned only with an exact X-Amz-Credential.
    assert block.count("not query X-Amz-Credential=*") == 2
    assert "X-Amz-Signature" not in block
    assert "rewrite @unsigned ?" in block
    assert "header >X-Content-Type-Options nosniff" in block
    # Anything else under /media is a 404, never the SPA's index.html.
    assert block.splitlines()[-4:] == ["handle {", "respond 404", "}", "}"]
    text = _read(rel)
    # handle_path can't carry the method matcher; compression skips /media.
    assert "handle_path" not in text
    assert "@compress not path /media/*" in text and "encode @compress zstd gzip" in text


@pytest.mark.parametrize("query,blocked", [
    ("response-content-type=text/html", True),
    ("Response-Content-Type=text/html", True),
    ("v=1&response-cache-control=no-store", True),
    ("response-content-type=", True),
    ("v=1", False),
    ("", False),
    ("v=response-foo", False),
])
def test_the_override_matcher_catches_response_keys(query, blocked):
    block = "\n".join(_media_block("web/Caddyfile"))
    pattern = re.search(r'\{http\.request\.uri\.query\}\.matches\("([^"]+)"\)', block).group(1)
    # Caddy uses RE2; this pattern means the same in Python's re.
    assert bool(re.search(pattern, query)) is blocked
