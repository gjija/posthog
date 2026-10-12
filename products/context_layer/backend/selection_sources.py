import json
import time
from urllib.parse import quote

from django.conf import settings

import httpx

from products.context_layer.backend.selection_search import tokens
from products.context_layer.backend.selection_types import SOURCE_LIMITS, Candidate, SourceKind, digest

MAX_SOURCE_TEXT = 2_800
SOURCE_PATHS: dict[SourceKind, str] = {
    "skill": "llm_skills/search/",
    "metric": "data_catalog/metrics/",
    "certification": "data_catalog/certifications/",
    "relationship": "data_catalog/relationship_proposals/",
    "business_knowledge": "business_knowledge/documents/search/",
}


class SelectionSources:
    def __init__(self, team_id: int, authorization: str) -> None:
        self.team_id = team_id
        self._authorization = authorization

    def search(self, kind: SourceKind, prompt: str, history: str, deadline: float) -> list[Candidate]:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return []
        terms = " ".join(dict.fromkeys(tokens(prompt + "\n" + history)))[:200]
        if not terms:
            return []
        params: dict[str, str | int] = {"limit": SOURCE_LIMITS[kind]}
        params["query" if kind in ("skill", "business_knowledge") else "search"] = terms
        with httpx.Client(
            base_url=settings.SITE_URL,
            headers={"Authorization": self._authorization},
            follow_redirects=False,
            timeout=remaining,
        ) as client:
            response = client.get(f"/api/projects/{self.team_id}/{SOURCE_PATHS[kind]}", params=params)
            response.raise_for_status()
            payload = response.json()
        rows = payload["results"] if isinstance(payload, dict) else payload
        return [self.record(kind, row) for row in rows[: SOURCE_LIMITS[kind]]]

    def record(self, kind: SourceKind, row: dict[str, object]) -> Candidate:
        root = f"/api/projects/{self.team_id}/"
        document_id = ""
        if kind == "skill":
            title = str(row["name"])
            revision = str(row["version"])
            identifier = title
            payload = {"description": row["description"]}
            status = "skill"
            reference = f"skill-get: skill_name={title}, version={revision}"
        elif kind == "business_knowledge":
            identifier = str(row["chunk_id"])
            title = str(row["document_title"])
            payload = {"content": row["content"]}
            revision = digest(row["content"])
            document_id = str(row["document_id"])
            status = "generated_source" if row["is_generated"] else "source_passage"
            reference = f"{root}business_knowledge/documents/{document_id}/window/?around_ordinal={row['ordinal']}"
        else:
            identifier = str(row["id"])
            revision = str(row.get("updated_at") or digest(row))
            status = str(row["status"])
            if kind == "metric":
                title = str(row["name"])
                payload = {key: row[key] for key in ("description", "definition", "unit")}
                status = "drifted" if row["is_drifted"] else status
                reference = f"{root}data_catalog/metrics/{quote(title, safe='')}/"
            elif kind == "certification":
                title = str(row["target_name"])
                payload = {key: row[key] for key in ("notes", "proposed_status")}
                reference = f"{root}data_catalog/certifications/{identifier}/"
            else:
                title = f"{row['source_table_name']} -> {row['joining_table_name']}"
                payload = {key: row[key] for key in ("source_table_key", "joining_table_key", "reasoning")}
                reference = f"{root}data_catalog/relationship_proposals/{identifier}/"
        text = json.dumps(payload, ensure_ascii=False, default=str)
        if len(text) > MAX_SOURCE_TEXT:
            text = text[:MAX_SOURCE_TEXT] + " [truncated; read the source]"
        return Candidate(
            id=identifier,
            kind=kind,
            title=title,
            text=text,
            revision=revision,
            status=status,
            reference=reference,
            document_id=document_id,
        )
