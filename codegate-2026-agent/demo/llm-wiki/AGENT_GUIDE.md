+++
schema_version = "1.0.0"
required_citations = ["document_id", "revision", "graph_version", "chunk_id", "section_id"]
document_content_trust = "untrusted"
read_acl_stage = "before_retrieval"
write_authority = "application_approval_only"
relation_statuses = ["VERIFIED"]
max_evidence_chunks = 5
+++

# Agent guide

이 frontmatter는 백엔드가 제한된 schema로 검증해 모든 Claude session의 시작 지침으로 변환한다. 아래 설명문은 사람을 위한 것이며 model prompt에 직접 삽입하지 않는다.
