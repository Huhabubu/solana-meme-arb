# solana-meme-arb

A Rust research project for monitoring and later executing cross-DEX arbitrage on mature Solana meme tokens.

## Current stage

Stage 0: repository and CI bootstrap.

No wallet integration, order execution, or profit claims are implemented at this stage.

## Planned stages

1. Pool discovery for selected mature meme tokens.
2. Helius RPC/WSS connectivity and live pool-account subscriptions.
3. DEX-specific pool-state parsing and local swap quoting.
4. Cross-pool opportunity detection and opportunity logging.
5. Atomic transaction construction and simulation.
6. Small-size execution and Jito integration after the monitoring data justifies it.

## Engineering rule

Only verified behavior is reported as complete. Unverified behavior and external-data assumptions must be labeled explicitly.
