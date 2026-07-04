#!/usr/bin/env python3.10
"""替换词管理：校验 replacements.txt → 生成上传文件 → 上传到火山引擎。

词表格式（replacements.txt）：每行 "源词|目标词"，# 开头为注释。
上传使用火山引擎替换词管理 API（ListCorrectTable → Create/Update），
需要 IAM 的 AK/SK（与 ASR 的 VOLC_TOKEN 不是同一套凭据）。

用法：
    python3.10 replacements.py              # 校验 + 生成 + 上传
    python3.10 replacements.py --check      # 仅校验源文件
    python3.10 replacements.py --no-upload  # 校验 + 生成上传文件，不上传

.env 需要配置（AK/SK 从 https://console.volcengine.com/iam/keymanage/ 获取）：
    VOLC_APPID     应用 ID（同 ASR）
    VOLC_AK        IAM Access Key
    VOLC_SK        IAM Secret Key
    VOLC_TABLE_NAME 替换词表名（可选，默认 voice_input_replacements）
"""

import os
import sys
import json
import uuid
import hashlib
import hmac
import binascii
import datetime
import urllib.request
import urllib.error
import urllib.parse
from pathlib import Path

# ---------- env ----------
_SCRIPT_DIR = Path(__file__).resolve().parent
_envfile = _SCRIPT_DIR / ".env"
if _envfile.is_file():
    for _line in _envfile.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

APPID = os.environ.get("VOLC_APPID", "")
AK = os.environ.get("VOLC_AK", "")
SK = os.environ.get("VOLC_SK", "")
TABLE_NAME = os.environ.get("VOLC_TABLE_NAME", "voice_input_replacements")

SRC_FILE = _SCRIPT_DIR / "replacements.txt"
OUT_FILE = _SCRIPT_DIR / "replacements_upload.txt"

DOMAIN = "open.volcengineapi.com"
REGION = "cn-north-1"
SERVICE = "speech_saas_prod"
VERSION = "2023-10-30"


# ---------- parse & build ----------
def parse_source(path: Path):
    """Parse replacements.txt → (rules, warnings).

    rules: list of (src, dst) tuples.
    warnings: list of diagnostic strings (duplicates, malformed lines).
    """
    rules = []
    warnings = []
    seen = {}
    if not path.is_file():
        return rules, [f"source file not found: {path}"]
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "|" not in line:
            warnings.append(f"line {lineno}: no '|' separator — skipped: {raw}")
            continue
        src, dst = (x.strip() for x in line.split("|", 1))
        if not src or not dst:
            warnings.append(f"line {lineno}: empty src or dst — skipped: {raw}")
            continue
        if src in seen:
            warnings.append(f"line {lineno}: duplicate src '{src}' — overrides line {seen[src]}")
        seen[src] = lineno
        rules.append((src, dst))
    return rules, warnings


def build_upload_text(rules) -> str:
    """Build the TXT content for Volcano upload (one 'src|dst' per line).

    No trailing newline: the server treats a trailing \\n as an extra
    empty line and rejects it with CorrectTableWrongFormat.
    """
    return "\n".join(f"{src}|{dst}" for src, dst in rules)


# ---------- volcano HMAC-SHA256 signing (per official doc example) ----------
def _sha256_hex(data: bytes) -> str:
    return binascii.b2a_hex(hashlib.sha256(data).digest()).decode("ascii")


def _hmac(key: bytes, data: str) -> bytes:
    return hmac.new(key, data.encode("utf-8"), digestmod=hashlib.sha256).digest()


