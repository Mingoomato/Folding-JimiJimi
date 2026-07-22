# Document Editing API v2

The monorepo is the only current source of truth. `UPSTREAM_SOURCES.md` records
historical provenance; `component-manifest.json` pins the integration baseline
and supported runtimes.

## Trust boundary

- Claude may read capabilities and native structure, search LLMWIKI, and propose
  typed operations. It cannot call a writer or supply a filesystem path.
- The local Python sidecar is the only process allowed to apply native document
  artifacts. Cloud APIs never receive a local path or original file bytes.
- The desktop save dialog chooses creation targets. The sidecar accepts only a
  relative path confined to the registered workspace.
- Every mutation and creation is prepared as an immutable proposed artifact.
  Approval applies that exact artifact; the writer is not run again.

## Supported operations

| Format | Read | Create | Mutate |
| --- | --- | --- | --- |
| HWP | yes | HWPX derivative only | no |
| HWPX | yes | Markdown | text and table cells |
| DOCX | yes | Markdown, optional approved template | text and table cells |
| PPTX | yes | Markdown, optional approved template | text and table cells |
| XLSX | yes | typed workbook JSON | typed cell batches |
| PDF | yes | Markdown through DOCX and LibreOffice | annotation, form field, exact-text redaction |

Macro-enabled, encrypted, signed, ActiveX/OLE, external-link, and unknown package
structures are rejected according to the capability response. Macros are never
executed. General PDF body editing creates a derivative instead of replacing the
source.

## API contract

All v2 POST endpoints require `Idempotency-Key`. Plan preparation, approval, sync
retry, and undo return `202 Accepted`.

- `POST /api/v2/chat/messages`
- `GET /api/v2/document-capabilities`
- `POST /api/v2/documents/{id}/mutation-plans`
- `POST /api/v2/document-creation-plans`
- `GET /api/v2/change-plans/{id}`
- `GET /api/v2/change-plans/{id}/previews/{artifact_id}`
- `POST /api/v2/change-plans/{id}/approve`
- `POST /api/v2/change-plans/{id}/reject`
- `GET /api/v2/executions/{id}`
- `POST /api/v2/executions/{id}/retry-sync`
- `POST /api/v2/executions/{id}/undo`

`/api/v1` remains unchanged. `doc2md /capabilities` remains read-only with
`write_back=false`.

## Approved templates

Set `CODEGATE_DOCUMENT_TEMPLATE_ROOT` to a local app-managed directory. The
directory may contain `manifest.json` with this shape:

```json
{
  "schema_version": "1.0.0",
  "templates": [
    {
      "template_id": "company-docx",
      "format": "docx",
      "relative_path": "company.docx",
      "sha256": "<64 lowercase hexadecimal characters>"
    }
  ]
}
```

Only DOCX and PPTX templates are accepted. PDF creation may use an approved DOCX
template before conversion. A path escape, link/reparse point, format mismatch,
or hash mismatch fails closed.

## Crash and recovery invariants

The filesystem adapter provides shared/exclusive locks, confined snapshots,
immutable backups, atomic replacement, create-new, and recovery moves. POSIX and
Windows use separate implementations; importing the package on Windows does not
import `fcntl`.

The write journal records an execution as `prepared` before touching a source.
On startup the sidecar reconciles prepared executions:

- If a mutation still has the before hash, it applies the approved artifact.
- If it already has the after hash, it requires the immutable backup and resumes.
- If a creation target is absent, it creates it; if it has the approved hash, it
  resumes; any other existing content is a conflict.
- Creation undo moves the file to the app-managed recovery directory rather than
  deleting it.
- A crash after the filesystem change but before the HTTP response preserves the
  approval idempotency record. Replaying the same key returns the original
  execution without rechecking a newer capability snapshot or rerunning a writer.

Outbox and LLMWIKI publication happen after the file operation. Sync failure
leaves the file in place, marks the execution `sync_failed`, and retains the
previous `current.json`. Retry is deduplicated by event ID. Backup and recovery
execution directories are retained for 30 days by default; cleanup validates the
entire tree before deleting any entry and never follows links or reparse points.

## Configuration

- `CODEGATE_LIBREOFFICE_BIN`: optional allow-listed LibreOffice executable.
- `CODEGATE_DOCUMENT_TEMPLATE_ROOT`: approved local template registry.
- `CODEGATE_DOCUMENT_RETENTION_DAYS`: backup/recovery retention, default 30.

When managed LibreOffice is unavailable, Office/PDF creation or preview
capabilities that depend on it are reported as disabled rather than failing late.
