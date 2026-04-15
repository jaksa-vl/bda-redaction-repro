#!/usr/bin/env python3
"""
BDA PII Redaction Repro

Demonstrates that AWS Bedrock Data Automation (BDA) does not redact PII
from document outputs even when the project is configured with
DETECTION_AND_REDACTION mode and all PII entity types.

Required env vars (or create a .env file):
    BDA_BUCKET          - S3 bucket for staging/output
    BDA_PROJECT_ARN     - BDA project ARN (with redaction config)
    BDA_PROFILE_ARN     - BDA profile ARN

Optional env vars:
    BDA_REGION          - AWS region (default: us-east-1)
    BDA_PREFIX          - S3 key prefix (default: bda-repro)

Usage:
    cp .env.example .env   # fill in your values
    python repro.py --save-artifacts ./artifacts --cleanup
    python repro.py --text-pdf
    python repro.py --fixture path/to/file.pdf
"""

import argparse
import io
import json
import os
import re
import time
import uuid
from pathlib import Path

import boto3

# ---------------------------------------------------------------------------
# Fixture: obvious PII that BDA should detect and redact
# ---------------------------------------------------------------------------

FIXTURE_LINES = [
    "NAME: John Q Public",
    "EMAIL: john.public@example.com",
    "PHONE: 415-555-1212",
    "SSN: 123-45-6789",
    "ADDRESS: 123 Main St, Springfield, IL 62704",
]

EXPECTED_PII_VALUES = [
    "John Q Public",
    "john.public@example.com",
    "415-555-1212",
    "123-45-6789",
    "123 Main St, Springfield, IL 62704",
]


def _load_truetype_font(size: int = 48):
    """Try common system TrueType font paths across platforms."""
    from PIL import ImageFont

    candidates = [
        # macOS
        "/System/Library/Fonts/Helvetica.ttc",
        "/System/Library/Fonts/SFNSMono.ttf",
        "/Library/Fonts/Arial.ttf",
        # Linux
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        # Windows
        "C:/Windows/Fonts/arial.ttf",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue

    return ImageFont.load_default(size=size)


def generate_image_based_pdf(lines: list[str]) -> bytes:
    """Generate a PDF with text rendered as an image (forces OCR path)."""
    from reportlab.lib.pagesizes import LETTER
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    from PIL import Image, ImageDraw, ImageFont

    # Render text to a high-resolution image
    width, height = 2400, 800
    img = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(img)

    font = _load_truetype_font(size=48)

    y = 80
    for line in lines:
        draw.text((100, y), line, fill="black", font=font)
        y += 100

    img_buf = io.BytesIO()
    img.save(img_buf, format="PNG", dpi=(300, 300))
    img_buf.seek(0)

    # Embed image in PDF
    pdf_buf = io.BytesIO()
    c = canvas.Canvas(pdf_buf, pagesize=LETTER)
    page_w, page_h = LETTER
    margin = 36
    img_width = page_w - 2 * margin
    img_height = img_width * (height / width)
    c.drawImage(ImageReader(img_buf), margin, page_h - margin - img_height,
                width=img_width, height=img_height)
    c.save()

    return pdf_buf.getvalue()


def generate_text_based_pdf(lines: list[str]) -> bytes:
    """Generate a PDF with selectable text (no OCR needed)."""
    from reportlab.lib.pagesizes import LETTER
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=LETTER)
    _, page_h = LETTER
    y = page_h - 72

    for line in lines:
        c.setFont("Helvetica", 14)
        c.drawString(72, y, line)
        y -= 28

    c.save()
    return buf.getvalue()


# ---------------------------------------------------------------------------
# BDA interaction
# ---------------------------------------------------------------------------

def upload_to_s3(s3, bucket: str, key: str, content: bytes):
    s3.put_object(Bucket=bucket, Key=key, Body=content)


