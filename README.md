# radirec — radiko 録音ツール（個人利用）

## 準備
- Python 3.8 以上
- ffmpeg（Mac: `brew install ffmpeg` / Windows: `winget install ffmpeg`）
- **日本国内のネット回線から**実行すること（radiko の地域判定のため）

## 使い方
```sh
python3 radirec.py stations                         # 聴ける局と局IDを確認
python3 radirec.py programs TBS 20261014            # 番組表
python3 radirec.py live TBS 60                      # 今から60分録音
python3 radirec.py timefree TBS 202610140100 202610140300   # 過去1週間の番組を録音
python3 radirec.py auto                             # config.json のキーワードで自動録音
```
保存先は `recordings/`（`-o` で変更可）。形式は m4a（AAC、無劣化コピー）。

## 裏で自動録音する
`config.json` に局IDとキーワードを書いておき、`auto` を1時間おきに走らせるだけ。
放送が終わった番組をタイムフリーから取得するので、PCが放送時間中に寝ていても1週間以内なら取りこぼしません。録音済みは `recordings/.done.json` に記録され二重録音しません。

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
