"""Exact evidence reconciliation. Candidates do not imply adopted business values."""

from copy import deepcopy
import hashlib


def issue(code, message, sources):
    unique = []
    for source in sources:
        if source not in unique:
            unique.append(deepcopy(source))
    return {
        "id": "takeoff-" + hashlib.sha256(f"{code}:{unique}".encode()).hexdigest()[:24],
        "code": code,
        "message": message,
        "severity": "blocks_pricing",
        "sources": unique,
        "affectedItemIds": [],
    }


def reconcile_model_claims(subject_id, claims):
    """Caller establishes association; no spatial/fuzzy association is inferred here."""
    copied = deepcopy(claims)
    if not isinstance(subject_id, str) or not subject_id:
        raise ValueError("subject_id required")
    for claim in copied:
        if not isinstance(claim.get("value"), str) or not claim.get("kind"):
            raise ValueError("model evidence requires string value and kind")
        if not claim.get("source", {}).get("fileVersionId"):
            raise ValueError("model evidence requires source file version")
    # Only surrounding whitespace is ignored; suffixes, case, hyphens and digits remain.
    values = {c["value"].strip() for c in copied if c["value"].strip()}
    issues = []
    if len(values) != 1:
        code = "MODEL_CONFLICT" if values else "MODEL_UNRESOLVED"
        issues.append(
            issue(
                code,
                "型号证据冲突" if values else "缺少可采用的型号证据",
                [c["source"] for c in copied],
            )
        )
    return {
        "subjectId": subject_id,
        "candidateModel": next(iter(values)) if len(values) == 1 else None,
        "adoptedModel": None,
        "claims": copied,
        "issues": issues,
    }