def invoke_bda(runtime_client, input_uri: str, output_uri: str,
               project_arn: str, profile_arn: str, client_token: str) -> str:
    params = {
        "clientToken": client_token,
        "inputConfiguration": {"s3Uri": input_uri},
        "outputConfiguration": {"s3Uri": output_uri},
        "dataAutomationProfileArn": profile_arn,
    }
    if project_arn:
        params["dataAutomationConfiguration"] = {
            "dataAutomationProjectArn": project_arn,
        }

    resp = runtime_client.invoke_data_automation_async(**params)
    return resp["invocationArn"]


def poll_until_complete(runtime_client, invocation_arn: str,
                        interval: int = 10, max_attempts: int = 60):
    for attempt in range(1, max_attempts + 1):
        resp = runtime_client.get_data_automation_status(invocationArn=invocation_arn)
        status = resp.get("status", "")

        if status == "Success":
            print(f"  Job completed (attempt {attempt})")
            return
        elif status in ("ClientError", "ServiceError"):
            raise RuntimeError(
                f"BDA job failed: status={status}, "
                f"error_type={resp.get('errorType')}, "
                f"error_message={resp.get('errorMessage')}"
            )
        else:
            print(f"  Polling... status={status} (attempt {attempt}/{max_attempts})")
            time.sleep(interval)

    raise TimeoutError(f"BDA job did not complete after {max_attempts} attempts")


def find_result_files(s3, bucket: str, prefix: str) -> dict:
    """List BDA output files and classify them."""
    paginator = s3.get_paginator("list_objects_v2")
    all_keys = []
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            all_keys.append(obj["Key"])

    result = {"job_metadata": None, "standard": None, "redacted": None, "all": all_keys}

    for key in all_keys:
        lower = key.lower()
        if key.endswith("job_metadata.json"):
            result["job_metadata"] = key
        elif lower.endswith("result.json") and "/standard_output/" in lower:
            if "/redacted/" in lower:
                result["redacted"] = key
            else:
                result["standard"] = key

    return result


def download_json(s3, bucket: str, key: str) -> dict:
    resp = s3.get_object(Bucket=bucket, Key=key)
    return json.loads(resp["Body"].read())


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip())


def extract_text(payload: dict) -> str:
    """Extract markdown text from a BDA result payload."""
    for key in ("pages", "elements"):
        items = payload.get(key, [])
        if isinstance(items, list) and items:
            parts = []
            for item in items:
                md = (item.get("representation") or {}).get("markdown", "")
                if md.strip():
                    parts.append(md.strip())
            if parts:
                return "\n\n".join(parts)
    return ""


