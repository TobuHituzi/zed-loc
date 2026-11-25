# Zed 日本語化：障害・地雷カタログ（回帰テストの正本）

このファイルは、翻訳辞書と replace 系ツールが踏んだ障害を全部記録する正本。
新しい障害が起きたら必ずここに1件追加し、対応する「検出ルール」と「回帰テスト観点」をセットで書くこと。
replace_ast.py 等の検証は、このカタログの全件をテストケースとして使うこと。

各件の項目：症状 / 原因の仕組み / 該当リテラルと場所 / 検出ルール / 回帰テスト観点 / 状態。

---

## INC-01 キーコンテキスト名の翻訳でキーバインド全滅（ビルド #8 で混入、2026-11-25 発覚）
- 症状：エディタで Backspace / Delete / 矢印キー / Enter / Tab が効かない。文字入力だけできる。
- 仕組み：GPUI のキーコンテキスト名は文字列で登録・照合される。`key_context.add("Editor")` が訳されると、キーマップ側が要求する "Editor" と一致せず、Editor コンテキストの全バインドが無効化される。コンパイルは通る（実行時障害）。
- 該当：editor.rs の "Editor" のほか、同一ファイル内・他ファイルの文脈名多数。
  - editor.rs / items.rs: "Editor"
  - key_dispatch.rs: "Editor", "Pane", "Workspace"
  - git_panel.rs: "GitPanel", "ChangesList", "CommitEditor"
  - go_to_line.rs: "GoToLine"
  - pane.rs: "Pane"
  - diagnostics.rs: "Diagnostics"
  - workspace.rs: "Workspace"（context.add）
  - terminal_view.rs: "Terminal"
  - theme_preview.rs: "ThemePreview"
- 検出ルール：スキャン stage4/stage5（key_context.add / .key_context / KeyContext::parse / KeyBinding::new の Some("..")）。AST 版では「コンテキスト登録呼び出しの引数は訳さない」。
- 回帰テスト：置換後に `key_context.add("Editor")` 等の識別子が原文のまま残ることを grep で確認。実機で Backspace/Delete/矢印の動作確認。
- 状態：辞書から除外済み（2,589→2,579件、後に redo でも除外を継承）。

## INC-02 proc-macro の ident 比較文字列の翻訳で gpui が型エラー56件（ビルド #10、2026-11-25）
- 症状：CI コンパイル失敗。gpui で E0308（Option の二重化・不足）56件。
- 仕組み：derive_refineable.rs:517 の `s.ident == "Option"` が Style フィールドの Option ラップ判定。訳されると判定が全滅し、生成コードの型が反転する。同ファイル `is_ident("Debug")`（27行）も同種。
- 該当：crates/refineable/derive_refineable/src/derive_refineable.rs の "Option", "Debug"。
- 検出ルール：スキャン stage5（同一ファイル内で is_ident / ident == の位置に現れるキー）。AST 版では「== / != の被演算子と is_ident 引数は訳さない」。
- 回帰テスト：置換後に同ファイルの `"Option"` / `"Debug"` 比較が原文のままであること。cargo check -p gpui が通ること。
- 状態：除外済み（辞書 4,002→3,996件の一部）。

## INC-03 sqlez の SQL 句検索文字列の翻訳でマイグレーション検証エラー（ビルド #13、2026-11-25）
- 症状：CI コンパイル失敗。workspace クレートで Sql Error: no such column が4件（workspace_location / remote_project_id / path / kind）。全て RENAME COLUMN / DROP COLUMN のマイグレーション。
- 仕組み：sqlez connection.rs:247,255 が `remaining_sql_str.find("rename column")` / `.find("drop column")` でマイグレーション SQL を解析しカラム改名・削除を台帳管理する。検索句が訳されると句が見つからず台帳がずれ、後続マイグレーションの検証が「カラムがない」で落ちる。
- 該当：crates/sqlez/src/connection.rs の "rename column", "drop column"。（statement.rs のテスト SQL "DELETE FROM blobs" も衛生のため除外）
- 検出ルール：一般化スキャンに `.find(` / `.rfind(` / `.split(` / `.replace(` を追加して検出（当初は contains 系のみで見逃した）。AST 版では「検索系メソッドの引数は訳さない」。
- 回帰テスト：置換後に `find("rename column")` が原文のままであること。cargo check -p workspace が通ること（sqlez マクロ検証を含む）。
- 状態：除外済み（辞書 3,974→3,970件の一部）。ビルド #14 で検証中。

