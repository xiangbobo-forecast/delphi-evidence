# delphi-evidence

Evidence-mining agent `xiangbobo-evidence` for the [Delphi Agent Competition](https://competition.delphi.fyi/participate) (Gensyn, 2026-11-25 to 2026-12-11).

每 2 小时跑一次（GitHub Actions）：读竞赛市场列表 → 搜最新新闻（Bing News RSS、GDELT，含当地语言）→ 用免费 Gemini 判断有没有市场创建后才出现、带日期和出处的新事实 → 有就提交（每市场最多 3 条，只站一边）→ 读回执，写 `REPORT.md`（中文战报）。

- 需要的 Secrets：`DELPHI_API_ACCESS_KEY`（Delphi Mainnet API key）、`GEMINI_API_KEY`。
- 不需要私钥，不交易，不签名。奖励自动发到 Delphi 账户钱包。
- 手动试跑：Actions → mine-evidence → Run workflow，`dry_run=1` 只看不交。
- 调整：`MARKETS_PER_RUN`（默认 4）、`MAX_SUBMITS_PER_RUN`（默认 2）。

MIT License.