def analyze(standard_payload: dict, redacted_payload: dict):
    std_text = normalize(extract_text(standard_payload))
    red_text = normalize(extract_text(redacted_payload))

    print("\n--- Detection summary ---")
    for label, payload in [("standard", standard_payload), ("redacted", redacted_payload)]:
        sdd = payload.get("sensitive_data_detection", {})
        pages = len(sdd.get("pages", []))
        elements = len(sdd.get("elements", []))
        print(f"  {label} sensitive_data_detection.pages:    {pages}")
        print(f"  {label} sensitive_data_detection.elements: {elements}")

    print("\n--- PII presence in redacted output ---")
    still_present = 0
    for value in EXPECTED_PII_VALUES:
        found = normalize(value) in red_text
        if found:
            still_present += 1
        print(f"  {value:45s} => {'PRESENT' if found else 'REDACTED'}")

    print(f"\n--- Verdict ---")
    print(f"  Redacted output still contains {still_present}/{len(EXPECTED_PII_VALUES)} PII values.")
    print(f"  Standard == Redacted text: {std_text == red_text}")

    print(f"\n--- Standard output preview ---")
    print(f"  {std_text[:300]}")
    print(f"\n--- Redacted output preview ---")
    print(f"  {red_text[:300]}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def load_env_file():
    """Load .env file if present (simple key=value parser, no dependency)."""
    env_path = Path(__file__).parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("\"'")
        if key and value and key not in os.environ:
            os.environ[key] = value


def require_env(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise SystemExit(f"ERROR: {name} is not set. Set it as an env var or in .env")
    return value


def main():
    load_env_file()

    parser = argparse.ArgumentParser(description="BDA PII Redaction Repro")
    parser.add_argument("--fixture", help="Path to a custom PDF fixture (default: generate one)")
    parser.add_argument("--text-pdf", action="store_true",
                        help="Generate a text-based PDF instead of image-based")
    parser.add_argument("--save-artifacts", metavar="DIR",
                        help="Save all artifacts to this local directory")
    parser.add_argument("--cleanup", action="store_true",
                        help="Delete S3 objects after the run")
    args = parser.parse_args()

    bucket = require_env("BDA_BUCKET")
    project_arn = require_env("BDA_PROJECT_ARN")
    profile_arn = require_env("BDA_PROFILE_ARN")
    region = os.environ.get("BDA_REGION", "us-east-1")
    prefix = os.environ.get("BDA_PREFIX", "bda-repro")

    s3 = boto3.client("s3", region_name=region)
    runtime = boto3.client("bedrock-data-automation-runtime", region_name=region)

    # 1. Generate or load fixture
    if args.fixture:
        pdf_bytes = Path(args.fixture).read_bytes()
        print(f"Using custom fixture: {args.fixture} ({len(pdf_bytes)} bytes)")
    elif args.text_pdf:
        pdf_bytes = generate_text_based_pdf(FIXTURE_LINES)
        print(f"Generated text-based PDF fixture ({len(pdf_bytes)} bytes)")
    else:
        pdf_bytes = generate_image_based_pdf(FIXTURE_LINES)
        print(f"Generated image-based PDF fixture ({len(pdf_bytes)} bytes)")

    print("Fixture PII lines:")
    for line in FIXTURE_LINES:
        print(f"  {line}")

    # 2. Upload to S3
    token = str(uuid.uuid4())
    staging_key = f"{prefix}/staging/{token}.pdf"
    output_prefix = f"{prefix}/output/{token}"

    input_uri = f"s3://{bucket}/{staging_key}"
    output_uri = f"s3://{bucket}/{output_prefix}"

    upload_to_s3(s3, bucket, staging_key, pdf_bytes)
    print(f"\nUploaded to {input_uri}")

    # 3. Invoke BDA
    invocation_arn = invoke_bda(
        runtime, input_uri, output_uri,
        project_arn, profile_arn, token,
    )
    print(f"Invocation ARN: {invocation_arn}")

    # 4. Poll
    poll_until_complete(runtime, invocation_arn)

    # 5. Find and download results
    files = find_result_files(s3, bucket, output_prefix)
    print(f"\nOutput files ({len(files['all'])} total):")
    for f in files["all"]:
        print(f"  s3://{bucket}/{f}")

    if not files["standard"] or not files["redacted"]:
        print("\nERROR: Could not find standard and/or redacted result.json files.")
        return 1

    standard_payload = download_json(s3, bucket, files["standard"])
    redacted_payload = download_json(s3, bucket, files["redacted"])
    job_metadata = download_json(s3, bucket, files["job_metadata"]) if files["job_metadata"] else {}

    # 6. Analyze
    analyze(standard_payload, redacted_payload)

    # 7. Save artifacts
    if args.save_artifacts:
        out_dir = Path(args.save_artifacts)
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "fixture.pdf").write_bytes(pdf_bytes)
        (out_dir / "standard_result.json").write_text(json.dumps(standard_payload, indent=2))
        (out_dir / "redacted_result.json").write_text(json.dumps(redacted_payload, indent=2))
        (out_dir / "job_metadata.json").write_text(json.dumps(job_metadata, indent=2))
        print(f"\nArtifacts saved to {out_dir}")

    # 8. Cleanup
    if args.cleanup:
        s3.delete_object(Bucket=bucket, Key=staging_key)
        for key in files["all"]:
            s3.delete_object(Bucket=bucket, Key=key)
        print("\nS3 objects cleaned up.")
    else:
        print(f"\nS3 objects kept for inspection. Staging: {input_uri}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
