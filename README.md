# routines

Claude の定期タスク（Slack Kamo-Dev への通知など）が使うスキルとスクリプトの置き場所。

定期タスクは実行のたびにこのリポジトリを clone し、`skills/<名前>/SKILL.md` の手順に従う。
手順の正本は各 `SKILL.md` で、定期タスクのプロンプトには入口と最低限の約束事だけを書く。

| スキル | 定期タスク | 内容 |
|---|---|---|
| [`vuln-buzz`](skills/vuln-buzz/SKILL.md) | 脆弱性の騒ぎ検知（毎時） | 世の中で騒ぎになっている脆弱性を #vuln-buzz に通知する。新しい兆しが無い回はスクリプトの判定だけで終える |
| [`news-digest`](skills/news-digest/SKILL.md) | IT業界・AI・世界・ゲーム・AI×開発ニュース（毎朝） | 過去 24 時間のニュースを各チャンネルに 1 件のまとめとして投稿する。1 回の実行では 1 セクションの手順書だけを読む |
| [`model-watch`](skills/model-watch/SKILL.md) | AIモデル新着チェック（毎朝） | 新しい AI モデル・初めて見る提供元・価格改定・廃止予定を #ai-models に通知する。新着が無い日は何も投稿しない |

## 試験

```bash
python3 -m unittest discover -s skills/vuln-buzz/scripts
python3 -m unittest discover -s skills/model-watch/scripts
```

秘密情報（トークン、Webhook の URL など）はこのリポジトリに置かない。公開リポジトリである。
