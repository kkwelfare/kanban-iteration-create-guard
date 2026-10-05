# `kanban_create_guarded` specialist起票テンプレート

完全な登録済みtool payloadは `templates/specialist-create.json` をコピーして準備する。placeholderを実際に確認・判断した値で全て置き換え、scope admissionと `decision_input` をそれぞれのcanonical contractに照らす。`scope.status`、`freshness.observed_at`、`deduplication.checked_at`（statusがcheckedの場合）、`provenance.captured_at` を省略しない。時刻・根拠・source referenceを捏造しない。未解決の判断があるscopeは `needs_master` とし、起票せずmaster判断へ戻す。

Jev必須profileでは `tool_describe` で登録schemaを確認し、作成は必ず `tool_call` の `kanban_create_guarded` で行う。通常create/CLIや直接plugin関数へ切り替えない。wrapperが作成中に `provenance.task_id` を実タスクIDへ束縛し、blocked状態で保存、canonical readback検証後にunblockする。templateのplaceholderを残した入力は実行不可。Jev bridgeは `plugins.entries.kanban-iteration-create-guard.settings.jev_bridge_dir` または `HERMES_JEV_BRIDGE_DIR` で設定する。設定先のJev packageは `bridge.py` と必要なsibling modulesを含み、canonical `_validate_decision_input` / `_extract_decision_input` APIを提供すること。Hermesのprofileごとに設定し、明示設定が無い・API不一致ならfail closedする。scopeとdecision inputは別々に検証する。

成功後、返却された `jev_input.persisted`、`jev_input.task_id_bound`、`jev_input.canonical_readback_validated` が全て厳密に `true` であることを確認し、正本のtask record/bodyをreadbackして担当、skill、scope、保存状態を確認する。readyでactive runが無いカードだけdispatchする。すでにrunningなら二重dispatchしない。flag欠落、入力error、readback不一致、blocked状態ならdispatch停止。同じカードだけをsupported routeと既存retry/breaker/approval範囲内で確認する。新規カード、通常create、breaker回避へ切り替えない。

Jevの役割は助言的な事前分類であり、許可・停止・最終判断のauthorityではない。scope admission、approval、loop上のsafety境界と権限を置換しない。
