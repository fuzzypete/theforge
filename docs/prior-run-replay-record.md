Status: record (2026-08-20, issue #2608)

Prior-run selection replay - 30 stories, 28 with >=1 candidate

Qualifying signals:
  dir_overlap(analysis/db): 3
  dir_overlap(docs/decisions): 1
  dir_overlap(hdp_mcp): 1
  dir_overlap(src/pipeline/ingest): 2
  dir_overlap(src/theforge): 6
  dir_overlap(src/theforge/cli): 2
  dir_overlap(src/theforge/coordinator): 11
  dir_overlap(src/theforge/sprint): 3
  dir_overlap(src/theforge/task): 1
  dir_overlap(tests): 15
  dir_overlap(tests/unit): 2
  domain_match(api): 5
  domain_match(backend): 12
  domain_match(cli): 26
  domain_match(config): 12
  domain_match(data-processing): 12
  domain_match(database): 3
  domain_match(parsing): 2
  file_overlap(.env.example): 3
  file_overlap(api/constants.py): 3
  file_overlap(docker-compose.yml): 1
  file_overlap(docs/guides/inputs-reference.md): 3
  file_overlap(hdp_mcp/db.py): 3
  file_overlap(hdp_mcp/schema_gen.py): 5
  file_overlap(src/theforge/cli/shared.py): 2
  file_overlap(src/theforge/config/defaults.py): 1
  file_overlap(src/theforge/config/load.py): 1
  file_overlap(src/theforge/coordinator/audit_storage.py): 1
  file_overlap(src/theforge/coordinator/diagnose_flow.py): 1
  file_overlap(src/theforge/coordinator/engine.py): 1
  file_overlap(src/theforge/coordinator/knowledge_summary_flow.py): 2
  file_overlap(src/theforge/coordinator/plan_flow.py): 2
  file_overlap(src/theforge/coordinator/preflight.py): 1
  file_overlap(src/theforge/coordinator/preflight_cache.py): 1
  file_overlap(src/theforge/sprint/audit.py): 2
  file_overlap(src/theforge/sprint/audit_publish.py): 4
  file_overlap(src/theforge/sprint/runner.py): 8
  file_overlap(src/theforge/sprint/status_reader.py): 1
  file_overlap(tests/test_coord_knowledge_summary.py): 1
  story_match: 62

Useful truncation pressure:
  candidate cap: 3 phase(s)
  rendered-claim cap: 0 phase(s)

Corpus theforge (22 stories)
  root: /Users/pwickersham/src/theforge/.forge/worktrees/issue-2608
  fence probe 1a6b6e18d232: 1a6b6e18d232: offered [file_overlap(src/theforge/coordinator/diagnose_flow.py), file_overlap(src/theforge/diagnose_types.py), file_overlap(src/theforge/task/diagnose_prompts.py), reduced_rank]; 73d7de156730: offered [dir_overlap(src/theforge), dir_overlap(src/theforge/coordinator), dir_overlap(tests), reduced_rank]; co-surfaced=True
  fence probe 73d7de156730: 1a6b6e18d232: offered [dir_overlap(src/theforge), dir_overlap(src/theforge/coordinator), dir_overlap(tests), reduced_rank]; 73d7de156730: offered [file_overlap(src/theforge/coordinator/preflight.py), file_overlap(src/theforge/coordinator/preflight_cache.py), file_overlap(src/theforge/knowledge_summary.py), reduced_rank]; co-surfaced=True

Corpus hdp (8 stories)
  root: /Users/pwickersham/src/hdp
