# kawanolab_tools

## 周波数・波長・波数変換器

### インストール方法

1. [Releases](https://github.com/RuLzkin/kawanolab_tools/releases)から最新版をダウンロード
2. `WavelengthConverter.exe`を実行（インストール不要）

## SHRC-203 ステージコントローラ GUI

`python app_shrc203.py` で起動（`module_shrc203.py` と `pyvisa` が必要）。

- 十字キー: X/Y 相対移動、PageUp/PageDown: Z 相対移動、Shift 併用でステップ×10
- XY マップをクリック: その位置へ絶対値移動（Z は現在値を維持）／数値入力でも XYZ 絶対値移動
- Esc: 緊急停止 (`L:E`)、Space: 減速停止 (`L:W`)
- 「シミュレーション」にチェックを入れると実機なしで動作確認可能