def _build_headers(canonical_query: str, method: str,
                   content_type: str, body: bytes) -> dict:
    utc_time = datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    utc_day = datetime.datetime.utcnow().strftime("%Y%m%d")
    credential_scope = f"{utc_day}/{REGION}/{SERVICE}/request"
    payload_sign = _sha256_hex(body)
    canonical_headers = (
        f"content-type:{content_type}\n"
        f"host:{DOMAIN}\n"
        f"x-content-sha256:\n"
        f"x-date:{utc_time}\n"
    )
    signed_headers = "content-type;host;x-content-sha256;x-date"
    canonical_request = (
        f"{method}\n/\n{canonical_query}\n"
        f"{canonical_headers}\n{signed_headers}\n{payload_sign}"
    )
    string_to_sign = (
        f"HMAC-SHA256\n{utc_time}\n{credential_scope}\n"
        f"{_sha256_hex(canonical_request.encode())}"
    )
    signing_key = _hmac(
        _hmac(_hmac(_hmac(SK.encode(), utc_day), REGION), SERVICE), "request")
    signature = binascii.b2a_hex(_hmac(signing_key, string_to_sign)).decode("ascii")
    return {
        "content-type": content_type,
        "x-date": utc_time,
        "Authorization": (
            f"HMAC-SHA256 Credential={AK}/{credential_scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        ),
        "Host": DOMAIN,
    }


def _http(url: str, headers: dict, body: bytes, method: str = "POST"):
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8", "replace"))


# ---------- API: JSON endpoints ----------
def _json_call(action: str, payload: dict, method: str = "POST"):
    body = json.dumps({**payload, "Action": action, "Version": VERSION}).encode()
    content_type = "application/json; charset=utf-8"
    query = f"Action={action}&Version={VERSION}"
    headers = _build_headers(query, method, content_type, body)
    return _http(f"https://{DOMAIN}/?{query}", headers, body, method)


# ---------- API: multipart endpoints ----------
def _multipart_call(action: str, fields: dict,
                    file_content: bytes, filename: str = "replacements.txt"):
    boundary = "----VolcBoundary" + uuid.uuid4().hex
    parts = []
    for name, value in {**fields, "Action": action, "Version": VERSION}.items():
        parts.append(f"--{boundary}\r\n".encode())
        parts.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        parts.append(f"{value}\r\n".encode())
    parts.append(f"--{boundary}\r\n".encode())
    parts.append(
        f'Content-Disposition: form-data; name="File"; filename="{filename}"\r\n'.encode())
    parts.append(b"Content-Type: text/plain\r\n\r\n")
    parts.append(file_content)
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    body = b"".join(parts)
    content_type = f"multipart/form-data; boundary={boundary}"
    query = f"Action={action}&Version={VERSION}"
    headers = _build_headers(query, "POST", content_type, body)
    return _http(f"https://{DOMAIN}/?{query}", headers, body, "POST")


# ---------- high-level upload orchestration ----------
def list_tables() -> list:
    """Return list of existing correct tables for this AppID."""
    tables = []
    page = 1
    while True:
        status, resp = _json_call("ListCorrectTable", {
            "AppID": int(APPID),
            "PageNumber": page,
            "PageSize": 100,
            "PreviewSize": 0,
        })
        if status not in (200, 201) or "Result" not in resp:
            raise RuntimeError(f"ListCorrectTable failed: {status} {resp}")
        result = resp["Result"]
        tables.extend(result.get("CorrectTables", []))
        total = result.get("CorrectTableCount", 0)
        if page * 100 >= total:
            break
        page += 1
    return tables


def upload(file_content: bytes) -> str:
    """Create or update the correct table. Returns a human-readable summary."""
    if not AK or not SK:
        raise RuntimeError(
            "missing VOLC_AK / VOLC_SK in .env — "
            "get them from https://console.volcengine.com/iam/keymanage/")
    if not APPID:
        raise RuntimeError("missing VOLC_APPID in .env")

    tables = list_tables()
    existing = next(
        (t for t in tables if t.get("CorrectTableName") == TABLE_NAME), None)

    if existing:
        tid = existing["CorrectTableID"]
        status, resp = _multipart_call("UpdateCorrectTable", {
            "AppID": APPID,
            "TableID": tid,
        }, file_content)
        action = "updated"
    else:
        status, resp = _multipart_call("CreateCorrectTable", {
            "AppID": APPID,
            "TableName": TABLE_NAME,
        }, file_content)
        action = "created"

    if status not in (200, 201):
        raise RuntimeError(f"{action} failed: HTTP {status}\n{json.dumps(resp, indent=2)}")
    result = resp.get("Result", {})
    return (f"table '{TABLE_NAME}' {action} — "
            f"ID={result.get('CorrectTableID', '?')} "
            f"words={result.get('WordCount', '?')}")


# ---------- main ----------
def main():
    args = set(sys.argv[1:])
    check_only = "--check" in args
    no_upload = "--no-upload" in args

    rules, warnings = parse_source(SRC_FILE)
    for w in warnings:
        print(f"[warn] {w}", file=sys.stderr)
    if not rules:
        sys.exit("no valid rules found in replacements.txt")
    print(f"parsed {len(rules)} rule(s) from {SRC_FILE.name}", file=sys.stderr)

    if check_only:
        if warnings:
            sys.exit(1)
        print("all rules valid", file=sys.stderr)
        return

    text = build_upload_text(rules)
    OUT_FILE.write_text(text, encoding="utf-8")
    print(f"wrote {OUT_FILE.name} ({len(rules)} lines)", file=sys.stderr)

    if no_upload:
        return

    try:
        msg = upload(text.encode("utf-8"))
        print(msg, file=sys.stderr)
        print("remember to bind the table to your ASR app in the console "
              "if not already bound.", file=sys.stderr)
    except Exception as e:
        print(f"[error] upload failed: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