## INC-04 直列化・プロトコル文字列の翻訳（実行時地雷、スキャンで発見）
コンパイルは通るが実行時に壊れるもの。一般化スキャン（比較・属性・const・検索系メソッド・match・bind）で検出。
- serde 属性値：google_ai.rs / prettier.rs / vscode_format.rs の `rename_all = "camelCase"` の "camelCase" → API の JSON 形式が壊れる。
- HTTP ヘッダ名：client.rs の `.get("Location")` の "Location" → リダイレクト処理が壊れる。
- DB 直列化の判別子：workspace/persistence.rs の "Horizontal" / "Vertical" / "Maximized" / "FullScreen" / "Fixed" / "Windowed" → 保存済みレイアウト・ウィンドウ状態の読み書き値。訳すと既存 DB が読めなくなる。
- git 出力のパース：git_ui/remote_output.rs の "Updating" / "Merge" / "Successfully rebased"、git_panel.rs の "Update"、git/repository.rs の "Operation cancelled by user" → git コマンド出力の判定が壊れる。
- コミットトレーラー接頭辞：git_panel.rs の "Co-authored-by: " → コミットに書かれる文字列自体が変わる。
- 接頭辞剥がし：google_ai.rs の "models/"（starts_with でモデル ID を処理）。
- エラー表示文字列：client.rs の "upgrade required"（thiserror の表示文。比較に使われる可能性を考慮し除外）。
- 状態：全て除外済み（辞書 3,996→3,974件の一部、commit 8026145）。

## INC-05 テスト内リテラル（実害なし、衛生のため除外したもの）
- command_palette.rs の "editor: backspace"、hover_popover.rs の "Function to read a file"、task_inventory.rs の "debug scenarios"、outline_panel.rs の "  <==== selected"。
- いずれもテストコード内のリテラル。実害はないが、将来テストを回すときのために除外。
- 状態：除外済み（INC-04 と同じコミット）。

## 安全と判定したパターン（再判定しない）
同じファイル内で「作る側」と「比べる側」の両方が同じリテラルで、置換で両方とも一緒に変わるものは壊れない。
- language_model_selector.rs の "Recommended"（Separator の生成と比較が同一ファイル）。
- workspace/pane.rs の CONFLICT_MESSAGE 等の const（const 自体が表示文そのもの。参照は const 名経由）。
- gpui/color.rs のテスト const（EXPECTED_FORMATS 等、生成側も同一ファイル）。
- gpui/arena.rs の should_panic 期待文（パニック文も同一ファイル）。
- cli/main.rs の after_help（clap の表示属性。属性でも表示系は訳してよい）。
- task/vscode_format.rs のテスト内 replace("Food") 等（入力と期待値が同一リテラル）。

## 運用ルール（プロセスの教訓）
- ビルドが1回でも失敗したら、再ビルドの前に必ず失敗ログの実物で原因を確認し、原因とセットで報告する（2026-11-25 ご主人様指示）。辞書・コード起因の決定的エラーは再ビルドしない。
- 辞書を作り直したら、ビルド前に一般化スキャン（scan-identifiers.py）を必ず通し、ヒットは行を見て判定する。
- 置換後のソースで対象クレートの cargo check を通すのが理想。sheeepubuntu にはシステムの cc がないため、own-voice-tts の conda toolchain（/mnt/ssd4tb/muse-projects/own-voice-tts/build-tools/toolchain/bin/x86_64-conda-linux-gnu-gcc）を CC/リンカに使う。
- 翻訳スクリプト（translate-json-plamo.py）側の対策（プレースホルダ順序比較、危険な原文の事前 skip、外国文字・制御文字の不合格判定）は同スクリプト冒頭と MEMORY.md を参照。

## replace_ast.py の既知の限界（2026-11-25、合成回帰テスト 43 PASS / 4 gaps / 0 FAIL）
構文木版でも構文だけでは判定できないもの。辞書側の除外が引き続き防波堤になる。
- items.rs の "Editor"：serialized_item_kind() 等の戻り値がファイル跨ぎの識別子。構文的な手がかりなし。
- git/repository.rs の REMOTE_CANCELLED_BY_USER：文型 const だが git 出力と照合される。const ヒューリスティクスでは表示文と判定される。
- INC-05 のテスト const 2件（"debug scenarios"、"  <==== selected"）：空白を含むため表示文扱い。実害なし。
- git_panel.rs の Some("Update") は精査の結果ダイアログのボタンラベル（action_text）で、git 出力パースではなかった。AST 版では意図的に翻訳する。辞書側の除外は予防的なものとして残置。
- 詳細と対応表は ~/projects/zed-refresh/ast-tools/REPORT.md、合成テストは ast-tools/synth_test.py。
