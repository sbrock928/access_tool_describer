from pathlib import Path

import pytest

from portfolio_analyzer.models import AccessExtractedObject, AccessExtractionResult
from portfolio_analyzer.redaction import redact_sensitive_text


@pytest.mark.parametrize(
    "value",
    [
        "PWD=space bearing secret;SERVER=SQL01",
        '"password": "json-secret"',
        "Authorization: Bearer very-secret-token",
        "Account Key={semi;colon;secret};Database=Trust",
        "https://user:password-value@example.invalid/path",
    ],
)
def test_redaction_removes_secret_values_from_persistable_text(value: str) -> None:
    output = redact_sensitive_text(value)

    assert "very-secret-token" not in output
    assert "space bearing secret" not in output
    assert "json-secret" not in output
    assert "semi;colon;secret" not in output
    assert "password-value" not in output
    assert "<redacted>" in output


@pytest.mark.parametrize(
    ("key", "secret"),
    [
        ("User", "user-alias-canary"),
        ("User Name", "user-name-canary"),
        ("Credential", "credential-canary"),
        ("Account", "account-canary"),
        ("Client ID", "client-id-canary"),
    ],
)
def test_redaction_covers_connection_credential_aliases(key: str, secret: str) -> None:
    output = redact_sensitive_text(f"{key}={secret};SERVER=SQL01")

    assert secret not in output
    assert "<redacted>" in output


def test_temporary_extraction_result_is_sanitized_before_worker_serialization() -> None:
    extracted = AccessExtractionResult(
        tool_inventory_id="app-1",
        artifact_id="artifact-1",
        staged_path=Path("staged.accdb"),
        extractor_version="test",
        objects=[
            AccessExtractedObject(
                object_type="query",
                name="PWD=object-name-secret",
                definition=(
                    "SELECT 1 -- Password=definition-secret;"
                    "Client ID=definition-client-id-secret"
                ),
                properties={
                    "Password": "property-secret",
                    "Credential": "property-credential-secret",
                },
            )
        ],
        extraction_errors=["UID=error-secret"],
    )

    serialized = extracted.model_dump_json()

    assert "object-name-secret" not in serialized
    assert "definition-secret" not in serialized
    assert "property-secret" not in serialized
    assert "definition-client-id-secret" not in serialized
    assert "property-credential-secret" not in serialized
    assert "error-secret" not in serialized
