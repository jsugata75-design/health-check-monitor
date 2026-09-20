# health-check-monitor — ヘルチェック Web予約 空き枠監視＋自動予約変更

既存予約 **人間ドックC / 大宮センター 2027-01-19(火) 08:20** を、
**セデーション・鎮静剤付き**で **2026-12-31 までの平日枠**に前倒しする。
空きを見つけたら **その場で予約変更まで自動実行**し、結果をメール＋ntfy で知らせる。

対象サイト: https://web.health-check.jp/webreserve/wpgLogin.aspx

## 条件（本人指示 2026-09-14）

| 項目 | 設定 |
|---|---|
| センター優先順 | 大宮 > 池袋 > 新宿西口 > 渋谷アクシュ > 日本橋 |
| 曜日 | 平日（月〜金） |
| 時間帯 | AM / PM どちらも可（同日なら AM 優先） |
| 除外日 | 10/31, 11/11, 12/2 |
| 期限 | 2026-12-31 |
| 直近除外 | 2日先まで（受診前日・当日はWeb変更不可のため） |
| 受診時間 | 複数候補があれば最も早い時刻（`HC_TIME_PREF=latest` で逆） |

複数の空きが同時にあれば **センター優先順 → 日付が早い順 → AM** で1つ選ぶ。

## 仕組み

ブラウザは使わない。ASP.NET WebForms の POST を `urllib` で追う（stdlib のみ）。

```
wpgLogin.aspx ──ログイン──▶ Default.aspx
 ──予約状況を確認する──▶ wpgWBP01010 (予約内容)
 ──予約変更──▶ wpgWBP02141 (STEP3 オプション)  ← セデーション✓
 ──次に進む──▶ wpgWBP02151 (STEP4 2週間カレンダー) ← 「前週」で 9月まで遡り ○ を収集
 [空きあり] ○クリック → 時間ラジオ → STEP5 料金確認 → STEP6 お客様情報(既定値のまま)
            → STEP7 予約内容確認 → 「この内容で予約する」 → 完了
 → 予約状況ページを引き直し、施設・日付・セデーションが変わったことを確認して初めて「成功」
```

- カレンダーの各セルは `<a linktype="select_schedule" dtschedule="YYYYMMDD" cdofc="0010xx" ampm="AM|PM">` で、
  中身が `○`=空き / `×`=満 / `－`=選択不可 / `休`。**○ 以外は一切触らない**。
- センターコード: 大宮=001011, 池袋=001007, 新宿西口=001003, 渋谷アクシュ=001012, 日本橋=001008
  （他: 001001 横浜東口, 001002 横浜西口, 001004 レディース横浜, 001005 ファーストプレイス横浜,
  001006 レディース新宿, 001009 川崎, 001010 横濱ゲートタワー）
- 「前週」「次週」のリンク名はページ端で入れ替わる（最古ページでは 次週 が `0_2_1`）ので **テキストで探す**。
- 1回の巡回は約1分（10ページ×約5秒）。
- **○を見つけたページで、その場で押す**（2026-09-20〜）。以前は全ページ走査後にフローをやり直していたが、
  9/18 に渋谷 11/20 AM が 43秒後の再確認で × になっていた（○は1分持たない）。同一ページ内は優先順→消えていたら次の候補。
- 完了すると `state.json` に `done` が書かれ、以後は何もしない。

## 認証情報

- ログインID（画面右上の ID）は `login_id.txt`（gitignore）か環境変数 `HC_LOGIN_ID`。パスワードは **Keychain** `service=health-check-web`（クラウドでは `HC_PASSWORD`）。
- 登録/変更はダイアログ方式（ターミナルに打たない）:

      python3 ~/health-check-monitor/monitor.py --setup

- このサイトはパスワードの代わりに **Web予約番号** でもログインできる。
  ただし予約変更後は番号が変わる可能性があるので、本物のパスワードを入れておくのが安全。
- メールは Gmail SMTP、アプリパスワードは既存の Keychain `wonder-gym-mail` を流用。
  ntfy トピックは yuzureru-monitor と同じ（plist の `NTFY_TOPIC`）。

