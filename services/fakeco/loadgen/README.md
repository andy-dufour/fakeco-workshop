# Load generator

Random chats, trades, transfers and balance events for FakeCo Securities, through the public API only (SPEC section 3). Run it with `python -m fakeco.loadgen`; it runs for ever.

Every 5 seconds it reads `GET /api/ops/loadgen` (as the hidden operations person `SYS-loadgen`) and obeys `enabled`, `conversations_per_minute`, `transactions_per_minute`, `auto_stories` and `surge`. Every 15 seconds it posts its counts to `POST /api/ops/loadgen/stats` (also when they are all zero: the post tells the API that the load generator is alive).

Settings from the environment: `FAKECO_API_URL` (default: the `concierge-api` Service), `LOADGEN_MAX_CONCURRENT_CONVERSATIONS` (30), `LOADGEN_SEED` (unset: random).

- `conversation.py`: what a customer says at each step of a flow, chosen from the cards of the answer before it (trade ids, chips, ticket ids).
- `transactions.py`: trades, transfers and balance events.
- `runner.py`: the settings, the Poisson arrivals, stories, runs and the stats report. It starts at most one valuation conversation every 4 minutes (`VALUATION_GAP_S`), because a whale's runaway takes minutes and goes on after a customer gives up; 30 % of the valuation asks wait for the whole job (`Behavior.wait_valuation`), the others give up after 20-60 s. While the fault `asset_valuation_rush` is on, the surge's `valuation_per_minute` (2.5 by default) starts that many more valuation conversations a minute, on a loop of their own that ignores `VALUATION_GAP_S` and `conversations_per_minute` (60 % of them from a whale when one is free). `api.py`: sign-in and calls. `util.py`: clock, counters, spans.

Tests: `python -m pytest tests/test_loadgen.py` (a fake API in `tests/fake_api.py`, time sped up, no network).
