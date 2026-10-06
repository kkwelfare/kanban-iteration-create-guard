# `kanban_create_guarded` specialist起票テンプレート

完全な登録済みtool payloadは `templates/specialist-create.json` をコピーして準備する。placeholderを実際に確認・判断した値で全て置き換え、scope admissionと `decision_input` をそれぞれのcanonical contractに照らす。`scope.status`、`freshness.observed_at`、`deduplication.checked_at`（statusがcheckedの場合）、`provenance.captured_at` を省略しない。時刻・根拠・source referenceを捏造しない。未解決の判断があるscopeは `needs_master` とし、起票せずmaster判断へ戻す。

Jev必須profileでは `tool_describe` で登録schemaを確認し、作成は必ず `tool_call` の `kanban_create_guarded` で行う。通常create/CLIや直接plugin関数へ切り替えない。wrapperが作成中に `provenance.task_id` を実タスクIDへ束縛し、blocked状態で保存、canonical readback検証後にunblockする。templateのplaceholderを残した入力は実行不可。Jev bridgeは `plugins.entries.kanban-iteration-create-guard.settings.jev_bridge_dir` または `HERMES_JEV_BRIDGE_DIR` で設定する。設定先のJev packageは `bridge.py` と必要なsibling modulesを含み、canonical `_validate_decision_input` / `_extract_decision_input` APIを提供すること。Hermesのprofileごとに設定し、明示設定が無い・API不一致ならfail closedする。scopeとdecision inputは別々に検証する。

成功後、返却された `jev_input.persisted`、`jev_input.task_id_bound`、`jev_input.canonical_readback_validated` が全て厳密に `true` であることを確認し、正本のtask record/bodyをreadbackして担当、skill、scope、保存状態を確認する。readyでactive runが無いカードだけdispatchする。すでにrunningなら二重dispatchしない。flag欠落、入力error、readback不一致、blocked状態ならdispatch停止。同じカードだけをsupported routeと既存retry/breaker/approval範囲内で確認する。新規カード、通常create、breaker回避へ切り替えない。

Jevの役割は助言的な事前分類であり、許可・停止・最終判断のauthorityではない。scope admission、approval、loop上のsafety境界と権限を置換しない。

## docs成果物の完了要件の受渡し

担当がdocs、または `work_class=docs` の制作・変更では `artifact_outputs` に成果物ごとの `{path, format}` を宣言する。テンプレートの空配列を実際の最終出力の絶対パスと形式で埋める。素材の拡張子を納品形式として推定しない。read_onlyで成果物宣言がない場合と他profileは従来経路を保つ。read_onlyでも明示した成果物にはpacketを付ける。

wrapperはdocs完了consumerの対象拡張子とquality helperの対象拡張子を副作用のないAST読取りで参照し、宣言した出力、consumer/helperのhash、現在のmetadata要求とcanonical checker/helperの利用手順を「docs artifact completion readiness packet」としてbodyへ渡す。consumerは `get_profile_dir("docs")` でprofile-localに解決し、importしない。入力不足・不一致・consumer読取失敗では起票前に具体的なerrorを返す。全ての品質ルールを自動解釈するAPIではないため、consumerの構造・要求変更時にはadapterを確認する。

packetは要件伝達であり、QA実施やreceipt生成、品質合格の証拠ではない。production時に実際の `metadata.verification.final_output_filter.receipts`、適用する `artifact_quality` contract/receipts/finalization_manifest/receiverの証拠を用意する。通常DOCXは `ordinary-docx-baseline-1` の既存経路を使え、strict quality receiptsを一律に追加しない。HTML/HTMのHTML-to-PDFルールはPDF向けであり、HTML単体の視覚品質合格を保証しない。未対応をpassとせず、納品形式の変更も無断でしない。追加QA、追加worker、有料処理、承認外のbaseline拡張をpacketから自動追加しない。
