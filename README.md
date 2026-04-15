# BDA PII Redaction Not Working

## Summary

AWS Bedrock Data Automation (BDA) is configured with `DETECTION_AND_REDACTION` mode and all 31 PII entity types enabled, but:

- `sensitive_data_detection.pages` and `sensitive_data_detection.elements` are **always empty arrays** in both standard and redacted outputs
- The redacted output text is **byte-identical** to the standard output text
- **All PII values remain fully visible** in the redacted output (name, email, phone, SSN, address)

This was reproduced with **four different PDF input formats** across two separate codebases to rule out input formatting issues.

## Environment

| Item | Value |
|------|-------|
| Region | `us-east-1` |
| Project ARN | `arn:aws:bedrock:us-east-1:<ACCOUNT_ID>:data-automation-project/5e14304302cd` |
| Project name | `seqster-bda-pii-redacted` |
| Profile ARN | `arn:aws:bedrock:us-east-1:<ACCOUNT_ID>:data-automation-profile/us.data-automation-v1` |
| Project type | `ASYNC` |
| Project stage | `LIVE` |
| Date tested | 2026-04-15 |

## Project sensitive data configuration

From `GetDataAutomationProject` response (full response in [`evidence/project_config.json`](evidence/project_config.json)):

```json
{
    "detectionMode": "DETECTION_AND_REDACTION",
    "detectionScope": ["STANDARD"],
    "piiEntitiesConfiguration": {
        "piiEntityTypes": [
            "ADDRESS", "AGE", "NAME", "EMAIL", "PHONE",
            "USERNAME", "PASSWORD", "DRIVER_ID", "LICENSE_PLATE",
            "VEHICLE_IDENTIFICATION_NUMBER",
            "CREDIT_DEBIT_CARD_CVV", "CREDIT_DEBIT_CARD_EXPIRY",
            "CREDIT_DEBIT_CARD_NUMBER", "PIN",
            "INTERNATIONAL_BANK_ACCOUNT_NUMBER", "SWIFT_CODE",
            "IP_ADDRESS", "MAC_ADDRESS", "URL",
            "AWS_ACCESS_KEY", "AWS_SECRET_KEY",
            "US_BANK_ACCOUNT_NUMBER", "US_BANK_ROUTING_NUMBER",
            "US_INDIVIDUAL_TAX_IDENTIFICATION_NUMBER",
            "US_PASSPORT_NUMBER", "US_SOCIAL_SECURITY_NUMBER",
            "CA_HEALTH_NUMBER", "CA_SOCIAL_INSURANCE_NUMBER",
            "UK_NATIONAL_HEALTH_SERVICE_NUMBER",
            "UK_NATIONAL_INSURANCE_NUMBER",
            "UK_UNIQUE_TAXPAYER_REFERENCE_NUMBER"
        ],
        "redactionMaskMode": "ENTITY_TYPE"
    }
}
```

## Input fixture

A single-page PDF containing these lines:

```
NAME: John Q Public
EMAIL: john.public@example.com
PHONE: 415-555-1212
SSN: 123-45-6789
ADDRESS: 123 Main St, Springfield, IL 62704
```

The pre-generated fixture is available at [`fixtures/pii_sample.pdf`](fixtures/pii_sample.pdf).

## Reproduction

### Prerequisites

```bash
pip install -r requirements.txt
```

AWS credentials must be configured with permissions for S3 and Bedrock Data Automation.

### Configuration

```bash
cp .env.example .env
# Edit .env with your values:
#   BDA_BUCKET        - S3 bucket for staging/output
#   BDA_PROJECT_ARN   - BDA project ARN (with redaction config)
#   BDA_PROFILE_ARN   - BDA profile ARN
#   BDA_REGION        - AWS region (default: us-east-1)
#   BDA_PREFIX        - S3 key prefix (default: bda-repro)
```

### Run

```bash
python repro.py --save-artifacts ./artifacts --cleanup
```

Options:
- `--text-pdf` — generate a text-based PDF (selectable text) instead of image-based
- `--fixture path/to/file.pdf` — use a custom PDF file
- `--save-artifacts DIR` — save all outputs locally
- `--cleanup` — delete S3 objects after the run

## Expected vs actual behavior

### Expected

