# radirec — radiko 録音ツール（個人利用）

## 準備
- Python 3.8 以上
- ffmpeg（Mac: `brew install ffmpeg` / Windows: `winget install ffmpeg`）
- **日本国内のネット回線から**実行すること（radiko の地域判定のため）

## 画面で操作する（おすすめ）
```sh
python3 radirec.py ui
```
ブラウザで `http://127.0.0.1:8765/` が開きます（このPCからしか開けません）。終了はターミナルで Ctrl+C。

- **番組表**: 局と日付を選んで、放送済みの番組は「録音」、これからの番組は「予約」。放送中の番組は「今からライブ録音」か「放送後に全編録音」を選べます。
- **自動録音**: 局とキーワードを登録すると、一致した番組を放送後に自動で録音します。予約の一覧・解除もここ。
- **録音状況**: 録音中・待機中・完了・失敗と進み具合。
- **録音済み**: 保存したファイルをその場で再生・削除。

UIを開いている間は30分ごとに自動録音をチェックします。UIを閉じていても録音したい場合は、下の「裏で自動録音する」の設定もしておくと確実です（設定ファイルは共通）。

## コマンドで使う
```sh
python3 radirec.py stations                         # 聴ける局と局IDを確認
python3 radirec.py programs TBS 20261014            # 番組表
python3 radirec.py live TBS 60                      # 今から60分録音
python3 radirec.py timefree TBS 202610140100 202610140300   # 過去1週間の番組を録音
python3 radirec.py auto                             # config.json のキーワードで自動録音
```
保存先は `recordings/`（`-o` で変更可）。形式は m4a（AAC、無劣化コピー）。

## 裏で自動録音する
UIの「自動録音」タブ（または `config.json`）で局IDとキーワードを登録しておき、`auto` を1時間おきに走らせるだけ。
放送が終わった番組をタイムフリーから取得するので、PCが放送時間中に寝ていても1週間以内なら取りこぼしません。UIで予約した番組も対象です。録音済みは `recordings/.done.json` に記録され二重録音しません。

**Mac / Linux（cron）** — `crontab -e` で:
```
5 * * * * cd /path/to/radirec && /usr/bin/python3 radirec.py auto >> radirec.log 2>&1
```

**Windows（タスクスケジューラ）** — PowerShell で:
```powershell
schtasks /create /tn radirec /sc hourly /tr "py C:\path\to\radirec\radirec.py auto -c C:\path\to\radirec\config.json"
```

時間指定でライブ録音したい場合は、同じ方法で `live 局ID 分数` を放送開始時刻に登録。

## 注意
- 個人で聴く範囲の私的利用に限ってください（再配布・共有はNG）。radiko の利用規約にも目を通しておいてください。
- radiko 側の仕様変更で動かなくなることがあります。