## 使い方

    # 巡回だけ（通知なし・予約なし）
    python3 ~/health-check-monitor/monitor.py --dry-run

    # 巡回して空きがあれば通知のみ（自動予約しない）
    python3 ~/health-check-monitor/monitor.py --no-reserve

    # 指定枠の○を押して時間候補まで確認（予約はしない）
    python3 ~/health-check-monitor/monitor.py --dry-run --test-reserve 001005:20261106:AM

    # 通知テスト
    MAIL_FROM=<Gmail> MAIL_TO=<宛先> python3 ~/health-check-monitor/monitor.py --test-mail

    # 各ページの HTML を snapshots/ に残す（デバッグ）
    python3 ~/health-check-monitor/monitor.py --dry-run --snapshot

## 自動実行（launchd）

    cp ~/health-check-monitor/com.junsugata.health-check-monitor.plist ~/Library/LaunchAgents/
    launchctl load ~/Library/LaunchAgents/com.junsugata.health-check-monitor.plist
    # 止める
    launchctl unload ~/Library/LaunchAgents/com.junsugata.health-check-monitor.plist

- 10分ごと（`StartInterval=600`）、ログ `~/Library/Logs/health-check-monitor.log`
- 自動予約を止めたい: plist の `AUTO_RESERVE` を `0` にして unload → load
- 除外日を増やす: plist の `HC_EXCLUDE`（カンマ区切り ISO 日付）

## 24時間監視（GitHub Actions）— Macがスリープ中でも回す

ローカル launchd は Mac が起きている間しか動かない（実測: 夜間・外出中は数時間の空白）。
`.github/workflows/monitor.yml` を Public リポジトリに置くと、GitHub のランナーが **5分ごと・終日** 同じ `monitor.py` を実行する。
Public なら実行時間は無料（Private だと月 2,000分の無料枠を超える）。コードに個人情報・秘密情報は含めていない。

### セットアップ（1回だけ・本人がブラウザで行う）

1. https://github.com/new でリポジトリ `health-check-monitor` を **Public** で作成（README 等は追加しない）
2. ローカルから push（下記）
3. リポジトリの Settings → Secrets and variables → Actions → New repository secret で以下を登録

| Secret | 中身 |
|---|---|
| `HC_LOGIN_ID` | ヘルチェックのログインID（画面右上の ID） |
| `HC_PASSWORD` | ヘルチェックのパスワード |
| `GMAIL_APP_PASSWORD` | Gmail アプリパスワード16桁（Keychain `wonder-gym-mail` と同じ値） |
| `MAIL_FROM` | 送信元 Gmail |
| `MAIL_TO` | 宛先（カンマ区切り可） |
| `NTFY_TOPIC` | ntfy トピック名 |

4. Actions タブ → health-check-monitor → **Run workflow** で1回手動実行し、ログに「10ページ走査」が出れば完了

### push

    cd ~/health-check-monitor && git add -A && git commit -m "..." && git push

### ローカルとの併用

両方動かしてよい。予約変更は「予約状況ページを引き直して既に目標を満たしていれば何もしない」ガードがあるので、
片方が取った直後にもう片方が二重に変更することはない（同じ数秒に重なった場合だけ理論上あり得るが、どちらも条件内の枠）。
クラウド側の `state.json` は Actions cache で引き継ぐ。

## 通知

| 件名 | 意味 |
|---|---|
| 【ヘルチェック】予約変更 完了: … | 自動予約成功。元の 1/19 は置き換わっている |
| 【ヘルチェック】空きあり・自動予約に失敗: … | ○は出たが取れなかった（直前に埋まった等）。手動で試す |
| 【ヘルチェック】空きが出たが直前に埋まった: … | ○を押しに行った時点で × だった。動きがある日 |
| 【ヘルチェック】空き枠あり: … | `AUTO_RESERVE=0` のときの通知のみ |
| 【ヘルチェック監視】ログインできません | パスワード誤り/変更。`--setup` で再登録 |

## 実績・メモ

- 2026-09-14 初回 dry-run: 9/14〜1/31 の10ページ走査、○は ファーストプレイス横浜 3枠のみ（対象外）。
  `--test-reserve` で ○クリック→時間候補(11:40) の取得まで確認済み。STEP5〜完了はブラウザで手順確認済み（未実行）。
- 2026-09-18 15:20 **渋谷アクシュ 11/20(金) AM に○** → 自動予約に入ったが 43秒後の再確認で ×（取り逃し）。
  これを受けて「その場で押す」方式に変更。同日は大宮の1月PM枠（1/5,1/19,1/22,1/26,1/29）が終日出ていたが期限外（本人判断で見送り）。
