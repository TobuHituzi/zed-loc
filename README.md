# zed-loc フォーク版 ― 日本語化＋ワークツリー再スキャン付き Zed for Windows

[TC999/zed-loc](https://github.com/TC999/zed-loc) の個人用フォークです。
[zed-industries/zed](https://github.com/zed-industries/zed)（main ブランチ）のソースに、ワークツリー再スキャン機能と日本語化を加えて、Windows 用の `zed.exe` をビルドします。

## 目的

Windows で rclone マウントしたフォルダを Zed のローカルフォルダとして開いて使っていると、サーバー側でファイルを変更してもファイルウォッチャーのイベントが発生せず、プロジェクトパネルが古い表示のままになります（上流の issue [zed-industries/zed#38109](https://github.com/zed-industries/zed/issues/38109)）。

公式ビルドにはパネルを手動で読み直す手段がないため、「再スキャン機能」と「日本語 UI」を持った自分専用のビルドを作るのがこのフォークの目的です。

## 上流からの変更点

### 1. ワークツリー再スキャン（`patches/rescan-worktrees.patch`）

- `RescanWorktrees` アクションを追加（上流 PR #48871 がベース）
- プロジェクトパネルの上部に再読み込みボタンを追加
- そのヘッダー行の追加でパネルのスクロールが効かなくなったため、レイアウト修正（`min_h_0`）も含む

### 2. 既存ウィンドウ再利用の強制パッチ

エクスプローラーなどからファイルを開いたときに、既定の挙動では新しいウィンドウが開いてしまうことがある問題への対策として、既存ウィンドウを再利用するよう強制するパッチを当てています。

### 3. 日本語化

- 辞書は `ja/ja-JP.json`。PLaMo 翻訳スクリプトで UI 文字列を1件ずつ翻訳して構築
- **識別子保護ルール**：キーコンテキスト、コード内で比較される識別子、SQL の断片など、「翻訳すると動作が壊れる文字列」は訳しません。過去に翻訳が原因で壊れた事例と対策の一覧は [INCIDENTS.md](INCIDENTS.md) を参照
- 辞書の適用は `replace.py`（正規表現ベースの置換）と `replace_ast.py`（AST ベースの置換）で行います

### 4. ビルド構成

- ワークフローは Windows ビルドのみに整理
- 公式のようなインストーラーは作らず、単体の `zed.exe` を GitHub Release のアセット（`zed-windows.zip`）として公開

## ビルド方法

GitHub Actions の「日本語版」ワークフロー（`.github/workflows/build-jp.yml`）を手動実行します。

- 入力 `branch`：ビルドする zed 本体のブランチ（既定は `main`）
- 入力 `release`：`true` にすると成果物が Release に発行される
- 成果物：`zed.exe`（Actions のアーティファクト `zed-release`、または Release の `zed-windows.zip`）

## 既知の制限

- `zed.exe` は未署名のため、初回起動時に SmartScreen の警告が出ます
- 自動アップデートはありません。更新は新しいビルドの `zed.exe` で手動で上書きしてください
- 設定ファイルは公式と同じく `%APPDATA%\Zed\settings.json` です

## ライセンス

MIT ライセンス（[LICENSE](LICENSE)）。ベースツールの zed-loc と、Zed 本体のすべての貢献者に感謝します。