The redacted output should:
1. Have `sensitive_data_detection.pages` / `sensitive_data_detection.elements` populated with detected PII entities
2. Replace PII values with redaction masks (e.g., `{NAME}`, `{EMAIL}`, `{US_SOCIAL_SECURITY_NUMBER}`) per the `ENTITY_TYPE` mask mode
3. Produce redacted text like: `NAME: {NAME} EMAIL: {EMAIL} PHONE: {PHONE} SSN: {US_SOCIAL_SECURITY_NUMBER} ADDRESS: {ADDRESS}`

### Actual

The redacted output:
1. Has `sensitive_data_detection.pages: []` and `sensitive_data_detection.elements: []` — **empty, no PII detected**
2. Contains **no redaction masks** — all original PII values are present
3. Is **byte-identical** to the standard output text

## Evidence: four reproduction runs

We tested with four different PDF generation methods to rule out input formatting as the cause.

### Run 1: raw PDF text operators

Minimal PDF built with raw `BT`/`Tj`/`T*` operators and Helvetica Type1 font.

- **Invocation**: `a9b8451a-b5b8-4ff2-8227-1bac8a9b329e`
- **Result**: 5/5 PII values still present, `sensitive_data_detection` empty
- **Note**: BDA extracted text with some field label/value jumbling (labels on separate lines from values)
- **Artifacts**: [`evidence/run-1-raw-pdf/`](evidence/run-1-raw-pdf/)

### Run 2: dompdf text-based PDF

PDF generated via dompdf (HTML-to-PDF renderer) with proper font embedding.

- **Invocation**: `6e40d99b-8e63-4a55-91ac-19a2cd14647e`
- **Result**: 4/5 PII values present (email lost in extraction, not redacted), `sensitive_data_detection` empty
- **Note**: Standard and redacted outputs are identical — the missing email value is an extraction artifact, not redaction
- **Artifacts**: [`evidence/run-2-dompdf-text/`](evidence/run-2-dompdf-text/)

### Run 3: image-based PDF (OCR path)

Text rendered to a PNG image via GD, then embedded in a PDF via dompdf. This forces BDA through its OCR pipeline.

- **Invocation**: `51a48800-6256-42ff-9485-e0ce8fc7045a`
- **Result**: 5/5 PII values still present, `sensitive_data_detection` empty
- **Note**: BDA extracted all text perfectly via OCR — the PII is clearly visible in the output, just not detected or redacted
- **Artifacts**: [`evidence/run-3-image-based/`](evidence/run-3-image-based/)

### Run 4: standalone repro script (reportlab + Pillow)

Image-based PDF generated by the standalone `repro.py` script using reportlab and Pillow, run outside of the application stack against the same BDA project.

- **Invocation**: `1083587e-0c2c-4c40-91cf-2a8682bcd57a`
- **Result**: 5/5 PII values still present, `sensitive_data_detection` empty
- **Note**: High-resolution Helvetica 48pt at 300 DPI. BDA extracts all text perfectly — standard and redacted outputs are byte-identical
- **Artifacts**: [`evidence/run-4-repro-script/`](evidence/run-4-repro-script/)

### Key observation from run 3 redacted output

BDA successfully extracts all PII via OCR but does not flag or redact any of it:

```json
"sensitive_data_detection": {
    "pages": [],
    "elements": []
}
```

Redacted markdown output (identical to standard):

```
NAME:	John Q Public
EMAIL:	john.public@example.com
PHONE:	415-555-1212
SSN:	123-45-6789
ADDRESS:	123 Main St, Springfield, IL 62704
```

## What we've ruled out

| Possible cause | Status |
|----------------|--------|
| Reading the wrong output path (not `/redacted/`) | Ruled out — we read from `.../standard_output/0/redacted/result.json` as confirmed in `job_metadata.json` |
| Wrong project ARN | Ruled out — `GetDataAutomationProject` confirms `seqster-bda-pii-redacted` with `DETECTION_AND_REDACTION` |
| Input formatting / line breaks affecting detection | Ruled out — tested 4 PDF formats including image-based OCR across 2 codebases; all produce clean text extraction |
| Profile ARN overriding project config | Investigated — using default `us.data-automation-v1` profile |
| Project not in LIVE stage | Ruled out — project stage is `LIVE`, status is `COMPLETED` |
| Multiple projects tested | Tested 3 projects with identical `sensitiveDataConfiguration` — same result on all |
